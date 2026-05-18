"""Main plugin bundle exporter.

Transforms APM packages into plugin-native directories.
"""

import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path

from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
from ...models.apm_package import APMPackage
from ...utils.console import _rich_warning
from ...utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree
from ..packer import PackResult
from .collectors import (
    collect_apm_components,
    collect_bare_skill,
    collect_hooks_from_apm,
    collect_hooks_from_root,
    collect_mcp,
    collect_root_plugin_components,
)
from .hooks_mcp import deep_merge
from .utils import (
    dep_install_path,
    get_dev_dependency_urls,
    merge_file_map,
    sanitize_bundle_name,
    validate_output_rel,
)


def _compat_rich_warning(message: str) -> None:
    """Route warnings through the package-level seam when available."""
    compat_pkg = sys.modules.get("apm_cli.bundle.plugin_exporter")
    compat_warn = getattr(compat_pkg, "_rich_warning", None) if compat_pkg else None
    if compat_warn is not None and compat_warn is not _compat_rich_warning:
        compat_warn(message)
        return
    _rich_warning(message)


def _find_or_synthesize_plugin_json(
    project_root: Path,
    apm_yml_path: Path,
    logger=None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``."""
    from ...deps.plugin_parser import synthesize_plugin_json_from_apm_yml
    from ...utils.helpers import find_plugin_json

    plugin_json_path = find_plugin_json(project_root)
    if plugin_json_path is not None:
        try:
            return json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warn_msg = (
                f"Found plugin.json at {plugin_json_path} but could not parse it: {exc}. "
                "Falling back to synthesis from apm.yml."
            )
            if logger:
                logger.warning(warn_msg)
            else:
                _compat_rich_warning(warn_msg)

    else:
        warn_msg = (
            "No plugin.json found. Synthesising from apm.yml. Consider running 'apm init --plugin'."
        )
        if logger:
            logger.warning(warn_msg)
        else:
            _compat_rich_warning(warn_msg)
    return synthesize_plugin_json_from_apm_yml(apm_yml_path)


def _update_plugin_json_paths(plugin_json: dict, output_files: list[str], logger=None) -> dict:
    r"""Strip component-path keys from ``plugin.json``.

    Per the official Claude Code plugin manifest schema, the
    ``agents``/``skills``/``commands`` keys point to *additional* files
    OUTSIDE the convention directories (``agents/``, ``skills/``,
    ``commands/``) and each entry must match ``^\./.*`` (relative path)
    and the per-key file-extension pattern. The ``instructions`` key is
    not defined by the schema at all. The convention directories
    themselves are auto-discovered by Claude Code -- listing them here
    is invalid (or unrecognised).

    APM emits everything into the convention directories, so we drop
    these keys entirely to keep the manifest schema-conformant.

    The ``output_files`` argument is retained for signature stability
    (and as a hook for future "additional files" extensions); it is
    currently unused.
    """
    result = dict(plugin_json)
    stripped = [k for k in ("agents", "skills", "commands", "instructions") if k in result]
    for key in stripped:
        result.pop(key, None)
    if stripped:
        msg = (
            "Stripped schema-invalid keys from authored plugin.json: "
            f"{', '.join(stripped)} -- convention directories are auto-discovered by Claude Code"
        )
        if logger:
            logger.warning(msg)
        else:
            _compat_rich_warning(msg)
    return result


def _collect_components_from_dependencies(
    lockfile: LockFile | None,
    apm_modules_dir: Path,
    dev_dep_urls: set[tuple[str, str]],
    force: bool,
) -> tuple[dict[str, tuple[Path, str]], list[str], dict, dict]:
    """Collect components from all non-dev dependencies.

    Returns:
        Tuple of (file_map, collisions, merged_hooks, merged_mcp).
    """
    file_map: dict[str, tuple[Path, str]] = {}
    collisions: list[str] = []
    merged_hooks: dict = {}
    merged_mcp: dict = {}

    if not lockfile:
        return file_map, collisions, merged_hooks, merged_mcp

    for dep in lockfile.get_all_dependencies():
        # Prefer lockfile is_dev flag (covers transitive deps);
        # fall back to apm.yml URL matching for older lockfiles
        if (
            getattr(dep, "is_dev", False)
            or (dep.repo_url, getattr(dep, "virtual_path", "") or "") in dev_dep_urls
        ):
            continue

        install_path = dep_install_path(dep, apm_modules_dir)
        if not install_path.is_dir():
            continue

        dep_name = dep.repo_url

        # Collect from .apm/
        dep_apm_dir = install_path / ".apm"
        dep_components = collect_apm_components(dep_apm_dir)

        # Also collect root-level plugin-native dirs from the dep
        dep_components.extend(collect_root_plugin_components(install_path))

        # Bare Claude skills: SKILL.md at dep root with no skills/ subdir
        collect_bare_skill(install_path, dep, dep_components)

        merge_file_map(file_map, dep_components, dep_name, force, collisions)

        # Hooks -- deps merge (first wins among deps)
        dep_hooks = collect_hooks_from_apm(dep_apm_dir)
        dep_hooks_root = collect_hooks_from_root(install_path)
        deep_merge(dep_hooks, dep_hooks_root, overwrite=False)
        deep_merge(merged_hooks, dep_hooks, overwrite=False)

        # MCP -- deps merge (first wins among deps)
        dep_mcp = collect_mcp(install_path)
        deep_merge(merged_mcp, dep_mcp, overwrite=False)

    return file_map, collisions, merged_hooks, merged_mcp


def _collect_components_from_root(
    project_root: Path,
    pkg_name: str,
    force: bool,
    file_map: dict[str, tuple[Path, str]],
    collisions: list[str],
    merged_hooks: dict,
    merged_mcp: dict,
) -> None:
    """Collect components from root package and merge into file_map.

    Mutates file_map, collisions, merged_hooks, and merged_mcp in place.
    """
    own_apm_dir = project_root / ".apm"
    own_components = collect_apm_components(own_apm_dir)
    own_components.extend(collect_root_plugin_components(project_root))
    merge_file_map(file_map, own_components, pkg_name, force, collisions)

    # Hooks -- root package wins on key collision
    root_hooks = collect_hooks_from_apm(own_apm_dir)
    root_hooks_top = collect_hooks_from_root(project_root)
    deep_merge(root_hooks, root_hooks_top, overwrite=False)
    deep_merge(merged_hooks, root_hooks, overwrite=True)

    # MCP -- root package wins on server-name collision
    root_mcp = collect_mcp(project_root)
    deep_merge(merged_mcp, root_mcp, overwrite=True)


def _security_scan_files(file_map: dict[str, tuple[Path, str]], logger) -> None:
    """Scan bundle files for security issues (warn-only, never blocks)."""
    from ...security.gate import WARN_POLICY, SecurityGate

    scan_findings_total = 0
    for _rel, (src, _owner) in file_map.items():
        if src.is_symlink():
            continue
        if src.is_dir():
            verdict = SecurityGate.scan_files(src, policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
        elif src.is_file():
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            verdict = SecurityGate.scan_text(text, str(src), policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
    if scan_findings_total:
        warn_msg = (
            f"Bundle contains {scan_findings_total} hidden character(s) across "
            f"source files — run 'apm audit' to inspect before publishing"
        )
        if logger:
            logger.warning(warn_msg)
        else:
            _compat_rich_warning(warn_msg)


def _write_bundle_files(
    file_map: dict[str, tuple[Path, str]],
    bundle_dir: Path,
) -> None:
    """Write collected files to bundle directory."""
    for output_rel, (source_abs, _owner) in file_map.items():
        if not validate_output_rel(output_rel):
            continue
        dest = bundle_dir / output_rel
        if source_abs.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            ensure_path_within(dest, bundle_dir)
        except PathTraversalError:
            continue
        shutil.copy2(source_abs, dest, follow_symlinks=False)


def _write_metadata_files(
    bundle_dir: Path,
    merged_hooks: dict,
    merged_mcp: dict,
    plugin_json: dict,
    output_files: list[str],
    logger,
) -> None:
    """Write hooks.json, .mcp.json, and plugin.json to bundle."""
    if merged_hooks:
        (bundle_dir / "hooks.json").write_text(
            json.dumps(merged_hooks, indent=2, sort_keys=True), encoding="utf-8"
        )

    if merged_mcp:
        (bundle_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": merged_mcp}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    plugin_json = _update_plugin_json_paths(plugin_json, output_files, logger=logger)
    (bundle_dir / "plugin.json").write_text(
        json.dumps(plugin_json, indent=2, sort_keys=False), encoding="utf-8"
    )


def _write_enriched_lockfile(
    lockfile: LockFile | None,
    bundle_dir: Path,
    target: str | None,
) -> None:
    """Write enriched lockfile with bundle_files manifest."""
    if lockfile is None:
        return

    from ..lockfile_enrichment import enrich_lockfile_for_pack

    bundle_files: dict[str, str] = {}
    for fp in bundle_dir.rglob("*"):
        if not fp.is_file() or fp.is_symlink():
            continue
        rel = fp.relative_to(bundle_dir).as_posix()
        if rel == "apm.lock.yaml":
            continue
        bundle_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    # Issue #1207 D1: do NOT silently substitute ``"copilot"`` when
    # ``target`` is missing.  Bundles are target-agnostic at install
    # time; ``pack.target`` is recorded as informational metadata only.
    # Falling back to ``"all"`` preserves the lockfile-filter shape
    # (which uses target prefixes to narrow each dep's deployed_files
    # list to the union of supported targets) without locking the
    # bundle to a single client.
    enriched_yaml = enrich_lockfile_for_pack(
        lockfile,
        "plugin",
        target or "all",
        bundle_files=bundle_files,
    )
    (bundle_dir / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")


def export_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Export the project as a plugin-native directory.

    The output contains only plugin-spec artefacts (``agents/``, ``skills/``,
    ``commands/``, ``plugin.json``, …) with no APM-specific files.

    Args:
        project_root: Root of the project containing ``apm.yml``.
        output_dir: Parent directory for the generated bundle.
        target: Unused for plugin format (reserved for future use).
        archive: If True, produce a ``.tar.gz`` and remove the directory.
        dry_run: If True, resolve the file list without writing to disk.
        force: On collision, last writer wins instead of first.

    Returns:
        :class:`PackResult` describing what was produced.
    """
    # 1. Read lockfile
    migrate_lockfile_if_needed(project_root)
    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)

    # 2. Read apm.yml
    apm_yml_path = project_root / "apm.yml"
    package = APMPackage.from_apm_yml(apm_yml_path)
    pkg_name = package.name
    pkg_version = package.version or "0.0.0"

    # Guard: reject local-path dependencies (non-portable)
    for dep_ref in package.get_apm_dependencies():
        if dep_ref.is_local:
            raise ValueError(
                f"Cannot pack — apm.yml contains local path dependency: "
                f"{dep_ref.local_path}\n"
                f"Local dependencies are for development only. Replace them with "
                f"remote references (e.g., 'owner/repo') before packing."
            )

    # 3. Find or synthesise plugin.json
    plugin_json = _find_or_synthesize_plugin_json(project_root, apm_yml_path, logger=logger)

    # 4. devDependencies filtering
    dev_dep_urls = get_dev_dependency_urls(apm_yml_path)

    # 5. Collect components from dependencies
    apm_modules_dir = project_root / "apm_modules"
    file_map, collisions, merged_hooks, merged_mcp = _collect_components_from_dependencies(
        lockfile, apm_modules_dir, dev_dep_urls, force
    )

    # 6. Collect own components and merge
    _collect_components_from_root(
        project_root, pkg_name, force, file_map, collisions, merged_hooks, merged_mcp
    )

    # 7. Emit collision warnings
    for msg in collisions:
        if logger:
            logger.warning(msg)
        else:
            _compat_rich_warning(msg)

    # 8. Build output file list (sorted for determinism)
    output_files = sorted(file_map.keys())

    # Add generated files to the list
    if merged_hooks:
        output_files.append("hooks.json")
    if merged_mcp:
        output_files.append(".mcp.json")
    output_files.append("plugin.json")

    # 9. Dry run -- return file list without writing
    safe_name = sanitize_bundle_name(pkg_name)
    safe_version = sanitize_bundle_name(pkg_version)
    bundle_dir = output_dir / f"{safe_name}-{safe_version}"
    ensure_path_within(bundle_dir, output_dir)
    if dry_run:
        return PackResult(bundle_path=bundle_dir, files=output_files)

    # 10. Security scan (warn-only, never blocks)
    _security_scan_files(file_map, logger)

    # 11. Write files to output directory (clean slate to prevent symlink attacks)
    if bundle_dir.exists():
        safe_rmtree(bundle_dir, output_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    _write_bundle_files(file_map, bundle_dir)

    # 12-14. Write metadata files
    _write_metadata_files(bundle_dir, merged_hooks, merged_mcp, plugin_json, output_files, logger)

    # 14b. Write enriched lockfile with bundle_files manifest
    _write_enriched_lockfile(lockfile, bundle_dir, target)

    result = PackResult(bundle_path=bundle_dir, files=output_files)

    # 15. Archive if requested
    if archive:
        archive_path = output_dir / f"{bundle_dir.name}.tar.gz"
        ensure_path_within(archive_path, output_dir)
        with tarfile.open(archive_path, "w:gz") as tar:

            def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                if info.issym() or info.islnk():
                    return None  # reject symlinks injected after write
                return info

            tar.add(bundle_dir, arcname=bundle_dir.name, filter=_tar_filter)
        shutil.rmtree(bundle_dir)
        result.bundle_path = archive_path

    return result
