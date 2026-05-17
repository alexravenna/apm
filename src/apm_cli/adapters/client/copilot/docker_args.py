"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import re

from ..base import _ENV_VAR_RE

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _inject_env_vars_into_docker_args(self, docker_args, env_vars):
    """Inject environment variables into Docker arguments following registry template.

    The registry provides a complete Docker command template in runtime_arguments.
    We need to inject actual environment variable values while respecting the template structure.
    Also ensures required Docker flags (-i, --rm) are present.

    Args:
        docker_args (list): Docker arguments from registry runtime_arguments.
        env_vars (dict): Resolved environment variables.

    Returns:
        list: Docker arguments with environment variables properly injected and required flags.
    """
    if not env_vars:
        env_vars = {}

    result = []
    i = 0
    has_interactive = False
    has_rm = False

    # Check for existing -i and --rm flags
    for arg in docker_args:
        if arg in {"-i", "--interactive"}:
            has_interactive = True
        elif arg == "--rm":
            has_rm = True

    while i < len(docker_args):
        arg = docker_args[i]
        result.append(arg)

        # When we encounter "run", inject required flags first
        if arg == "run":
            # Add -i flag if not present
            if not has_interactive:
                result.append("-i")

            # Add --rm flag if not present
            if not has_rm:
                result.append("--rm")

        # If this is an environment variable name placeholder, replace with actual env var
        if arg in env_vars:
            # This is an environment variable name that should be replaced with -e VAR=value
            result.pop()  # Remove the env var name
            result.extend(["-e", f"{arg}={env_vars[arg]}"])
        elif arg == "-e" and i + 1 < len(docker_args):
            # Handle -e flag followed by env var name
            next_arg = docker_args[i + 1]
            if next_arg in env_vars:
                result.append(f"{next_arg}={env_vars[next_arg]}")
                i += 1  # Skip the next argument as we've processed it
            else:
                # Keep the original argument structure
                result.append(next_arg)
                i += 1

        i += 1

    # Add any remaining environment variables that weren't in the template
    template_env_vars = set()
    for arg in docker_args:
        if arg in env_vars:
            template_env_vars.add(arg)

    for env_name, env_value in env_vars.items():
        if env_name not in template_env_vars:
            # Find a good place to insert additional env vars (after "run" but before image name)
            insert_pos = len(result)
            for idx, arg in enumerate(result):
                if arg == "run":
                    # Insert after run command but before image name (usually last arg)
                    insert_pos = min(len(result) - 1, idx + 1)
                    break

            result.insert(insert_pos, "-e")
            result.insert(insert_pos + 1, f"{env_name}={env_value}")

    # Add default GitHub MCP server environment variables if not already present
    # Only add defaults for variables that were NOT explicitly provided (even if empty)

    existing_env_vars = set()
    for i, arg in enumerate(result):
        if arg == "-e" and i + 1 < len(result):
            env_spec = result[i + 1]
            if "=" in env_spec:
                env_name = env_spec.split("=", 1)[0]
                existing_env_vars.add(env_name)

    # For Copilot, defaults are already added during environment resolution
    # This section is kept for compatibility but shouldn't add duplicates

    return result


def _inject_docker_env_vars(self, args, env_vars):
    """Inject environment variables into Docker arguments.

    Args:
        args (list): Original Docker arguments.
        env_vars (dict): Environment variables to inject.

    Returns:
        list: Updated arguments with environment variables injected.
    """
    result = []

    for arg in args:
        result.append(arg)
        # If this is a docker run command, inject environment variables after "run"
        if arg == "run" and env_vars:
            for env_name, env_value in env_vars.items():
                result.extend(["-e", f"{env_name}={env_value}"])

    return result
