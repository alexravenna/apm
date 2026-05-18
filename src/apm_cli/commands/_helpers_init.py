"""Init and script/config helpers extracted from _helpers.py.

This private module holds two cohesive groups that were previously part of
``apm_cli.commands._helpers``:

* **Script / config helpers** — functions for reading ``apm.yml`` scripts and
  configuration, shared by ``run``, ``list``, and ``config`` commands.
* **Init helpers** — project-scaffolding utilities shared by ``init`` and
  ``install`` commands.

All public names are re-exported via ``apm_cli.commands._helpers`` so that
existing callers and test patches remain unaffected.

This module must NOT import from any command module.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..constants import APM_YML_FILENAME

# ------------------------------------------------------------------
# Script / config helpers (shared by run, list, config commands)
# ------------------------------------------------------------------


def _load_apm_config():
    """Load configuration from apm.yml."""
    if Path(APM_YML_FILENAME).exists():
        from ..utils.yaml_io import load_yaml

        return load_yaml(APM_YML_FILENAME)
    return None


def _get_default_script():
    """Get the default script (start) from apm.yml scripts."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config and "start" in apm_config["scripts"]:
        return "start"
    return None


def _list_available_scripts():
    """List all available scripts from apm.yml."""
    apm_config = _load_apm_config()
    if apm_config and "scripts" in apm_config:
        return apm_config["scripts"]
    return {}


# ------------------------------------------------------------------
# Init helpers (shared by init and install commands)
# ------------------------------------------------------------------


def _auto_detect_author():
    """Auto-detect author from git config."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "Developer"


def _auto_detect_description(project_name):
    """Auto-detect description from git repository or use default."""
    try:
        # Try to get git repository description
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # We have a git repo, but description is typically not set
            # Just use a sensible default
            pass
    except Exception:
        pass
    return f"APM project for {project_name}"


def _get_default_config(project_name):
    """Get default configuration for new projects with auto-detection."""
    return {
        "name": project_name,
        "version": "1.0.0",
        "description": _auto_detect_description(project_name),
        "author": _auto_detect_author(),
    }


def _validate_plugin_name(name):
    """Validate plugin name is kebab-case (lowercase, numbers, hyphens).

    Returns True if valid, False otherwise.
    """
    return bool(re.match(r"^[a-z][a-z0-9-]{0,63}$", name))


def _validate_project_name(name):
    """Validate that a project name is safe to use as a directory name.

    Project names are used directly as directory names and must not contain
    '/' or '\' so the name is not interpreted as a filesystem path,
    and must not be '..' to prevent directory traversal.

    Returns True if valid, False otherwise.
    """
    if "/" in name or "\\" in name:
        return False
    if name == "..":  # noqa: SIM103
        return False
    return True


def _create_plugin_json(config):
    """Create plugin.json file with package metadata.

    Args:
        config: dict with name, version, description, author keys.
    """
    plugin_data = {
        "name": config["name"],
        "version": config.get("version", "0.1.0"),
        "description": config.get("description", ""),
        "author": {"name": config.get("author", "")},
        "license": "MIT",
    }

    with open("plugin.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(plugin_data, indent=2) + "\n")


def _create_minimal_apm_yml(config, plugin=False, target_path=None):
    """Create minimal apm.yml file with auto-detected metadata.

    Args:
        config: dict with name, version, description, author keys.
            Optional 'target' key: CSV string of targets to pin.
        plugin: if True, include a devDependencies section.
        target_path: explicit file path to write (defaults to cwd/apm.yml).
    """
    # Create minimal apm.yml structure
    apm_yml_data = {
        "name": config["name"],
        "version": config["version"],
        "description": config["description"],
        "author": config["author"],
    }

    # Add targets field if present in config (plural list form -- canonical).
    # Older callers may still pass a singular CSV "target" string; honor that
    # for backwards compatibility but normalise on disk to plural list form.
    if config.get("targets"):
        apm_yml_data["targets"] = list(config["targets"])
    elif config.get("target"):
        raw = config["target"]
        if isinstance(raw, list):
            apm_yml_data["targets"] = list(raw)
        else:
            apm_yml_data["targets"] = [t.strip() for t in str(raw).split(",") if t.strip()]

    apm_yml_data["dependencies"] = {"apm": [], "mcp": []}
    # Issue #887: scaffold with explicit consent for local content
    # deployment so day-2 audit doesn't surprise the maintainer with
    # an "includes not declared" advisory the moment they drop a
    # primitive in .apm/.  Override with an explicit path list to
    # gate what gets deployed.
    apm_yml_data["includes"] = "auto"

    if plugin:
        apm_yml_data["devDependencies"] = {"apm": []}

    apm_yml_data["scripts"] = {}

    # Write apm.yml
    from ..utils.yaml_io import dump_yaml

    out_path = target_path or APM_YML_FILENAME
    dump_yaml(apm_yml_data, out_path)

    # Post-process: add target comment header
    out_file = Path(out_path)
    content = out_file.read_text(encoding="utf-8")

    if "targets" in apm_yml_data:
        # Insert comment before the targets: line
        targets_comment = (
            "# Which agent platforms to deploy to.\n"
            "# Resolution order: --target flag > this field > auto-detect from filesystem.\n"
            "# Accepted values: copilot, claude, cursor, opencode, codex, gemini, "
            "windsurf, all\n"
        )
        content = content.replace("targets:", targets_comment + "targets:", 1)
    else:
        # Insert commented-out skeleton before dependencies:
        skeleton = (
            "# Which agent platforms to deploy to (uncomment to pin):\n"
            "# targets:\n"
            "#   - copilot\n"
            "#   - claude\n"
        )
        content = content.replace("dependencies:", skeleton + "\ndependencies:", 1)

    out_file.write_text(content, encoding="utf-8")
