"""Standalone MCP lifecycle orchestrator.

Owns all MCP dependency resolution, installation, stale cleanup, and lockfile
persistence logic.  This is NOT a BaseIntegrator subclass  -- MCP integration is
config-level orchestration (registry APIs, runtime configs, lockfile tracking),
not file-level deployment (copy/collision/sync).

The existing adapters (client/, package_manager/) and registry operations
(registry/operations.py) are *used* by this class, not modified.
"""

import logging

_log = logging.getLogger(__name__)


def install(
    mcp_deps: list,
    runtime: str | None = None,  # noqa: RUF013
    exclude: str | None = None,  # noqa: RUF013
    verbose: bool = False,
    apm_config: dict | None = None,  # noqa: RUF013
    stored_mcp_configs: dict | None = None,  # noqa: RUF013
    project_root=None,
    user_scope: bool = False,
    explicit_target: str | None = None,
    logger=None,
    diagnostics=None,
    scope=None,
) -> int:
    """Install MCP dependencies.

    Args:
        mcp_deps: List of MCP dependency entries (registry strings or
            MCPDependency objects).
        runtime: Target specific runtime only.
        exclude: Exclude specific runtime from installation.
        verbose: Show detailed installation information.
        apm_config: The parsed apm.yml configuration dict (optional).
            When not provided, the method loads it from disk.
        stored_mcp_configs: Previously stored MCP configs from lockfile
            for diff-aware installation.  When provided, servers whose
            manifest config has changed are re-applied automatically.
        project_root: Project root for repo-local runtime configs.
        user_scope: Whether runtime configuration is being resolved at user scope.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: InstallScope (PROJECT or USER). When USER, only
            runtimes whose adapter declares ``supports_user_scope``
            are targeted; workspace-only runtimes are skipped.

    Returns:
        Number of MCP servers newly configured or updated.
    """
    from apm_cli.integration.mcp_integrator_install import run_mcp_install

    return run_mcp_install(
        mcp_deps,
        runtime=runtime,
        exclude=exclude,
        verbose=verbose,
        apm_config=apm_config,
        stored_mcp_configs=stored_mcp_configs,
        project_root=project_root,
        user_scope=user_scope,
        explicit_target=explicit_target,
        logger=logger,
        diagnostics=diagnostics,
        scope=scope,
    )
