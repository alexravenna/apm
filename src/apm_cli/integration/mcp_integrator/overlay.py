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
import warnings

_log = logging.getLogger(__name__)


def _build_self_defined_info(dep) -> dict:
    """Build a synthetic server_info dict from a self-defined MCPDependency.

    Mimics the structure returned by the MCP registry so that existing
    adapter code can consume self-defined deps without changes.
    """
    info: dict = {"name": dep.name}

    # For stdio self-defined deps, store raw command/args so adapters
    # can bypass registry-specific formatting (npm, docker, etc.).
    if dep.transport == "stdio" or (
        dep.transport not in ("http", "sse", "streamable-http") and dep.command
    ):
        info["_raw_stdio"] = {
            "command": dep.command or dep.name,
            "args": list(dep.args) if dep.args else [],
            "env": dict(dep.env) if dep.env else {},
        }

    if dep.transport in ("http", "sse", "streamable-http"):
        # Build as a remote endpoint
        remote = {
            "transport_type": dep.transport,
            "url": dep.url or "",
        }
        if dep.headers:
            remote["headers"] = [{"name": k, "value": v} for k, v in dep.headers.items()]
        info["remotes"] = [remote]
    else:
        # Build as a stdio package
        env_vars = []
        if dep.env:
            env_vars = [{"name": k, "description": "", "required": True} for k in dep.env]

        runtime_args = []
        if dep.args:
            if isinstance(dep.args, builtins.list):
                runtime_args = [{"is_required": True, "value_hint": a} for a in dep.args]
            elif isinstance(dep.args, builtins.dict):
                runtime_args = [{"is_required": True, "value_hint": v} for v in dep.args.values()]

        info["packages"] = [
            {
                "runtime_hint": dep.command or dep.name,
                "name": dep.name,
                "registry_name": "self-defined",
                "runtime_arguments": runtime_args,
                "package_arguments": [],
                "environment_variables": env_vars,
            }
        ]

    # Embed tools override for adapters to pick up
    if dep.tools:
        info["_apm_tools_override"] = dep.tools

    return info


def _apply_overlay(server_info_cache: dict, dep) -> None:
    """Apply MCPDependency overlay fields onto cached server_info (in-place).

    Modifies the server_info dict in *server_info_cache[dep.name]* to
    reflect overlay preferences (transport selection, env, headers, tools).
    """
    info = server_info_cache.get(dep.name)
    if not info:
        return

    # Transport overlay: select matching transport from available options
    if dep.transport:
        if dep.transport in ("http", "sse", "streamable-http"):
            # User prefers remote transport  -- remove packages to force remote path
            if info.get("remotes"):
                info.pop("packages", None)
        elif dep.transport == "stdio":
            # User prefers stdio  -- remove remotes to force package path
            if info.get("packages"):
                info.pop("remotes", None)

    # Package type overlay: select specific package registry (npm, pypi, oci)
    if dep.package and "packages" in info:
        filtered = [
            p for p in info["packages"] if p.get("registry_name", "").lower() == dep.package.lower()
        ]
        if filtered:
            info["packages"] = filtered

    # Headers overlay: merge into remote headers
    if dep.headers and "remotes" in info:
        for remote in info["remotes"]:
            existing_headers = remote.get("headers", [])
            if isinstance(existing_headers, builtins.list):
                for k, v in dep.headers.items():
                    existing_headers.append({"name": k, "value": v})
                remote["headers"] = existing_headers
            elif isinstance(existing_headers, builtins.dict):
                existing_headers.update(dep.headers)

    # Args overlay: merge into package runtime arguments
    if dep.args and "packages" in info:
        for pkg in info["packages"]:
            existing_args = pkg.get("runtime_arguments", [])
            if isinstance(dep.args, builtins.list):
                for arg in dep.args:
                    existing_args.append({"value_hint": str(arg)})
            elif isinstance(dep.args, builtins.dict):
                for k, v in dep.args.items():
                    existing_args.append({"value_hint": f"--{k}={v}"})
            pkg["runtime_arguments"] = existing_args

    # Tools overlay: embed for adapters to pick up
    if dep.tools:
        info["_apm_tools_override"] = dep.tools

    # Warn about overlay fields not yet applied at install time
    if dep.version:
        warnings.warn(
            f"MCP overlay field 'version' on '{dep.name}' is not yet applied "
            f"at install time and will be ignored.",
            stacklevel=2,
        )
    if isinstance(dep.registry, str):
        warnings.warn(
            f"MCP overlay field 'registry' on '{dep.name}' is not yet applied "
            f"at install time and will be ignored.",
            stacklevel=2,
        )
