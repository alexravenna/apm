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

from apm_cli.deps.lockfile import LockFile, get_lockfile_path

_log = logging.getLogger(__name__)


def update_lockfile(
    mcp_server_names: builtins.set,
    lock_path: Path | None = None,
    *,
    mcp_configs: builtins.dict | None = None,
) -> None:
    """Update the lockfile with the current set of APM-managed MCP server names.

    Accepts the lock path directly to avoid a redundant disk read when the
    caller already has it.

    Args:
        mcp_server_names: Set of MCP server names to persist.
        lock_path: Path to the lockfile.  Defaults to ``apm.lock.yaml`` in CWD.
        mcp_configs: Keyword-only.  When provided, overwrites ``mcp_configs``
                     in the lockfile (used for drift-detection baseline).
    """
    if lock_path is None:
        lock_path = get_lockfile_path(Path.cwd())
    if not lock_path.exists():
        return
    try:
        lockfile = LockFile.read(lock_path)
        if lockfile is None:
            return
        lockfile.mcp_servers = sorted(mcp_server_names)
        if mcp_configs is not None:
            lockfile.mcp_configs = mcp_configs
        lockfile.save(lock_path)
    except Exception:
        _log.debug(
            "Failed to update MCP servers in lockfile at %s",
            lock_path,
            exc_info=True,
        )
