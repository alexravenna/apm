"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import builtins
import logging
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.lockfile import LockFile

_log = logging.getLogger(__name__)


def collect_transitive(
    apm_modules_dir: Path,
    lock_path: Path | None = None,
    trust_private: bool = False,
    logger=None,
    diagnostics=None,
) -> list:
    """Collect MCP dependencies from resolved APM packages listed in apm.lock.

    Only scans apm.yml files for packages present in apm.lock to avoid
    picking up stale/orphaned packages from previous installs.
    Falls back to scanning all apm.yml files if no lock file is available.

    Self-defined servers (registry: false) from direct dependencies
    (depth == 1) are auto-trusted.  Self-defined servers from transitive
    dependencies (depth > 1) are skipped with a warning unless
    *trust_private* is True.
    """
    if logger is None:
        logger = NullCommandLogger()
    if not apm_modules_dir.exists():
        return []

    from apm_cli.models.apm_package import APMPackage

    # Build set of expected apm.yml paths from apm.lock
    locked_paths = None
    direct_paths: builtins.set = builtins.set()
    lockfile = None
    if lock_path and lock_path.exists():
        lockfile = LockFile.read(lock_path)
        if lockfile is not None:
            locked_paths = builtins.set()
            for dep in lockfile.get_package_dependencies():
                if dep.repo_url:
                    yml = (
                        apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml"
                        if dep.virtual_path
                        else apm_modules_dir / dep.repo_url / "apm.yml"
                    )
                    locked_paths.add(yml.resolve())
                    if dep.depth == 1:
                        direct_paths.add(yml.resolve())

    # Prefer iterating lock-derived paths directly (existing files only).
    # Fall back to full scan only when lock parsing is unavailable.
    if locked_paths is not None:
        apm_yml_paths = [path for path in sorted(locked_paths) if path.exists()]
    else:
        apm_yml_paths = apm_modules_dir.rglob("apm.yml")

    collected = []
    for apm_yml_path in apm_yml_paths:
        try:
            pkg = APMPackage.from_apm_yml(apm_yml_path)
            mcp = pkg.get_mcp_dependencies()
            if mcp:
                is_direct = apm_yml_path.resolve() in direct_paths
                for dep in mcp:
                    if hasattr(dep, "is_self_defined") and dep.is_self_defined:
                        if is_direct:
                            logger.progress(
                                f"Trusting direct dependency MCP '{dep.name}' from '{pkg.name}'"
                            )
                        elif trust_private:
                            logger.progress(
                                f"Trusting self-defined MCP server '{dep.name}' "
                                f"from transitive package '{pkg.name}' (--trust-transitive-mcp)"
                            )
                        else:
                            _trust_msg = (
                                f"Transitive package '{pkg.name}' declares self-defined "
                                f"MCP server '{dep.name}' (registry: false). "
                                f"Re-declare it in your apm.yml or use --trust-transitive-mcp."
                            )
                            if diagnostics:
                                diagnostics.warn(_trust_msg)
                            else:
                                logger.warning(_trust_msg)
                            continue
                    collected.append(dep)
        except Exception:
            _log.debug(
                "Skipping package at %s: failed to parse apm.yml",
                apm_yml_path,
                exc_info=True,
            )
            continue
    return collected


def deduplicate(deps: list) -> list:
    """Deduplicate MCP dependencies by name; first occurrence wins.

    Root deps are listed before transitive, so root overlays take
    precedence.
    """
    seen_names: builtins.set = builtins.set()
    result = []
    for dep in deps:
        if hasattr(dep, "name"):
            name = dep.name
        elif isinstance(dep, dict):
            name = dep.get("name", "")
        else:
            name = str(dep)
        if not name:
            if dep not in result:
                result.append(dep)
            continue
        if name not in seen_names:
            seen_names.add(name)
            result.append(dep)
    return result
