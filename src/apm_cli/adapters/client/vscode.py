"""VSCode implementation of MCP client adapter.

This adapter implements the VSCode-specific handling of MCP server configuration,
following the official documentation at:
https://code.visualstudio.com/docs/copilot/chat/mcp-servers
"""

import json
from pathlib import Path

from ...registry.client import SimpleRegistryClient
from ...registry.integration import RegistryIntegration
from ...utils.console import _rich_warning
from ._vscode_format import (
    _LEGACY_ANGLE_VAR_RE,
    _build_package_input_vars,
    _build_python_command_args,
    _extract_package_args,
    _select_remote_with_url,
    _translate_env_vars_for_vscode,
)
from .base import _INPUT_VAR_RE, MCPClientAdapter


class VSCodeClientAdapter(MCPClientAdapter):
    """VSCode implementation of MCP client adapter.

    This adapter handles VSCode-specific configuration for MCP servers using
    a repository-level .vscode/mcp.json file, following the format specified
    in the VSCode documentation.
    """

    target_name: str = "vscode"
    mcp_servers_key: str = "servers"

    # Re-expose pure formatting helpers as staticmethods so that both
    # ``self.method(...)`` and ``VSCodeClientAdapter.method(...)`` work.
    _translate_env_vars_for_vscode = staticmethod(_translate_env_vars_for_vscode)
    _extract_package_args = staticmethod(_extract_package_args)
    _select_remote_with_url = staticmethod(_select_remote_with_url)
    _build_python_command_args = staticmethod(_build_python_command_args)
    _build_package_input_vars = staticmethod(_build_package_input_vars)

    @staticmethod
    def _warn_on_legacy_angle_vars(mapping, server_name, field):
        """Emit a warning when legacy ``<VAR>`` placeholders appear in *mapping*.

        VS Code does not resolve ``<VAR>`` placeholders, so they would render
        as literal ``<VAR>`` text in the generated mcp.json -- silently
        breaking auth headers / env values at server-start. Surface this as
        an explicit warning so authors can switch to the cross-harness
        ``${VAR}`` / ``${env:VAR}`` syntax (see manifest-schema reference).
        """
        if not mapping:
            return
        offenders = []
        for value in mapping.values():
            if isinstance(value, str):
                offenders.extend(_LEGACY_ANGLE_VAR_RE.findall(value))
        if offenders:
            unique = sorted(set(offenders))
            _rich_warning(
                f"Server '{server_name}' {field} use legacy <VAR> placeholder(s) "
                f"({', '.join('<' + n + '>' for n in unique)}) which VS Code "
                f"cannot resolve. Use ${{VAR}} or ${{env:VAR}} instead so the "
                f"value resolves at runtime."
            )

    def __init__(
        self,
        registry_url=None,
        project_root: Path | str | None = None,
        user_scope: bool = False,
    ):
        """Initialize the VSCode client adapter.

        Args:
            registry_url (str, optional): URL of the MCP registry.
                If not provided, uses the MCP_REGISTRY_URL environment variable
                or falls back to the default demo registry.
            project_root: Project root used to resolve the repository-local
                `.vscode/mcp.json` path.
            user_scope: Whether to resolve user-scope config paths instead of
                project-local paths when supported.
        """
        super().__init__(project_root=project_root, user_scope=user_scope)
        self.registry_client = SimpleRegistryClient(registry_url)
        self.registry_integration = RegistryIntegration(registry_url)

    def get_config_path(self, logger=None):
        """Get the path to the VSCode MCP configuration file in the repository.

        Returns:
            str: Path to the .vscode/mcp.json file.
        """
        # Use the resolved project root, which may be explicitly provided
        repo_root = self.project_root

        # Path to .vscode/mcp.json in the repository
        vscode_dir = repo_root / ".vscode"
        mcp_config_path = vscode_dir / "mcp.json"

        # Create the .vscode directory if it doesn't exist
        try:
            if not vscode_dir.exists():
                vscode_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if logger:
                logger.warning(f"Could not create .vscode directory: {e}")
            else:
                print(f"Warning: Could not create .vscode directory: {e}")

        return str(mcp_config_path)

    def update_config(self, new_config, logger=None):
        """Update the VSCode MCP configuration with new values.

        Args:
            new_config (dict): Complete configuration object to write.

        Returns:
            bool: True if successful, False otherwise.
        """
        config_path = self.get_config_path(logger=logger)

        try:
            # Write the updated config
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(new_config, f, indent=2)

            return True
        except Exception as e:
            if logger:
                logger.error(f"Error updating VSCode MCP configuration: {e}")
            else:
                print(f"Error updating VSCode MCP configuration: {e}")
            return False

    def get_current_config(self, logger=None):
        """Get the current VSCode MCP configuration.

        Returns:
            dict: Current VSCode MCP configuration from the local .vscode/mcp.json file.
        """
        config_path = self.get_config_path(logger=logger)

        try:
            try:
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}
        except Exception as e:
            if logger:
                logger.error(f"Error reading VSCode MCP configuration: {e}")
            else:
                print(f"Error reading VSCode MCP configuration: {e}")
            return {}

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
        logger=None,
    ):
        """Configure an MCP server in VS Code mcp.json file.

        This method updates the .vscode/mcp.json file to add or update
        an MCP server configuration.

        Args:
            server_url (str): URL or identifier of the MCP server.
            server_name (str, optional): Name of the server. Defaults to None.
            enabled (bool, optional): Whether to enable the server. Defaults to True.
            env_overrides (dict, optional): Environment variable overrides. Defaults to None.
            server_info_cache (dict, optional): Pre-fetched server info to avoid duplicate registry calls.
            logger: Optional CommandLogger for structured output.

        Returns:
            bool: True if successful, False otherwise.

        Raises:
            ValueError: If server is not found in registry.
        """
        if not server_url:
            if logger:
                logger.error("server_url cannot be empty")
            else:
                print("Error: server_url cannot be empty")
            return False

        try:
            # Use cached server info if available, otherwise fetch from registry
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                # Fallback to registry lookup if not cached
                server_info = self.registry_client.find_server_by_reference(server_url)

            # Fail if server is not found in registry - security requirement
            # This raises ValueError as expected by tests
            if not server_info:
                raise ValueError(
                    f"Failed to retrieve server details for '{server_url}'. Server not found in registry."
                )

            # Generate server configuration
            server_config, input_vars = self._format_server_config(server_info)

            if not server_config:
                if logger:
                    logger.error(f"Unable to configure server: {server_url}")
                else:
                    print(f"Unable to configure server: {server_url}")
                return False

            # Use provided server name or fallback to server_url
            config_key = server_name or server_url

            # Get current config
            current_config = self.get_current_config(logger=logger)

            # Ensure servers and inputs sections exist
            if "servers" not in current_config:
                current_config["servers"] = {}
            if "inputs" not in current_config:
                current_config["inputs"] = []

            # Add the server configuration
            current_config["servers"][config_key] = server_config

            # Add input variables (avoiding duplicates)
            existing_input_ids = {
                var.get("id") for var in current_config["inputs"] if isinstance(var, dict)
            }
            for var in input_vars:
                if var.get("id") not in existing_input_ids:
                    current_config["inputs"].append(var)
                    existing_input_ids.add(var.get("id"))

            # Update the configuration
            result = self.update_config(current_config, logger=logger)

            if result:
                if logger:
                    logger.verbose_detail(f"Configured MCP server '{config_key}' for VS Code")
                else:
                    print(f"Successfully configured MCP server '{config_key}' for VS Code")
            return result

        except ValueError:
            # Re-raise ValueError for registry errors
            raise
        except Exception as e:
            if logger:
                logger.error(f"Error configuring MCP server: {e}")
            else:
                print(f"Error configuring MCP server: {e}")
            return False

    def _format_server_config(self, server_info):
        """Format server details into VSCode mcp.json compatible format.

        Args:
            server_info (dict): Server information from registry.

        Returns:
            tuple: (server_config, input_vars) where:
                - server_config is the formatted server configuration for mcp.json
                - input_vars is a list of input variable definitions
        """
        # Self-defined stdio deps carry raw command/args  -- use directly
        raw = server_info.get("_raw_stdio")
        if raw:
            return self._format_raw_stdio_config(server_info, raw)

        # Check for packages information
        if server_info.get("packages"):
            return self._format_package_config(server_info)

        # Check for SSE endpoints or remotes
        return self._format_remote_config(server_info)

    def _format_raw_stdio_config(self, server_info, raw):
        """Format raw stdio configuration."""
        server_config = {
            "type": "stdio",
            "command": raw["command"],
            "args": raw["args"],
        }
        input_vars = []
        if raw.get("env"):
            # Translate bare ${VAR} -> ${env:VAR} so VS Code's runtime env
            # interpolation resolves them at server-start. ${input:...}
            # references are preserved for input-variable extraction below.
            self._warn_on_legacy_angle_vars(raw["env"], server_info.get("name", "unknown"), "env")
            env_translated = self._translate_env_vars_for_vscode(raw["env"])
            server_config["env"] = env_translated
            input_vars.extend(
                self._extract_input_variables(env_translated, server_info.get("name", ""))
            )
        return server_config, input_vars

    def _format_package_config(self, server_info):
        """Format package-based server configuration."""
        package = self._select_best_package(server_info["packages"])
        if package is None:
            return self._handle_incomplete_config(server_info)

        runtime_hint = package.get("runtime_hint", "")
        registry_name = self._infer_registry_name(package)
        pkg_args = self._extract_package_args(package)

        server_config = self._build_package_server_config(
            package, runtime_hint, registry_name, pkg_args
        )
        if not server_config:
            return self._handle_incomplete_config(server_info)

        input_vars = self._build_package_input_vars(package, server_config)
        return server_config, input_vars

    def _build_package_server_config(self, package, runtime_hint, registry_name, pkg_args):
        """Build server configuration for a package."""
        # Handle npm packages
        if runtime_hint == "npx" or registry_name == "npm":
            package_name = package.get("name")
            extra_args = [a for a in pkg_args if a != package_name] if pkg_args else []
            return {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", package_name, *extra_args],
            }

        # Handle docker packages
        if runtime_hint == "docker" or registry_name == "docker":
            args = pkg_args if pkg_args else ["run", "-i", "--rm", package.get("name")]
            return {"type": "stdio", "command": "docker", "args": args}

        # Handle Python packages
        if (
            runtime_hint in ["uvx", "pip", "python"]
            or "python" in runtime_hint
            or registry_name == "pypi"
        ):
            command, args = self._build_python_command_args(package, runtime_hint, pkg_args)
            return {"type": "stdio", "command": command, "args": args}

        # Generic fallback for packages with a runtime_hint
        if package and runtime_hint:
            args = pkg_args if pkg_args else [package.get("name", "")]
            return {"type": "stdio", "command": runtime_hint, "args": args}

        return {}

    def _format_remote_config(self, server_info):
        """Format remote server configuration."""
        # Check for SSE endpoints
        if "sse_endpoint" in server_info:
            server_config = {
                "type": "sse",
                "url": server_info["sse_endpoint"],
                "headers": server_info.get("sse_headers", {}),
            }
            return server_config, []

        # Check for remotes
        if server_info.get("remotes"):
            return self._format_remote_endpoint_config(server_info)

        # No packages AND no endpoints/remotes
        return self._handle_incomplete_config(server_info)

    def _format_remote_endpoint_config(self, server_info):
        """Format configuration for remote endpoint."""
        remote = self._select_remote_with_url(server_info["remotes"])
        if not remote:
            return self._handle_incomplete_config(server_info)

        transport = (remote.get("transport_type") or "").strip()
        if not transport:
            transport = "http"
        elif transport not in ("sse", "http", "streamable-http"):
            raise ValueError(
                f"Unsupported remote transport '{transport}' for VS Code. "
                f"Server: {server_info.get('name', 'unknown')}. "
                f"Supported transports: http, sse, streamable-http."
            )

        headers = remote.get("headers", {})
        if isinstance(headers, list):
            headers = {h["name"]: h["value"] for h in headers if "name" in h and "value" in h}

        self._warn_on_legacy_angle_vars(headers, server_info.get("name", "unknown"), "headers")
        headers = self._translate_env_vars_for_vscode(headers)

        server_config = {
            "type": transport,
            "url": remote["url"].strip(),
            "headers": headers,
        }
        input_vars = self._extract_input_variables(headers, server_info.get("name", ""))
        return server_config, input_vars

    def _handle_incomplete_config(self, server_info):
        """Handle error case for incomplete server configuration."""
        packages = server_info.get("packages", [])
        if packages:
            inferred = [self._infer_registry_name(p) or p.get("name", "unknown") for p in packages]
            raise ValueError(
                f"No supported transport for VS Code runtime. "
                f"Server '{server_info.get('name', 'unknown')}' provides stdio packages "
                f"({', '.join(inferred)}) but none could be mapped to a VS Code configuration. "
                f"Supported package types: npm, pypi, docker."
            )
        raise ValueError(
            f"MCP server has incomplete configuration in registry - no package information or remote endpoints available. "
            f"Server: {server_info.get('name', 'unknown')}"
        )

    def _extract_input_variables(self, mapping, server_name):
        """Scan dict values for ${input:...} references and return input variable definitions.

        Args:
            mapping (dict): Header or env dict whose values may contain
                ``${input:<id>}`` placeholders.
            server_name (str): Server name used in the description field.

        Returns:
            list[dict]: Input variable definitions (``promptString``, ``password: true``).
                Duplicates within *mapping* are already deduplicated.
        """
        seen: set = set()
        result: list = []
        for value in (mapping or {}).values():
            if not isinstance(value, str):
                continue
            for match in _INPUT_VAR_RE.finditer(value):
                var_id = match.group(1)
                if var_id in seen:
                    continue
                seen.add(var_id)
                result.append(
                    {
                        "type": "promptString",
                        "id": var_id,
                        "description": f"{var_id} for MCP server {server_name}",
                        "password": True,
                    }
                )
        return result

    def _select_best_package(self, packages):
        """Select the best package for VS Code installation from available packages.

        Prioritizes packages in order: npm, pypi, docker, then others.
        Uses ``_infer_registry_name`` so selection works even when the
        API returns an empty ``registry_name``.

        Args:
            packages (list): List of package dictionaries.

        Returns:
            dict: Best package to use, or None if no suitable package found.
        """
        priority_order = ["npm", "pypi", "docker"]

        for target in priority_order:
            for package in packages:
                if self._infer_registry_name(package) == target:
                    return package

        # Fall back to any package that has a runtime_hint
        for package in packages:
            if package.get("runtime_hint"):
                return package

        return packages[0] if packages else None
