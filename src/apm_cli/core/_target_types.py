"""Type definitions and constants for target detection.

Private module — do not import directly outside of ``apm_cli.core``.
All public symbols are re-exported from :mod:`apm_cli.core.target_detection`.
"""

from typing import Literal, Union

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# Valid target values (internal canonical form)
TargetType = Literal[
    "vscode",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
    "agent-skills",
    "all",
    "minimal",
]

# Compiler families used inside a multi-target frozenset. Narrower than
# TargetType because the families are produced by _resolve_compile_target()
# (in the compile CLI) from CLI-validated target names.
#
# Family semantics:
#   "agents"  -> AGENTS.md is generated (any of copilot/vscode/agents/cursor/
#                opencode/codex was requested)
#   "vscode"  -> .github/copilot-instructions.md is generated (only when
#                copilot/vscode/agents was specifically requested -- NOT for
#                cursor/opencode/codex which use their own native config files)
#   "claude"  -> CLAUDE.md is generated
#   "gemini"  -> GEMINI.md is generated
CompileFamily = Literal["agents", "vscode", "claude", "gemini"]

# Compile target: either a single TargetType string or a frozenset of compiler
# families ({"agents", "claude", "gemini"}) for multi-target lists.
CompileTargetType = Union[TargetType, frozenset[CompileFamily]]  # noqa: UP007

# Detection reason returned by detect_target() when no integration folder is
# present. Exported as a constant so consumers can compare with equality
# instead of substring matching.
REASON_NO_TARGET_FOLDER = "no target folder found"

# User-facing target values (includes aliases accepted by CLI)
UserTargetType = Literal[
    "copilot",
    "vscode",
    "agents",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
    "agent-skills",
    "all",
    "minimal",
]


# ---------------------------------------------------------------------------
# Multi-target constants
# ---------------------------------------------------------------------------

#: The complete set of real (non-pseudo) canonical targets.
#: "minimal" is intentionally excluded -- it is a fallback pseudo-target.
ALL_CANONICAL_TARGETS = frozenset(
    {"vscode", "claude", "cursor", "opencode", "codex", "gemini", "windsurf"}
)

#: Targets that the parser must accept but that are gated at runtime by
#: ``is_enabled()`` in ``core/experimental.py`` and ``_flag_gated()`` in
#: ``integration/targets.py``.  They are NOT included in the
#: ``parse_target_arg("all")`` expansion -- explicit opt-in only.
EXPERIMENTAL_TARGETS: frozenset[str] = frozenset({"copilot-cowork"})

#: Stable targets excluded from "all" expansion (cross-client deploy
#: locations). Unlike EXPERIMENTAL_TARGETS, these are GA -- they just do
#: not represent a single client tool.
EXPLICIT_ONLY_TARGETS: frozenset[str] = frozenset({"agent-skills"})

#: Alias mapping: user-facing name -> canonical internal name.
TARGET_ALIASES: dict[str, str] = {
    "copilot": "vscode",
    "agents": "vscode",
    "vscode": "vscode",
}

#: All values accepted by the ``--target`` CLI option.
#: Derived from canonical targets, alias keys, and the ``"all"`` keyword.
VALID_TARGET_VALUES: frozenset[str] = (
    ALL_CANONICAL_TARGETS
    | EXPERIMENTAL_TARGETS
    | EXPLICIT_ONLY_TARGETS
    | frozenset(TARGET_ALIASES)
    | frozenset({"all"})
)


def normalize_target_list(
    value: str | list[str] | None,
) -> list[str] | None:
    """Normalize a user-provided target value to a list of canonical names.

    Handles:
    - ``None`` -> ``None`` (auto-detect)
    - ``"claude"`` -> ``["claude"]``
    - ``"copilot"`` -> ``["vscode"]``  (alias resolution)
    - ``"all"`` -> ``["claude", "codex", "cursor", "gemini", "opencode", "vscode"]``
    - ``["claude", "copilot"]`` -> ``["claude", "vscode"]``
    - Deduplicates while preserving first-seen order.

    Args:
        value: A single target string, a list of target strings, or ``None``.

    Returns:
        A deduplicated list of canonical target names, or ``None`` if the
        input was ``None`` (meaning "auto-detect").
    """
    if value is None:
        return None

    raw: list[str] = [value] if isinstance(value, str) else list(value)

    # "all" anywhere in the input means "every target" -- expand to the
    # full sorted list of canonical targets.
    if "all" in raw:
        return sorted(ALL_CANONICAL_TARGETS)

    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        canonical = TARGET_ALIASES.get(item, item)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# v2 Resolution constants
# ---------------------------------------------------------------------------

# Ordered list of targets for display (excludes agent-skills meta-target).
CANONICAL_TARGETS_ORDERED: list[str] = [
    "claude",
    "copilot",
    "cursor",
    "codex",
    "gemini",
    "opencode",
    "windsurf",
]

# Canonical deploy directories for each target.
CANONICAL_DEPLOY_DIRS: dict[str, str] = {
    "claude": ".claude/",
    "copilot": ".github/",
    "cursor": ".cursor/",
    "codex": ".codex/",
    "gemini": ".gemini/",
    "opencode": ".opencode/",
    "windsurf": ".windsurf/",
}

# The primary (lowest-friction) signal for each target, used in
# "needs <path>" display for inactive targets.
CANONICAL_SIGNAL: dict[str, str] = {
    "claude": "CLAUDE.md",
    "copilot": ".github/copilot-instructions.md",
    "cursor": ".cursor/",
    "codex": ".codex/",
    "gemini": "GEMINI.md",
    "opencode": ".opencode/",
    "windsurf": ".windsurf/",
}
