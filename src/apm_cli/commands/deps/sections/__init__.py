"""APM dependency management CLI commands."""

import shutil
import sys
from pathlib import Path

import click

from ....constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ....core.command_logger import CommandLogger
from ....models.apm_package import APMPackage
from ..._helpers import _expand_with_ancestors, _standalone_installed_packages
from .._utils import (
    _add_tree_children,
    _count_primitives,
    _dep_display_name,
    _deps_list_source_label,
    _format_primitive_counts,
    _get_package_display_info,
    _is_nested_under_package,
)

# Re-export complex functions from helper modules
from .scope_resolver import _resolve_scope_deps
from .update_engine import update


def _show_scope_deps(scope_label, apm_dir, logger, console, has_rich, insecure_only=False):
    """Display dependencies for a single scope (Project or Global)."""
    installed_packages, orphaned_packages = _resolve_scope_deps(apm_dir, logger, insecure_only)

    if installed_packages is None:
        logger.progress(f"No APM dependencies installed ({scope_label} scope)")
        logger.verbose_detail("Run 'apm install' to install dependencies from apm.yml")
        return

    if not installed_packages:
        if insecure_only:
            logger.progress(f"No insecure APM dependencies installed ({scope_label} scope)")
        else:
            logger.progress(
                f"apm_modules/ directory exists but contains no valid packages ({scope_label} scope)"
            )
        return

    # Display packages in table format
    if has_rich:
        from rich.table import Table

        table = Table(
            title=(
                f" Insecure APM Dependencies ({scope_label})"
                if insecure_only
                else f" APM Dependencies ({scope_label})"
            ),
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Package", style="bold white")
        table.add_column("Version", style="yellow")
        table.add_column("Source", style="blue")
        if insecure_only:
            table.add_column("Origin", style="bold red")
        table.add_column("Prompts", style="magenta", justify="center")
        table.add_column("Instructions", style="green", justify="center")
        table.add_column("Agents", style="cyan", justify="center")
        table.add_column("Skills", style="yellow", justify="center")
        table.add_column("Hooks", style="red", justify="center")

        for pkg in installed_packages:
            p = pkg["primitives"]
            table.add_row(
                pkg["name"],
                pkg["version"],
                pkg["source"],
                *([pkg["insecure_via"]] if insecure_only else []),
                str(p.get("prompts", 0)) if p.get("prompts", 0) > 0 else "-",
                str(p.get("instructions", 0)) if p.get("instructions", 0) > 0 else "-",
                str(p.get("agents", 0)) if p.get("agents", 0) > 0 else "-",
                str(p.get("skills", 0)) if p.get("skills", 0) > 0 else "-",
                str(p.get("hooks", 0)) if p.get("hooks", 0) > 0 else "-",
            )

        console.print(table)

        # Show orphaned packages warning -- routed through CommandLogger
        # so output goes through the central STATUS_SYMBOLS prefix path
        # (no raw `[!]` literal that Rich would parse as markup) and so
        # behaviour is consistent with prune.py.
        if orphaned_packages:
            logger.warning(f"{len(orphaned_packages)} orphaned package(s) found (not in apm.yml):")
            for pkg in orphaned_packages:
                logger.warning(f"  - {pkg}")
            logger.info("Run 'apm prune' to remove orphaned packages")
    else:
        # Fallback text table
        if insecure_only:
            click.echo(f" Insecure APM Dependencies ({scope_label}):")
            click.echo(
                f"{'Package':<30} {'Version':<10} {'Source':<12} {'Origin':<18} "
                f"{'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
            )
            click.echo("-" * 117)
        else:
            click.echo(f" APM Dependencies ({scope_label}):")
            click.echo(
                f"{'Package':<30} {'Version':<10} {'Source':<12} {'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
            )
            click.echo("-" * 98)

        for pkg in installed_packages:
            p = pkg["primitives"]
            name = pkg["name"][:28]
            version = pkg["version"][:8]
            source = pkg["source"][:10]
            insecure_via = pkg["insecure_via"][:16]
            prompts = str(p.get("prompts", 0)) if p.get("prompts", 0) > 0 else "-"
            instructions = str(p.get("instructions", 0)) if p.get("instructions", 0) > 0 else "-"
            agents = str(p.get("agents", 0)) if p.get("agents", 0) > 0 else "-"
            skills = str(p.get("skills", 0)) if p.get("skills", 0) > 0 else "-"
            hooks = str(p.get("hooks", 0)) if p.get("hooks", 0) > 0 else "-"
            if insecure_only:
                click.echo(
                    f"{name:<30} {version:<10} {source:<12} {insecure_via:<18} "
                    f"{prompts:>7} {instructions:>7} {agents:>7} {skills:>7} {hooks:>7}"
                )
            else:
                click.echo(
                    f"{name:<30} {version:<10} {source:<12} {prompts:>7} {instructions:>7} {agents:>7} {skills:>7} {hooks:>7}"
                )

        # Show orphaned packages warning -- route through CommandLogger
        # for consistency with the rich branch above and with prune.py.
        if orphaned_packages:
            logger.warning(f"{len(orphaned_packages)} orphaned package(s) found (not in apm.yml):")
            for pkg in orphaned_packages:
                logger.warning(f"  - {pkg}")
            logger.info("Run 'apm prune' to remove orphaned packages")


def _build_dep_tree(apm_dir):
    """Build dependency tree data from lockfile or directory scan.

    Returns a dict describing the tree structure::

        {
            'project_name': str,
            'apm_modules_path': Path,
            'source': 'lockfile' | 'directory',
            'direct': [dep, ...],           # lockfile mode only
            'children_map': {url: [dep]},   # lockfile mode only
            'scanned_packages': [{...}],    # directory fallback only
            'has_modules': bool,
        }
    """
    apm_modules_path = apm_dir / APM_MODULES_DIR

    # Load project info
    project_name = "my-project"
    try:
        apm_yml_path = apm_dir / APM_YML_FILENAME
        if apm_yml_path.exists():
            root_package = APMPackage.from_apm_yml(apm_yml_path)
            project_name = root_package.name
    except Exception:
        pass

    result = {
        "project_name": project_name,
        "apm_modules_path": apm_modules_path,
        "source": "directory",
        "direct": [],
        "children_map": {},
        "scanned_packages": [],
        "has_modules": apm_modules_path.exists(),
    }

    # Try to load lockfile for accurate tree with depth/parent info
    try:
        from ....deps.lockfile import LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(apm_dir)
        if lockfile_path.exists():
            lockfile = LockFile.read(lockfile_path)
            if lockfile:
                lockfile_deps = lockfile.get_package_dependencies()
                if lockfile_deps:
                    result["source"] = "lockfile"
                    result["direct"] = [d for d in lockfile_deps if d.depth <= 1]
                    transitive = [d for d in lockfile_deps if d.depth > 1]
                    children_map: dict[str, list] = {}
                    for dep in transitive:
                        parent_key = dep.resolved_by or ""
                        if parent_key not in children_map:
                            children_map[parent_key] = []
                        children_map[parent_key].append(dep)
                    result["children_map"] = children_map
                    return result
    except Exception:
        pass

    # Fallback: scan apm_modules directory (no lockfile)
    if not apm_modules_path.exists():
        return result

    scanned = []
    for candidate in sorted(apm_modules_path.rglob("*")):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        has_apm = (candidate / APM_YML_FILENAME).exists()
        has_skill = (candidate / SKILL_MD_FILENAME).exists()
        if not has_apm and not has_skill:
            continue
        rel_parts = candidate.relative_to(apm_modules_path).parts
        if len(rel_parts) < 2:
            continue
        if ".apm" in rel_parts:
            continue
        if has_skill and not has_apm and _is_nested_under_package(candidate, apm_modules_path):
            continue
        info = _get_package_display_info(candidate)
        primitives = _count_primitives(candidate)
        scanned.append(
            {
                "display_name": info["display_name"],
                "primitives": primitives,
            }
        )
    result["scanned_packages"] = scanned
    return result


def tree(global_):
    """Display dependencies in hierarchical tree format using lockfile."""
    logger = CommandLogger("deps-tree")

    try:
        # Import Rich components with fallback
        from rich.console import Console
        from rich.tree import Tree

        console = Console()
        has_rich = True
    except ImportError:
        has_rich = False
        console = None

    try:
        from ....core.scope import InstallScope, get_apm_dir

        scope = InstallScope.USER if global_ else InstallScope.PROJECT
        apm_dir = get_apm_dir(scope)

        tree_data = _build_dep_tree(apm_dir)
        project_name = tree_data["project_name"]
        apm_modules_path = tree_data["apm_modules_path"]

        if tree_data["source"] == "lockfile":
            direct = tree_data["direct"]
            children_map = tree_data["children_map"]

            if has_rich:
                root_tree = Tree(f"[bold cyan]{project_name}[/bold cyan] (local)")
                if not direct:
                    root_tree.add("[dim]No dependencies installed[/dim]")
                else:
                    for dep in direct:
                        display = _dep_display_name(dep)
                        install_key = dep.get_unique_key()
                        install_path = apm_modules_path / install_key
                        branch = root_tree.add(f"[green]{display}[/green]")
                        if install_path.exists():
                            prim_summary = _format_primitive_counts(_count_primitives(install_path))
                            if prim_summary:
                                branch.add(f"[dim]{prim_summary}[/dim]")
                        _add_tree_children(branch, dep.repo_url, children_map, has_rich)
                console.print(root_tree)
            else:
                click.echo(f"{project_name} (local)")
                if not direct:
                    click.echo("+-- No dependencies installed")
                else:
                    for i, dep in enumerate(direct):
                        is_last = i == len(direct) - 1
                        prefix = "+-- " if is_last else "|-- "
                        display = _dep_display_name(dep)
                        click.echo(f"{prefix}{display}")
                        # Show transitive deps
                        kids = children_map.get(dep.repo_url, [])
                        sub_prefix = "    " if is_last else "|   "
                        for j, child in enumerate(kids):
                            child_is_last = j == len(kids) - 1
                            child_prefix = "+-- " if child_is_last else "|-- "
                            click.echo(f"{sub_prefix}{child_prefix}{_dep_display_name(child)}")
        # Fallback: scan apm_modules directory (no lockfile)
        elif has_rich:
            root_tree = Tree(f"[bold cyan]{project_name}[/bold cyan] (local)")
            if not tree_data["has_modules"]:
                root_tree.add("[dim]No dependencies installed[/dim]")
            else:
                for pkg in tree_data["scanned_packages"]:
                    branch = root_tree.add(f"[green]{pkg['display_name']}[/green]")
                    prim_summary = _format_primitive_counts(pkg["primitives"])
                    if prim_summary:
                        branch.add(f"[dim]{prim_summary}[/dim]")
            console.print(root_tree)
        else:
            click.echo(f"{project_name} (local)")
            if not tree_data["has_modules"]:
                click.echo("+-- No dependencies installed")

    except Exception as e:
        logger.error(f"Error showing dependency tree: {e}")
        sys.exit(1)


def clean(dry_run: bool, yes: bool):
    """Remove entire apm_modules/ directory."""
    logger = CommandLogger("deps-clean")

    project_root = Path(".")
    apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.progress("No apm_modules/ directory found - already clean")
        return

    # Count actual installed packages (not just top-level dirs like org namespaces or _local)
    from .._utils import _scan_installed_packages

    packages = _scan_installed_packages(apm_modules_path)
    package_count = len(packages)

    if dry_run:
        logger.progress(f"Dry run: would remove apm_modules/ ({package_count} package(s))")
        for pkg in sorted(packages):
            logger.progress(f"  - {pkg}")
        return

    logger.warning(
        f"This will remove the entire apm_modules/ directory ({package_count} package(s))"
    )

    # Confirmation prompt (skip if --yes provided)
    if not yes:
        try:
            from rich.prompt import Confirm

            confirm = Confirm.ask("Continue?")
        except ImportError:
            confirm = click.confirm("Continue?")

        if not confirm:
            logger.progress("Operation cancelled")
            return

    try:
        shutil.rmtree(apm_modules_path)
        logger.success("Successfully removed apm_modules/ directory")
    except Exception as e:
        logger.error(f"Error removing apm_modules/: {e}")
        sys.exit(1)


__all__ = [
    "_build_dep_tree",
    "_resolve_scope_deps",
    "_show_scope_deps",
    "clean",
    "tree",
    "update",
]
