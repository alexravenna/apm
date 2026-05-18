"""APM prune command."""

import sys
from pathlib import Path

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..utils.path_security import safe_rmtree
from . import prune_helpers as _prune_helpers
from .prune_helpers import (
    _cleanup_pruned_lockfile,
    _load_prune_state,
    _remove_orphaned_packages,
    _render_orphaned_packages,
)


@click.command(help="Remove APM packages not listed in apm.yml")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.pass_context
def prune(ctx, dry_run):
    """Remove installed APM packages that are not listed in apm.yml (like npm prune)."""
    logger = CommandLogger("prune", dry_run=dry_run)
    try:
        if not Path(APM_YML_FILENAME).exists():
            logger.error("No apm.yml found. Run 'apm init' first.")
            sys.exit(1)

        apm_modules_dir = Path(APM_MODULES_DIR)
        if not apm_modules_dir.exists():
            logger.progress("No apm_modules/ directory found. Nothing to prune.")
            return

        logger.start("Analyzing installed packages vs apm.yml...")
        _lockfile, orphaned_packages = _load_prune_state(apm_modules_dir, logger)
        if not orphaned_packages:
            logger.success("No orphaned packages found. apm_modules/ is clean.", symbol="check")
            return

        _render_orphaned_packages(orphaned_packages, dry_run, logger)
        _prune_helpers.safe_rmtree = safe_rmtree
        if dry_run:
            logger.success("Dry run complete - no changes made")
            return

        removed_count, pruned_keys, deleted_pkg_paths = _remove_orphaned_packages(
            orphaned_packages,
            apm_modules_dir,
            logger,
        )
        from ..integration.base_integrator import BaseIntegrator

        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)
        _cleanup_pruned_lockfile(pruned_keys, logger)
        if removed_count > 0:
            logger.success(f"Pruned {removed_count} orphaned package(s)")
        else:
            logger.warning("No packages were removed")
    except Exception as e:
        logger.error(f"Error pruning packages: {e}")
        sys.exit(1)
