"""Dataclass parameter objects for the MCP install orchestration chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope


@dataclass
class MCPInstallOpts:
    """Bundled optional arguments for MCP install functions.

    Passed through the call chain:
    ``MCPIntegrator.install`` → ``install_delegate.install``
    → ``run_mcp_install`` → ``_resolve_runtimes``.
    """

    runtime: str | None = None
    exclude: str | None = None
    verbose: bool = False
    apm_config: dict | None = None
    stored_mcp_configs: dict | None = None
    project_root: Any = None
    user_scope: bool = False
    explicit_target: str | None = None
    logger: Any = None
    diagnostics: Any = None
    scope: Any = None  # InstallScope | None


@dataclass
class _ResolveRuntimesOpts:
    """Bundled arguments for :func:`_resolve_runtimes`."""

    runtime: str | None
    exclude: str | None
    verbose: bool
    apm_config: dict | None
    project_root: Any
    user_scope: bool
    explicit_target: str | None
    scope: Any  # InstallScope | None
    logger: Any
    console: Any
    mcp_integrator_cls: Any
    is_vscode_available: Any
