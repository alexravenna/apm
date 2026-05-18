"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import re

from ..base import _ENV_VAR_RE

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _check_docker_flags(docker_args):
    """Check for existing -i and --rm flags in Docker arguments.

    Args:
        docker_args: Docker arguments to scan.

    Returns:
        tuple: (has_interactive, has_rm) flags.
    """
    has_interactive = False
    has_rm = False

    for arg in docker_args:
        if arg in {"-i", "--interactive"}:
            has_interactive = True
        elif arg == "--rm":
            has_rm = True

    return has_interactive, has_rm


def _inject_required_docker_flags(result, has_interactive, has_rm):
    """Inject required -i and --rm flags after the 'run' command.

    Args:
        result: Accumulated result list.
        has_interactive: Whether -i flag is already present.
        has_rm: Whether --rm flag is already present.
    """
    if not has_interactive:
        result.append("-i")
    if not has_rm:
        result.append("--rm")


def _process_env_arg(result, arg, env_vars, docker_args, i):
    """Process environment variable arguments.

    Args:
        result: Accumulated result list.
        arg: Current argument.
        env_vars: Environment variables dict.
        docker_args: Full docker arguments list.
        i: Current index in docker_args.

    Returns:
        int: Updated index (may skip next arg).
    """
    if arg in env_vars:
        # This is an environment variable name that should be replaced with -e VAR=value
        result.pop()  # Remove the env var name
        result.extend(["-e", f"{arg}={env_vars[arg]}"])
        return i
    elif arg == "-e" and i + 1 < len(docker_args):
        # Handle -e flag followed by env var name
        next_arg = docker_args[i + 1]
        if next_arg in env_vars:
            result.append(f"{next_arg}={env_vars[next_arg]}")
        else:
            result.append(next_arg)
        return i + 1
    return i


def _collect_template_env_vars(docker_args, env_vars):
    """Collect environment variable names that were in the original template.

    Args:
        docker_args: Docker arguments from registry.
        env_vars: Environment variables dict.

    Returns:
        set: Environment variable names found in template.
    """
    template_env_vars = set()
    for arg in docker_args:
        if arg in env_vars:
            template_env_vars.add(arg)
    return template_env_vars


def _find_env_insert_position(result):
    """Find the best position to insert additional env vars.

    Finds position after 'run' command but before image name.

    Args:
        result: Current result list.

    Returns:
        int: Index where env vars should be inserted.
    """
    insert_pos = len(result)
    for idx, arg in enumerate(result):
        if arg == "run":
            # Insert after run command but before image name (usually last arg)
            insert_pos = min(len(result) - 1, idx + 1)
            break
    return insert_pos


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
    has_interactive, has_rm = _check_docker_flags(docker_args)

    while i < len(docker_args):
        arg = docker_args[i]
        result.append(arg)

        # When we encounter "run", inject required flags first
        if arg == "run":
            _inject_required_docker_flags(result, has_interactive, has_rm)

        # Process environment variable arguments
        i = _process_env_arg(result, arg, env_vars, docker_args, i)
        i += 1

    # Add any remaining environment variables that weren't in the template
    template_env_vars = _collect_template_env_vars(docker_args, env_vars)

    for env_name, env_value in env_vars.items():
        if env_name not in template_env_vars:
            insert_pos = _find_env_insert_position(result)
            result.insert(insert_pos, "-e")
            result.insert(insert_pos + 1, f"{env_name}={env_value}")

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
