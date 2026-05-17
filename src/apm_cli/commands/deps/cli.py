"""APM dependency management CLI commands."""

import sys
from pathlib import Path

import click

# Import existing APM components
from ...constants import APM_MODULES_DIR
from ...core.command_logger import CommandLogger
from ...core.target_detection import TargetParamType
from ...models.apm_package import APMPackage as APMPackage

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_primitive_counts(primitives):
    """Format primitive type counts into a comma-separated summary string."""
    parts = []
    for ptype, count in primitives.items():
        if count > 0:
            parts.append(f"{count} {ptype}")
    return ", ".join(parts)


def _deps_list_source_label(
    host: str | None,
    *,
    is_local: bool = False,
    lockfile_source: str | None = None,
) -> str:
    """Map host / local flags to the ``apm deps list`` Source column."""
    from ...utils.github_host import is_azure_devops_hostname, is_gitlab_hostname

    if is_local or lockfile_source == "local":
        return "local"
    if host and is_azure_devops_hostname(host):
        return "azure-devops"
    if host and is_gitlab_hostname(host):
        return "gitlab"
    return "github"


def _dep_display_name(dep) -> str:
    """Get display name for a locked dependency (key@version)."""
    key = dep.get_unique_key()
    version = (
        dep.version
        or (dep.resolved_commit[:7] if dep.resolved_commit else None)
        or dep.resolved_ref
        or "latest"
    )
    return f"{key}@{version}"


def _add_tree_children(parent_branch, parent_repo_url, children_map, has_rich, depth=0):
    """Recursively add transitive deps as nested children of a tree node."""
    kids = children_map.get(parent_repo_url, [])
    for child_dep in kids:
        child_name = _dep_display_name(child_dep)
        child_branch = parent_branch.add(f"[dim]{child_name}[/dim]") if has_rich else child_name
        if depth < 5:  # Prevent infinite recursion
            _add_tree_children(child_branch, child_dep.repo_url, children_map, has_rich, depth + 1)


# ---------------------------------------------------------------------------
# Data resolution — deps list
# ---------------------------------------------------------------------------


def _resolve_scope_deps(apm_dir, logger, insecure_only=False):
    return _deps_sections._resolve_scope_deps(apm_dir, logger, insecure_only)


@click.group(help="Manage APM package dependencies")
def deps():
    """APM dependency management commands."""
    pass


def _show_scope_deps(scope_label, apm_dir, logger, console, has_rich, insecure_only=False):
    return _deps_sections._show_scope_deps(
        scope_label, apm_dir, logger, console, has_rich, insecure_only
    )


@deps.command(name="list", help="List installed APM dependencies")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="List user-scope dependencies (~/.apm/) instead of project",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show both project and user-scope dependencies",
)
@click.option(
    "--insecure",
    "insecure_only",
    is_flag=True,
    default=False,
    help="Show only installed dependencies locked to http:// sources",
)
def list_packages(global_, show_all, insecure_only):
    """Show all installed APM dependencies with context files and agent workflows."""
    logger = CommandLogger("deps-list")

    try:
        # Import Rich components with fallback
        import shutil

        from rich.console import Console

        term_width = shutil.get_terminal_size((120, 24)).columns
        console = Console(width=max(120, term_width))
        has_rich = True
    except ImportError:
        has_rich = False
        console = None

    try:
        from ...core.scope import InstallScope, get_apm_dir

        if show_all:
            # Show both scopes
            _show_scope_deps(
                "Project",
                get_apm_dir(InstallScope.PROJECT),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
            if console and has_rich:
                console.print()  # spacing between tables
            _show_scope_deps(
                "Global",
                get_apm_dir(InstallScope.USER),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
        elif global_:
            _show_scope_deps(
                "Global",
                get_apm_dir(InstallScope.USER),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
        else:
            _show_scope_deps(
                "Project",
                get_apm_dir(InstallScope.PROJECT),
                logger,
                console,
                has_rich,
                insecure_only=insecure_only,
            )
    except Exception as e:
        logger.error(f"Error listing dependencies: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data resolution — deps tree
# ---------------------------------------------------------------------------


def _build_dep_tree(apm_dir):
    return _deps_sections._build_dep_tree(apm_dir)


@deps.command(help="Show dependency tree structure")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Show user-scope dependency tree (~/.apm/)",
)
def tree(global_):
    return _deps_sections.tree(global_)


@deps.command(help="Remove all APM dependencies")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show what would be removed without removing"
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
def clean(dry_run: bool, yes: bool):
    return _deps_sections.clean(dry_run, yes)


@deps.command(help="Update APM dependencies to latest refs")
@click.argument("packages", nargs=-1)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed update information")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite locally-authored files on collision",
)
@click.option(
    "--target",
    "-t",
    type=TargetParamType(),
    default=None,
    help="Target platform (comma-separated). Values: copilot, claude, cursor, opencode, codex, gemini, windsurf, agent-skills, all. 'agent-skills' deploys to .agents/skills/ (cross-client). 'all' = copilot+claude+cursor+opencode+codex+gemini+windsurf (excludes agent-skills); combine with 'agent-skills' for both. 'copilot-cowork' is also accepted when the copilot-cowork experimental flag is enabled (run 'apm experimental enable copilot-cowork').",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Update user-scope dependencies (~/.apm/)",
)
@click.option(
    "--legacy-skill-paths",
    "legacy_skill_paths",
    is_flag=True,
    default=False,
    help=(
        "Deploy skill files to per-client paths (e.g. .cursor/skills/) instead of "
        "the shared .agents/skills/ directory. Compatibility flag for projects that "
        "need per-client skill layouts."
    ),
)
def update(packages, verbose, force, target, parallel_downloads, global_, legacy_skill_paths):
    return _deps_sections.update(
        packages, verbose, force, target, parallel_downloads, global_, legacy_skill_paths
    )


@deps.command(help="Show detailed package information")
@click.argument("package", required=True)
def info(package: str):
    """Show detailed information about a specific package including context files and workflows."""
    from ..view import display_package_info, resolve_package_path

    logger = CommandLogger("deps-info")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.error("No apm_modules/ directory found")
        logger.progress("Run 'apm install' to install dependencies first")
        sys.exit(1)

    package_path = resolve_package_path(package, apm_modules_path, logger)
    display_package_info(package, package_path, logger)


from . import deps_sections as _deps_sections
