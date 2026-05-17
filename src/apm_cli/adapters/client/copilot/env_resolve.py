"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

import os
import re

from ..base import _ENV_VAR_RE
from .class_ import (
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    _translate_env_placeholder,
)

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _resolve_environment_variables(self, env_vars, env_overrides=None):
    """Resolve (or translate) declared environment variables.

    Behaviour depends on ``self._supports_runtime_env_substitution``:

    - True (Copilot CLI default): each declared env var ``NAME`` gets a
      ``${NAME}`` placeholder that Copilot CLI resolves at server-start
      from the host environment. Hardcoded literal defaults
      (``GITHUB_TOOLSETS``, ``GITHUB_DYNAMIC_TOOLSETS``) stay literal
      because they are not secrets and provide essential server
      configuration. The host environment is NOT read; secrets never
      touch disk. See issue #1152 for context.

    - False (legacy / sibling-adapter behaviour): resolve each variable
      to its literal value via ``env_overrides`` -> ``os.environ`` ->
      optional interactive prompt, baking the result into the config.

    Args:
        env_vars (list): List of environment variable definitions from
            server info (each item is ``{name, description, required}``).
        env_overrides (dict, optional): Pre-collected environment
            variable overrides. Ignored in translate mode.

    Returns:
        dict: ``{name: value}`` -- placeholder string in translate mode,
        literal value in legacy mode.
    """
    # Hardcoded literal defaults that supply essential server behaviour
    # rather than secrets. These stay literal in translate mode so that
    # tool-selection still works without a user export step.
    default_github_env = {"GITHUB_TOOLSETS": "context", "GITHUB_DYNAMIC_TOOLSETS": "1"}

    # Self-defined stdio deps pass ``env`` as a plain dict
    # ({NAME: value-or-placeholder}); registry-sourced deps pass a list
    # of {name, description, required} dicts. Translate-mode handling
    # for the dict shape: each value is either already a placeholder
    # (translate it to the canonical ${VAR} form) or a literal (record
    # the key as a placeholder reference and emit ${NAME} so the
    # value never lands on disk). See issue #1152.
    if isinstance(env_vars, dict) and self._supports_runtime_env_substitution:
        translated = {}
        placeholder_keys = []
        for name, raw_value in env_vars.items():
            if not name:
                continue
            if raw_value is None:
                continue
            if not isinstance(raw_value, str):
                translated[name] = _stringify_env_literal(raw_value)
                continue
            if _has_env_placeholder(raw_value):
                self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(raw_value))
                translated[name] = _translate_env_placeholder(raw_value)
                # Record every ${VAR} in the translated value (handles
                # both ${env:VAR} -> ${VAR} and bare ${VAR} cases).
                for match in _ENV_VAR_RE.finditer(translated[name]):
                    placeholder_keys.append(match.group(1))
            elif name in default_github_env and raw_value == default_github_env[name]:
                translated[name] = raw_value
            else:
                # Literal value present in apm.yml -- replace with a
                # runtime placeholder so the secret never touches disk.
                translated[name] = "${" + name + "}"
                placeholder_keys.append(name)
        self._last_env_placeholder_keys = set(placeholder_keys)
        return translated

    if self._supports_runtime_env_substitution:
        resolved = {}
        placeholder_keys = []
        for env_var in env_vars:
            if not isinstance(env_var, dict):
                continue
            name = env_var.get("name", "")
            if not name:
                continue
            if name in default_github_env:
                # Non-secret literal default -- preserve as-is.
                resolved[name] = default_github_env[name]
            else:
                # Emit a runtime-substitution placeholder; Copilot CLI
                # resolves ``${NAME}`` from the host environment at
                # server-start. APM never reads or stores the value.
                resolved[name] = "${" + name + "}"
                placeholder_keys.append(name)
        # Record for the post-install summary line and the
        # security-improvement notice.
        self._last_env_placeholder_keys = set(placeholder_keys)
        return resolved

    if isinstance(env_vars, dict):
        resolved = {}
        for name, value in env_vars.items():
            if not name:
                continue
            if isinstance(value, str):
                resolved[name] = self._resolve_env_variable(
                    name, value, env_overrides=env_overrides
                )
            elif value is not None:
                resolved[name] = _stringify_env_literal(value)
        return resolved

    import os
    import sys

    from rich.prompt import Prompt

    resolved = {}
    env_overrides = env_overrides or {}

    # If env_overrides is provided, it means the CLI has already handled environment variable collection
    # In this case, we should NEVER prompt for additional variables
    skip_prompting = bool(env_overrides)

    # Check for CI/automated environment via APM_E2E_TESTS flag (more reliable than TTY detection)
    if os.getenv("APM_E2E_TESTS") == "1":
        skip_prompting = True
        print(" APM_E2E_TESTS detected, will skip environment variable prompts")

    # Also skip prompting if we're in a non-interactive environment (fallback)
    is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_interactive:
        skip_prompting = True

    # Track which variables were explicitly provided with empty values (user wants defaults)
    empty_value_vars = set()
    if env_overrides:
        for key, value in env_overrides.items():
            if key in env_overrides and (not value or not value.strip()):
                empty_value_vars.add(key)

    for env_var in env_vars:
        if isinstance(env_var, dict):
            name = env_var.get("name", "")
            description = env_var.get("description", "")
            required = env_var.get("required", True)

            if name:
                # First check overrides, then environment
                value = env_overrides.get(name) or os.getenv(name)

                # Only prompt if not provided in overrides or environment AND it's required AND we're not in managed override mode
                if not value and required and not skip_prompting:
                    prompt_text = f"Enter value for {name}"
                    if description:
                        prompt_text += f" ({description})"
                    value = Prompt.ask(
                        prompt_text,
                        password=bool("token" in name.lower() or "key" in name.lower()),
                    )

                # Add variable if it has a value OR if user explicitly provided empty and we have a default
                if value and value.strip():
                    resolved[name] = value
                elif name in empty_value_vars and name in default_github_env:
                    # User provided empty value and we have a default - use default
                    resolved[name] = default_github_env[name]
                elif not required and name in default_github_env:
                    # Variable is optional and we have a default - use default
                    resolved[name] = default_github_env[name]
                elif skip_prompting and name in default_github_env:
                    # Non-interactive environment and we have a default - use default
                    resolved[name] = default_github_env[name]

    return resolved


def _resolve_env_variable(self, name, value, env_overrides=None):
    """Resolve (or translate) a single environment variable value.

    Behaviour depends on ``self._supports_runtime_env_substitution``:

    - True (Copilot CLI default): translate placeholders to Copilot CLI's
      native runtime substitution syntax (``${VAR}``). The host
      environment is NOT read; the secret never touches disk. See issue
      #1152 for context. Legacy ``<VAR>`` offenders are tracked for the
      aggregated deprecation warning emitted by
      ``configure_mcp_server``.

    - False (legacy / sibling-adapter behaviour): resolve placeholders
      to literal values via ``env_overrides`` -> ``os.environ`` ->
      optional interactive prompt, baking the result into the config.

    Args:
        name (str): Environment variable name.
        value (str): Environment variable value or placeholder.
        env_overrides (dict, optional): Pre-collected environment
            variable overrides. Ignored in translate mode.

    Returns:
        str: Translated placeholder (translate mode) or resolved
        literal value (legacy mode).
    """
    if self._supports_runtime_env_substitution:
        # Track legacy <VAR> offenders for the aggregated deprecation
        # warning. Translation itself is a pure-textual rewrite.
        self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(value))
        # Track env-var names referenced via this header/value so the
        # security-upgrade detector and per-server summary can see
        # them (the env-block path tracks via _resolve_environment_variables).
        for match in _ENV_VAR_RE.finditer(value):
            self._last_env_placeholder_keys.add(match.group(1))
        return _translate_env_placeholder(value)

    import sys

    from rich.prompt import Prompt

    env_overrides = env_overrides or {}
    # If env_overrides is provided, it means we're in managed environment collection mode
    skip_prompting = bool(env_overrides)

    # Check for CI/automated environment via APM_E2E_TESTS flag (more reliable than TTY detection)
    if os.getenv("APM_E2E_TESTS") == "1":
        skip_prompting = True

    # Also skip prompting if we're in a non-interactive environment (fallback)
    is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not is_interactive:
        skip_prompting = True

    # Three accepted placeholder syntaxes (see _COPILOT_ENV_RE at module
    # top), all resolved against env_overrides -> os.environ -> optional
    # interactive prompt. Single-pass substitution preserves the legacy
    # ``<VAR>`` semantics: resolved values are not re-scanned for further
    # placeholder expansion.
    def _replace(match):
        # Group 1 = legacy <VAR>; group 2 = ${VAR} / ${env:VAR}.
        env_name = match.group(1) or match.group(2)
        env_value = env_overrides.get(env_name) or os.getenv(env_name)
        if not env_value and not skip_prompting:
            prompt_text = f"Enter value for {env_name}"
            env_value = Prompt.ask(
                prompt_text,
                password=bool("token" in env_name.lower() or "key" in env_name.lower()),
            )
        return env_value if env_value else match.group(0)

    return _COPILOT_ENV_RE.sub(_replace, value)
