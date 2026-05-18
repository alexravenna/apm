"""Local bundle (tarball / directory) integration pipeline.

Provides ``integrate_local_bundle`` and its private helpers.
The public symbol is re-exported from ``apm_cli.install.services`` so
all existing import paths continue to work.
"""

from __future__ import annotations

import builtins
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.utils.diagnostics import DiagnosticCollector

# Defensive builtins aliases (see __init__ module-level comment).
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass
class LocalBundleOpts:
    """Optional arguments for :func:`integrate_local_bundle`."""

    diagnostics: Any = None
    logger: Any = None
    scope: Any = None
    alias: str | None = None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_pack_files(bundle_info: Any, bundle_dir: Path) -> dict[str, str]:
    """Build the ``{rel_path: sha256hex}`` mapping for all bundle files.

    Reads from ``bundle_info.lockfile["pack"]["bundle_files"]`` when
    available; falls back to a recursive directory walk.  In both cases
    ``plugin.json`` and ``.mcp.json`` are filtered out (they are bundle
    metadata, never deployable in consumer projects).

    Returns an empty dict when the bundle has no deployable files.
    """
    pack_files: dict[str, str] = {}
    if bundle_info.lockfile:
        pack = bundle_info.lockfile.get("pack") or {}
        bf = pack.get("bundle_files") or {}
        if isinstance(bf, dict):
            pack_files = {str(k): str(v) for k, v in bf.items()}

    if not pack_files:
        # Fallback: walk bundle and hash everything except apm.lock.yaml
        # and plugin.json / .mcp.json.  Prevents zero-deploy when an older
        # bundle without bundle_files lands.
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            if rel == "apm.lock.yaml" or rel.lower() == "plugin.json" or rel.lower() == ".mcp.json":
                continue
            pack_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    # py-arch-2: Filter bundle-metadata files (plugin.json, .mcp.json) out of
    # pack_files BEFORE the per-target loop.  Case-insensitive match mirrors
    # the fallback walk above and the previously-inline guards in the deploy
    # loop.
    filtered: dict[str, str] = {}
    for _rel, _hash in pack_files.items():
        if _rel.lower() in {"plugin.json", ".mcp.json"}:
            continue
        filtered[_rel] = _hash
    return filtered


def _stage_instruction_dest(
    rel: str,
    slug: Any,
    project_root: Path,
    logger: InstallLogger | None,
) -> tuple[Path, Path] | None:
    """Resolve stage dest for a bundled instruction on a compile-only target.

    Called when the current target lacks the ``"instructions"`` primitive
    (e.g. opencode, codex, gemini) so the file must be staged under
    ``apm_modules/<slug>/.apm/instructions/`` for ``apm compile`` to pick
    up later.

    Performs strict slug validation before constructing any filesystem path.

    Returns ``(dest, stage_root)`` on success, or ``None`` when the slug
    is invalid (the caller should increment *skipped* and ``continue``).
    """
    from apm_cli.utils.path_security import (
        PathTraversalError,
        ensure_path_within,
        validate_path_segments,
    )

    _slug_str = str(slug)
    # CR1.5 (#1217 review): ASCII-only validation — str.isalnum() accepts
    # non-Latin Unicode chars which would slip past [A-Za-z0-9._-].
    _ALLOWED = builtins.set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    _slug_ok = (
        bool(_slug_str)
        and all(c in _ALLOWED for c in _slug_str)
        and not _slug_str.startswith(".")
        and not _slug_str.endswith(".")
        and ".." not in _slug_str
    )
    if not _slug_ok:
        if logger is not None:
            logger.warning(
                f"Skipped instruction staging for unsafe slug {_slug_str!r}: "
                "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
            )
        return None
    try:
        validate_path_segments(_slug_str, context="bundle slug")
    except PathTraversalError as exc:
        if logger is not None:
            logger.warning(f"Skipped instruction staging for unsafe slug {_slug_str!r}: {exc}")
        return None
    stage_root = project_root / "apm_modules" / slug / ".apm" / "instructions"
    try:
        ensure_path_within(stage_root, project_root / "apm_modules")
    except PathTraversalError as exc:
        if logger is not None:
            logger.warning(f"Skipped unsafe stage root for {slug!r}: {exc}")
        return None
    # PR #1217 review: preserve nested subdirs under ``instructions/`` so
    # two files with the same basename do not collide at the staged location.
    _rel_under_instructions = rel.split("/", 1)[1] if "/" in rel else Path(rel).name
    dest = stage_root / _rel_under_instructions
    return dest, stage_root


def _compute_bundle_record(
    dest: Path,
    project_root: Path,
    scope: Any,
) -> str:
    """Return the lockfile key string for a deployed bundle file.

    User-scope installs use absolute paths; project-scope installs use
    ``project_root``-relative POSIX paths (with absolute fallback when
    *dest* is outside *project_root*).
    """
    from apm_cli.core.scope import InstallScope

    try:
        if scope == InstallScope.USER:
            return dest.as_posix()
        else:
            return (
                dest.relative_to(project_root).as_posix()
                if dest.is_relative_to(project_root)
                else dest.as_posix()
            )
    except ValueError:
        return dest.as_posix()


def _deploy_file(
    src: Path,
    dest: Path,
    record: str,
    expected_hash: str,
    force: bool,
    dry_run: bool,
    diagnostics: DiagnosticCollector | None,
    logger: InstallLogger | None,
) -> tuple[str | None, str | None, bool]:
    """Deploy a single bundle file to *dest*.

    Handles dry-run (no writes), collision detection (skip when content
    differs and *force* is False), and the actual copy + hash.

    Returns ``(record, file_hash, was_skipped)``:

    * On dry-run: ``(record, "sha256:<hex>", False)``
    * On skip:    ``(None, None, True)``
    * On deploy:  ``(record, "sha256:<hex>", False)``
    """
    from apm_cli.utils.content_hash import compute_file_hash

    if dry_run:
        if logger:
            logger.verbose_detail(f"[dry-run] would deploy {record}")
        # Normalize to "sha256:<hex>" so the dry-run lockfile preview matches
        # the format written by ``compute_file_hash`` on the real deploy path.
        return record, f"sha256:{expected_hash}", False

    # Collision handling: skip if file exists with different content (unless
    # --force).  Idempotent (same-content) writes are allowed through.
    if dest.exists() and not force:
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None
        if existing_hash and existing_hash != expected_hash:
            msg = (
                f"Skipped {record}: file exists with different "
                "content. Re-run with --force to overwrite."
            )
            if diagnostics is not None:
                diagnostics.warn(msg)
            elif logger is not None:
                logger.warning(msg)
            return None, None, True

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest, follow_symlinks=False)
    # IM4: hash the deployed file (post-copy) rather than trusting the source
    # bundle's expected_hash.  Today the integrator is a raw copy so the
    # values match, but documenting deployed-file provenance now keeps the
    # lockfile honest if future transforms mutate content during deploy.
    file_hash = compute_file_hash(dest)
    if logger:
        logger.verbose_detail(f"deployed {record}")
    return record, file_hash, False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def integrate_local_bundle(
    bundle_info: Any,
    project_root: Path,
    *,
    targets: Any,
    force: bool = False,
    dry_run: bool = False,
    opts: LocalBundleOpts | None = None,
    **kwargs,
) -> dict:
    """Integrate a detected local bundle into project / user scope.

    Local bundles are produced by ``apm pack`` and shipped (via shared file,
    USB, etc.) to environments that cannot reach the source registry.  This
    orchestrator deploys the bundle's plugin-format files into each active
    target's deploy root and returns a result dict so the caller can persist
    ``local_deployed_files`` / ``local_deployed_file_hashes`` into the
    project lockfile.

    The bundle is treated as a *synthetic* package -- its slug derives from
    *alias* (``--as``) when provided, else from ``bundle_info.package_id``.

    Important contract: this function does **NOT** mutate ``apm.yml``.  Local
    bundles are imperative deploys, not declarative dependencies.

    Args:
        bundle_info: ``LocalBundleInfo`` describing the verified bundle.
        project_root: Workspace root (or ``Path.home()`` for ``--global``).
        targets: Resolved ``TargetProfile`` instances from
            ``resolve_targets()``.
        force: When ``True``, overwrite locally-modified files on collision.
        dry_run: When ``True``, report what would be deployed without
            writing to disk.
        opts: Optional :class:`LocalBundleOpts` for diagnostics, logger,
            scope, and alias.

    Returns:
        Dict with keys ``deployed_files`` (list[str]),
        ``deployed_file_hashes`` (dict[str, str]), ``skipped`` (int), and
        per-primitive counters (``skills``, ``agents``, ``commands``, ...).
    """
    _opts = opts or LocalBundleOpts(
        diagnostics=kwargs.get("diagnostics"),
        logger=kwargs.get("logger"),
        scope=kwargs.get("scope"),
        alias=kwargs.get("alias"),
    )
    diagnostics = _opts.diagnostics
    logger = _opts.logger
    scope = _opts.scope
    alias = _opts.alias
    from apm_cli.utils.path_security import PathTraversalError, validate_path_segments

    bundle_dir: Path = bundle_info.source_dir
    pack_files = _build_pack_files(bundle_info, bundle_dir)

    if not pack_files:
        return {
            "deployed_files": [],
            "deployed_file_hashes": {},
            "skipped": 0,
            "skills": 0,
            "agents": 0,
            "commands": 0,
            "hooks": 0,
            "instructions": 0,
            "prompts": 0,
            "sub_skills": 0,
        }

    slug = alias or bundle_info.package_id
    if logger:
        logger.verbose_detail(
            f"Integrating local bundle '{slug}' "
            f"({len(pack_files)} file(s), targets={[t.name for t in targets]})"
        )

    # NOTE(M-arch-1): Local bundles intentionally do NOT route through
    # ``integrate_package_primitives`` -- they are an imperative deploy of
    # opaque files keyed by ``pack.bundle_files`` rather than a primitive
    # tree.  Revisit when local-bundle install needs to share collision /
    # link-resolution logic with the dependency-resolver pipeline.
    deployed_files: builtins.list[str] = []
    deployed_hashes: builtins.dict[str, str] = {}
    skipped = 0

    for target in targets:
        # Resolve deploy root for this target.  Cowork targets can return
        # a dynamically-resolved path; fall back to root_dir under
        # project_root otherwise.
        resolved_root = getattr(target, "resolved_deploy_root", None)
        if resolved_root is not None:
            default_deploy_root = Path(resolved_root)
        else:
            default_deploy_root = project_root / target.root_dir

        # Build a primitive→deploy_root lookup so bundle entries that fall
        # under a primitive with an explicit ``deploy_root`` (e.g.
        # skills→.agents) are routed to the converged directory rather than
        # the per-client ``target.root_dir``.
        _primitive_roots: builtins.dict[str, Path] = {}
        for prim_name, prim_mapping in (target.primitives or {}).items():
            if getattr(prim_mapping, "deploy_root", None) and resolved_root is None:
                _primitive_roots[prim_name] = project_root / prim_mapping.deploy_root

        for rel, expected_hash in sorted(pack_files.items()):
            # CR1: bundle_files keys come from untrusted lockfile YAML inside
            # the bundle.  Reject traversal sequences before constructing any
            # filesystem path, then assert the resolved destination stays
            # inside ``deploy_root``.
            try:
                validate_path_segments(str(rel), context="bundle_files key")
            except PathTraversalError as exc:
                if logger is not None:
                    logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
                skipped += 1
                continue
            src = bundle_dir / rel
            if not src.is_file() or src.is_symlink():
                skipped += 1
                continue

            # Issue #1207 D2.b: for compile-only targets (opencode, codex,
            # gemini -- no ``instructions`` primitive in their profile),
            # bundle ``instructions/*.md`` files must be staged under
            # ``apm_modules/<slug>/.apm/instructions/`` so ``apm compile``
            # can merge them into the target's output file.
            _first_seg = rel.split("/", 1)[0] if "/" in rel else ""
            if _first_seg == "instructions" and "instructions" not in (target.primitives or {}):
                _stage = _stage_instruction_dest(rel, slug, project_root, logger)
                if _stage is None:
                    skipped += 1
                    continue
                dest, deploy_root = _stage
            else:
                # Route the file to the correct deploy root.  If the first
                # path segment matches a primitive with an explicit
                # ``deploy_root`` (e.g. ``skills/`` -> ``.agents/``), use the
                # converged directory.  Otherwise fall back to the default.
                deploy_root = _primitive_roots.get(_first_seg, default_deploy_root)
                dest = deploy_root / rel

            try:
                from apm_cli.utils.path_security import ensure_path_within

                ensure_path_within(dest, deploy_root)
            except PathTraversalError as exc:
                if logger is not None:
                    logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
                skipped += 1
                continue

            record = _compute_bundle_record(dest, project_root, scope)
            deployed_record, file_hash, was_skipped = _deploy_file(
                src, dest, record, expected_hash, force, dry_run, diagnostics, logger
            )
            if was_skipped:
                skipped += 1
            elif deployed_record:
                deployed_files.append(deployed_record)
                deployed_hashes[deployed_record] = file_hash  # type: ignore[assignment]

    return {
        "deployed_files": deployed_files,
        "deployed_file_hashes": deployed_hashes,
        "skipped": skipped,
        "skills": 0,
        "agents": 0,
        "commands": 0,
        "hooks": 0,
        "instructions": 0,
        "prompts": 0,
        "sub_skills": 0,
    }


__all__ = ["integrate_local_bundle"]
