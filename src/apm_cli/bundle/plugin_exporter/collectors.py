"""Component collectors for plugin export.

Collects agents, skills, commands, instructions, and hooks from APM packages.
"""

import json
from pathlib import Path, PurePosixPath

from ...deps.lockfile import LockedDependency


def _collect_flat(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
) -> None:
    """Add every regular non-symlink file directly inside *src_dir*."""
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.iterdir()):
        if f.is_file() and not f.is_symlink():
            name = rename(f.name) if rename else f.name
            out.append((f, f"{output_prefix}/{name}"))


def _collect_recursive(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
) -> None:
    """Add every regular non-symlink file under *src_dir*, preserving hierarchy."""
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(src_dir)
        name = rename(rel.name) if rename else rel.name
        out_rel = (rel.parent / name).as_posix()
        out.append((f, f"{output_prefix}/{out_rel}"))


def _rename_prompt(name: str) -> str:
    """Strip the ``.prompt`` infix so ``foo.prompt.md`` becomes ``foo.md``."""
    if name.endswith(".prompt.md"):
        return name[: -len(".prompt.md")] + ".md"
    return name


def _normalize_bare_skill_slug(slug: str) -> str:
    """Normalise bare-skill slugs derived from dependency virtual paths."""
    normalised = slug.replace("\\", "/").strip("/")
    while normalised.startswith("skills/"):
        normalised = normalised[len("skills/") :].lstrip("/")
    if normalised == "skills":
        return ""
    return PurePosixPath(normalised).as_posix() if normalised else ""


def collect_apm_components(apm_dir: Path) -> list[tuple[Path, str]]:
    """Collect all components from a package's ``.apm/`` directory.

    Returns a list of ``(source_abs, output_rel_posix)`` tuples using the
    APM → plugin mapping table.
    """
    components: list[tuple[Path, str]] = []
    if not apm_dir.is_dir():
        return components

    # agents/ -> agents/
    _collect_flat(apm_dir / "agents", "agents", components)

    # skills/ -> skills/ (preserve sub-directory structure)
    _collect_recursive(apm_dir / "skills", "skills", components)

    # prompts/ -> commands/ (rename .prompt.md -> .md)
    _collect_recursive(apm_dir / "prompts", "commands", components, rename=_rename_prompt)

    # instructions/ -> instructions/
    _collect_recursive(apm_dir / "instructions", "instructions", components)

    # commands/ -> commands/
    _collect_recursive(apm_dir / "commands", "commands", components)

    return components


def collect_root_plugin_components(project_root: Path) -> list[tuple[Path, str]]:
    """Collect plugin-native components authored at root level.

    Packages that already follow the plugin directory convention (``agents/``,
    ``skills/``, etc. at the repo root) have their files picked up here.
    """
    components: list[tuple[Path, str]] = []
    for dir_name in ("agents", "skills", "commands", "instructions"):
        _collect_recursive(project_root / dir_name, dir_name, components)
    return components


def collect_bare_skill(
    install_path: Path,
    dep: "LockedDependency",
    out: list[tuple[Path, str]],
) -> None:
    """Detect a bare Claude skill (SKILL.md at dep root, no skills/ subdir).

    Bare skills are packages consisting of just ``SKILL.md`` + supporting files
    at the package root.  They have no ``.apm/`` directory or ``skills/``
    subdirectory, so the normal collectors miss them.  Map the entire package
    into ``skills/{name}/`` so the plugin host can discover it.
    """
    skill_md = install_path / "SKILL.md"
    if not skill_md.is_file():
        return
    # Already collected via .apm/skills/ or root skills/ — skip
    if any(rel.startswith("skills/") for _, rel in out):
        return
    # Derive a slug: prefer virtual_path (e.g. "frontend-design"), else last
    # segment of repo_url (e.g. "my-skill" from "owner/my-skill")
    slug = _normalize_bare_skill_slug(getattr(dep, "virtual_path", "") or "")
    if not slug:
        slug = dep.repo_url.rsplit("/", 1)[-1] if dep.repo_url else "skill"
    for f in sorted(install_path.iterdir()):
        if (
            f.is_file()
            and not f.is_symlink()
            and f.name
            not in (
                "apm.yml",
                "apm.lock.yaml",
                "plugin.json",
            )
        ):
            out.append((f, f"skills/{slug}/{f.name}"))


def collect_hooks_from_apm(apm_dir: Path) -> dict:
    """Return merged hooks from ``.apm/hooks/*.json``."""
    from .hooks_mcp import deep_merge

    hooks: dict = {}
    hooks_dir = apm_dir / "hooks"
    if not hooks_dir.is_dir():
        return hooks
    for f in sorted(hooks_dir.iterdir()):
        if f.is_file() and f.suffix == ".json" and not f.is_symlink():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    deep_merge(hooks, data, overwrite=False)
            except (json.JSONDecodeError, OSError):
                pass
    return hooks


def collect_hooks_from_root(package_root: Path) -> dict:
    """Return hooks from a root-level ``hooks.json`` or ``hooks/`` directory."""
    from .hooks_mcp import deep_merge

    hooks: dict = {}
    # Single file
    hooks_file = package_root / "hooks.json"
    if hooks_file.is_file() and not hooks_file.is_symlink():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                deep_merge(hooks, data, overwrite=False)
        except (json.JSONDecodeError, OSError):
            pass
    # Directory
    hooks_dir = package_root / "hooks"
    if hooks_dir.is_dir():
        for f in sorted(hooks_dir.iterdir()):
            if f.is_file() and f.suffix == ".json" and not f.is_symlink():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        deep_merge(hooks, data, overwrite=False)
                except (json.JSONDecodeError, OSError):
                    pass
    return hooks


def collect_mcp(package_root: Path) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json``."""
    mcp_file = package_root / ".mcp.json"
    if not mcp_file.is_file() or mcp_file.is_symlink():
        return {}
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            servers = data.get("mcpServers", {})
            return dict(servers) if isinstance(servers, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}
