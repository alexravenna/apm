"""APM install command and dependency installation engine."""

import builtins
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.plan import UpdatePlan

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.

# Re-export MCP add/build helpers under their underscore-prefixed legacy
# names. Aliases live in mcp/writer.py and mcp/entry.py respectively.

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.

# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".

# Re-export DI-seam helpers from the install services module so that test
# patches against ``apm_cli.commands.install._integrate_*`` keep working.

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.


# MCP --mcp helpers (module-level re-exports for test patches); must stay at
# import time per comments in the original mid-file block.

# ---------------------------------------------------------------------------
# Manifest snapshot + rollback (W2-pkg-rollback, #827)
# ---------------------------------------------------------------------------
# When the user runs ``apm install <pkg>``, ``_validate_and_add_packages_to_apm_yml``
# mutates ``apm.yml`` BEFORE the install pipeline runs.  If the pipeline fails
# (policy block, download error, etc.) the failed package would stay in
# ``apm.yml`` forever.  These helpers snapshot the raw bytes before mutation
# and atomically restore on failure.
# ---------------------------------------------------------------------------


# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclasses.dataclass
class InstallContext:
    """Bundles install command state to reduce function signatures.

    Created by :func:`install` after argument parsing and scope resolution,
    then threaded through :func:`_install_apm_packages` and
    :func:`_post_install_summary` to avoid long parameter lists.
    """

    scope: Any  # InstallScope
    manifest_path: "Path"
    manifest_display: str
    apm_dir: "Path"
    project_root: "Path"
    logger: Any  # InstallLogger
    auth_resolver: Any  # AuthResolver
    verbose: bool
    force: bool
    dry_run: bool
    update: bool
    dev: bool
    runtime: str | None
    exclude: str | None
    target: str | None
    parallel_downloads: int
    allow_insecure: bool
    allow_insecure_hosts: tuple
    protocol_pref: Any  # ProtocolPreference
    allow_protocol_fallback: bool
    trust_transitive_mcp: bool
    no_policy: bool
    install_mode: Any  # InstallMode
    packages: tuple  # Original Click packages
    refresh: bool = False
    only_packages: builtins.list | None = None
    manifest_snapshot: bytes | None = None
    snapshot_manifest_path: "Path | None" = None
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: "Callable[[UpdatePlan], bool] | None" = None
