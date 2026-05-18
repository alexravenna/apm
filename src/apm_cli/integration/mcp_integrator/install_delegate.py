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


def install(mcp_deps: list, opts) -> int:
    """Install MCP dependencies.

    Args:
        mcp_deps: List of MCP dependency entries (registry strings or
            MCPDependency objects).
        opts: :class:`MCPInstallOpts` containing all optional parameters.

    Returns:
        Number of MCP servers newly configured or updated.
    """
    from apm_cli.integration.mcp_integrator_install import run_mcp_install

    return run_mcp_install(mcp_deps, opts)
