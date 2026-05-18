"""Parser for Claude plugins (plugin.json format).

Aligns with the Claude Code plugin spec:
  https://docs.anthropic.com/en/docs/claude-code/plugins

Key spec rules:
- The manifest (.claude-plugin/plugin.json) is **optional**.
- When present, only `name` is required; everything else is optional metadata.
- When absent, the plugin name is derived from the directory name.
- Standard component directories: agents/, commands/, skills/, hooks/
- Pass-through files: .mcp.json, .lsp.json, settings.json
"""

import json
import logging
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sub-module imports (static – PyInstaller discovers these automatically)
# ---------------------------------------------------------------------------
from ._artifacts import _copy_agent_artifacts, _copy_skill_artifacts, _map_plugin_artifacts
from ._mcp import _extract_mcp_servers, _mcp_servers_to_apm_deps
from ._yml import _generate_apm_yml, synthesize_plugin_json_from_apm_yml

# Security contract: the copytree sites that moved into ``_artifacts.py`` still
# use ``ignore_non_content`` at every deploy-target boundary.
# - agents primary copytree -> ignore_non_content
# - agents merge copytree -> ignore_non_content
# - skills custom-list copytree -> ignore_non_content
# - skills primary copytree -> ignore_non_content
# - skills merge copytree -> ignore_non_content
# - hooks primary copytree -> ignore_non_content
# - hooks merge copytree -> ignore_non_content

__all__ = [
    # Public API
    "_copy_agent_artifacts",
    "_copy_skill_artifacts",
    "_extract_mcp_servers",
    "_generate_apm_yml",
    "_map_plugin_artifacts",
    "_mcp_servers_to_apm_deps",
    "normalize_plugin_directory",
    "parse_plugin_manifest",
    "synthesize_apm_yml_from_plugin",
    "synthesize_plugin_json_from_apm_yml",
    "validate_plugin_package",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def parse_plugin_manifest(plugin_json_path: Path) -> dict[str, Any]:
    """Parse a plugin.json manifest file.

    Args:
        plugin_json_path: Path to the plugin.json file

    Returns:
        dict: Parsed plugin manifest

    Raises:
        FileNotFoundError: If plugin.json does not exist
        ValueError: If plugin.json is invalid JSON
    """
    if not plugin_json_path.exists():
        raise FileNotFoundError(f"plugin.json not found: {plugin_json_path}")

    try:
        with open(plugin_json_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in plugin.json: {e}")  # noqa: B904

    if not manifest.get("name"):
        logging.getLogger("apm").warning(
            "plugin.json at %s is missing 'name' field; falling back to directory name",
            plugin_json_path,
        )

    return manifest


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def normalize_plugin_directory(plugin_path: Path, plugin_json_path: Path | None = None) -> Path:
    """Normalize a Claude plugin directory into an APM package.

    Works with or without plugin.json.  When plugin.json is present it is
    treated as optional metadata; when absent the plugin name is derived from
    the directory name.

    Auto-discovers the standard component directories defined by the spec:
    agents/, commands/, skills/, hooks/, and pass-through files
    (.mcp.json, .lsp.json, settings.json).

    Args:
        plugin_path: Root of the plugin directory.
        plugin_json_path: Optional path to plugin.json (may be None).

    Returns:
        Path: Path to the generated apm.yml.
    """
    manifest: dict[str, Any] = {}

    if plugin_json_path is not None and plugin_json_path.exists():
        try:  # noqa: SIM105
            manifest = parse_plugin_manifest(plugin_json_path)
        except (ValueError, FileNotFoundError):
            pass  # Treat as empty manifest; fall back to dir-name defaults

    # Derive name from directory if not in manifest
    if "name" not in manifest or not manifest["name"]:
        manifest["name"] = plugin_path.name

    return synthesize_apm_yml_from_plugin(plugin_path, manifest)


def synthesize_apm_yml_from_plugin(plugin_path: Path, manifest: dict[str, Any]) -> Path:
    """Synthesize apm.yml from plugin metadata.

    Maps the plugin's agents/, skills/, commands/, hooks/ directories and
    pass-through files (.mcp.json, .lsp.json, settings.json) into .apm/,
    then generates apm.yml.

    Args:
        plugin_path: Path to the plugin directory.
        manifest: Plugin metadata dict (only `name` is required; all other
                  fields are optional and default gracefully).

    Returns:
        Path: Path to the generated apm.yml.
    """
    if not manifest.get("name"):
        manifest["name"] = plugin_path.name

    # Create .apm directory structure
    apm_dir = plugin_path / ".apm"
    apm_dir.mkdir(exist_ok=True)

    # Map plugin structure into .apm/ subdirectories
    _map_plugin_artifacts(plugin_path, apm_dir, manifest)

    # Extract MCP servers from plugin and convert to dependency format
    mcp_servers = _extract_mcp_servers(plugin_path, manifest)
    if mcp_servers:
        mcp_deps = _mcp_servers_to_apm_deps(mcp_servers, plugin_path)
        if mcp_deps:
            manifest["_mcp_deps"] = mcp_deps

    # Generate apm.yml from plugin metadata
    apm_yml_content = _generate_apm_yml(manifest)
    apm_yml_path = plugin_path / "apm.yml"

    with open(apm_yml_path, "w", encoding="utf-8") as f:
        f.write(apm_yml_content)

    return apm_yml_path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_plugin_package(plugin_path: Path) -> bool:
    """Check whether a directory looks like a Claude plugin.

    A directory is a valid plugin if it has plugin.json (with at least a name),
    or if it contains at least one standard component directory.

    Args:
        plugin_path: Path to the plugin directory.

    Returns:
        bool: True if the directory appears to be a Claude plugin.
    """
    # Check for plugin.json (optional; only name is required when present)
    from ...utils.helpers import find_plugin_json

    plugin_json = find_plugin_json(plugin_path)
    if plugin_json is not None:
        try:
            with open(plugin_json, encoding="utf-8") as f:
                manifest = json.load(f)
            return bool(manifest.get("name"))
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: presence of any standard component directory
    for component_dir in ("agents", "commands", "skills", "hooks"):
        if (plugin_path / component_dir).is_dir():
            return True

    return False
