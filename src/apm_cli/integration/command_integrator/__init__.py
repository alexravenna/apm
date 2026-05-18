"""Command integration functionality for APM packages.

Integrates .prompt.md files as commands for any target that supports the
``commands`` primitive (e.g. ``.claude/commands/``, ``.opencode/commands/``).

All public names from the original ``command_integrator.py`` module are
re-exported here so existing import paths remain valid after the module was
converted to a package.
"""

from __future__ import annotations

from apm_cli.security.gate import SecurityGate  # noqa: F401

from ._input_helpers import (  # noqa: F401
    _PRESERVED_COMMAND_KEYS,
    _PRESERVED_COMMAND_KEYS_DISPLAY,
    _extract_input_names,
    _is_valid_input_name,
)
from ._integrator import CommandIntegrationResult, CommandIntegrator  # noqa: F401
from ._transform import _transform_prompt_to_command, _write_gemini_command  # noqa: F401

__all__ = [
    "_PRESERVED_COMMAND_KEYS",
    "_PRESERVED_COMMAND_KEYS_DISPLAY",
    "CommandIntegrationResult",
    "CommandIntegrator",
    "SecurityGate",
    "_extract_input_names",
    "_is_valid_input_name",
    "_transform_prompt_to_command",
    "_write_gemini_command",
]
