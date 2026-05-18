"""apm.yml generation and reverse-synthesis from Claude plugin manifests.

Private module – imported only via :mod:`apm_cli.deps.plugin_parser`.
"""

import logging
from pathlib import Path
from typing import Any

import yaml


def _generate_apm_yml(manifest: dict[str, Any]) -> str:
    """Generate apm.yml content from plugin metadata.

    Args:
        manifest: Plugin metadata dict.

    Returns:
        str: YAML content for apm.yml.
    """
    apm_package: dict[str, Any] = {
        "name": manifest.get("name"),
        "version": manifest.get("version", "0.0.0"),
        "description": manifest.get("description", ""),
    }

    # author: spec defines it as {name, email, url} object; accept string too
    if "author" in manifest:
        author = manifest["author"]
        if isinstance(author, dict):
            apm_package["author"] = author.get("name", "")
        else:
            apm_package["author"] = str(author)

    for field in ("license", "repository", "homepage", "tags"):
        if field in manifest:
            apm_package[field] = manifest[field]

    if manifest.get("dependencies"):
        apm_package["dependencies"] = {"apm": manifest["dependencies"]}

    # Inject MCP deps extracted from plugin mcpServers / .mcp.json
    mcp_deps = manifest.get("_mcp_deps")
    if mcp_deps:
        apm_package.setdefault("dependencies", {})["mcp"] = mcp_deps

    # Install behavior is driven by file presence (SKILL.md, etc.), not this
    # field.  Default to hybrid so the standard pipeline handles all components.
    apm_package["type"] = "hybrid"

    from ...utils.yaml_io import yaml_to_str

    return yaml_to_str(apm_package)


def synthesize_plugin_json_from_apm_yml(apm_yml_path: Path) -> dict:
    """Create a minimal ``plugin.json`` dict from ``apm.yml`` identity fields.

    Reads ``apm.yml`` and extracts ``name``, ``version``, ``description``,
    ``author``, and ``license``.  The ``author`` string is mapped to the plugin
    spec's ``{"name": author}`` object format.

    Args:
        apm_yml_path: Path to the ``apm.yml`` file.

    Returns:
        dict suitable for writing as ``plugin.json``.

    Raises:
        ValueError: If ``name`` is missing from ``apm.yml``.
        FileNotFoundError: If the file does not exist.
    """
    if not apm_yml_path.exists():
        raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")

    try:
        from ...utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {apm_yml_path}: {exc}") from exc

    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError("apm.yml must contain at least a 'name' field to synthesize plugin.json")

    result: dict[str, Any] = {"name": data["name"]}

    if data.get("version"):
        result["version"] = data["version"]
    if data.get("description"):
        result["description"] = data["description"]
    if data.get("author"):
        result["author"] = {"name": str(data["author"])}
    if data.get("license"):
        result["license"] = data["license"]

    return result
