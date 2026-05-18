"""Compile predicates and legacy folder-based detection.

Private module — do not import directly outside of ``apm_cli.core``.
All public symbols are re-exported from :mod:`apm_cli.core.target_detection`.
"""

from pathlib import Path

from apm_cli.core._target_types import (
    REASON_NO_TARGET_FOLDER,
    CompileTargetType,
    TargetType,
)

# ---------------------------------------------------------------------------
# Legacy detection (v1) — kept for backward compatibility
# ---------------------------------------------------------------------------


def _normalise_target_choice(value: str | None, reason: str) -> tuple[TargetType, str] | None:
    """Return the canonical target for an explicit/configured target value."""
    if not value:
        return None
    aliases: dict[str, TargetType] = {
        "copilot": "vscode",
        "vscode": "vscode",
        "agents": "vscode",
        "claude": "claude",
        "cursor": "cursor",
        "opencode": "opencode",
        "codex": "codex",
        "gemini": "gemini",
        "windsurf": "windsurf",
        "agent-skills": "agent-skills",
        "all": "all",
    }
    target = aliases.get(value)
    if target is None:
        return None
    return target, reason


def detect_target(
    project_root: Path,
    explicit_target: str | None = None,
    config_target: str | None = None,
) -> tuple[TargetType, str]:
    """Detect the appropriate target for compilation and integration."""
    for value, reason in (
        (explicit_target, "explicit --target flag"),
        (config_target, "apm.yml target"),
    ):
        choice = _normalise_target_choice(value, reason)
        if choice is not None:
            return choice

    folder_targets: tuple[tuple[str, TargetType, str, bool], ...] = (
        (".github/", "vscode", "detected .github/ folder", (project_root / ".github").exists()),
        (".claude/", "claude", "detected .claude/ folder", (project_root / ".claude").exists()),
        (".cursor/", "cursor", "detected .cursor/ folder", (project_root / ".cursor").is_dir()),
        (
            ".opencode/",
            "opencode",
            "detected .opencode/ folder",
            (project_root / ".opencode").is_dir(),
        ),
        (".codex/", "codex", "detected .codex/ folder", (project_root / ".codex").is_dir()),
        (".gemini/", "gemini", "detected .gemini/ folder", (project_root / ".gemini").is_dir()),
        (
            ".windsurf/",
            "windsurf",
            "detected .windsurf/ folder",
            (project_root / ".windsurf").is_dir(),
        ),
    )
    detected = [label for label, _, _, exists in folder_targets if exists]
    if len(detected) >= 2:
        return "all", f"detected {' and '.join(detected)} folders"
    for _, target, reason, exists in folder_targets:
        if exists:
            return target, reason
    return "minimal", REASON_NO_TARGET_FOLDER


# ---------------------------------------------------------------------------
# Compile predicates
# ---------------------------------------------------------------------------


def should_compile_agents_md(target: CompileTargetType) -> bool:
    """Check if AGENTS.md should be compiled.

    AGENTS.md is generated for vscode, codex, gemini, all, and minimal
    targets.  Gemini needs it because GEMINI.md imports AGENTS.md.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if AGENTS.md should be generated
    """
    if isinstance(target, frozenset):
        return "agents" in target or "gemini" in target
    return target in ("vscode", "opencode", "codex", "gemini", "windsurf", "all", "minimal")


def should_compile_claude_md(target: CompileTargetType) -> bool:
    """Check if CLAUDE.md should be compiled.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if CLAUDE.md should be generated
    """
    if isinstance(target, frozenset):
        return "claude" in target
    return target in ("claude", "all")


def should_compile_gemini_md(target: CompileTargetType) -> bool:
    """Check if GEMINI.md should be compiled.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if GEMINI.md should be generated
    """
    if isinstance(target, frozenset):
        return "gemini" in target
    return target in ("gemini", "all")


def should_compile_copilot_instructions_md(target: CompileTargetType) -> bool:
    """Check if .github/copilot-instructions.md should be compiled.

    Only the Copilot-native targets (copilot/vscode/agents alias) and "all"
    trigger generation.  cursor, opencode, and codex use their own native
    configuration files and must NOT receive copilot-instructions.md, even
    when combined in a multi-target list.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if Copilot root instructions should be generated
    """
    if isinstance(target, frozenset):
        # "vscode" family is added to the frozenset by _resolve_compile_target()
        # ONLY when copilot/vscode/agents was in the original list. Checking
        # "agents" would over-fire because cursor/opencode/codex also map to
        # the "agents" family for AGENTS.md generation.
        return "vscode" in target
    return target in ("vscode", "all")


def get_target_description(target: str) -> str:
    """Get a human-readable description of what will be generated for a target.

    Accepts both internal target types and user-facing aliases.

    Args:
        target: The target type (internal or user-facing alias)

    Returns:
        str: Description of output files
    """
    # Normalize aliases to internal value for lookup
    normalized = "vscode" if target in ("copilot", "agents") else target
    descriptions = {
        "vscode": "AGENTS.md + .github/copilot-instructions.md + .github/prompts/ + .github/agents/",
        "claude": "CLAUDE.md + .claude/commands/ + .claude/agents/ + .claude/skills/",
        "cursor": ".cursor/agents/ + .cursor/skills/ + .cursor/rules/",
        "opencode": "AGENTS.md + .opencode/agents/ + .opencode/commands/ + .opencode/skills/",
        "codex": "AGENTS.md + .agents/skills/ + .codex/agents/ + .codex/hooks.json",
        "gemini": "GEMINI.md + .gemini/commands/ + .gemini/skills/ + .gemini/settings.json (MCP/hooks)",
        "windsurf": "AGENTS.md + .windsurf/rules/ + .windsurf/skills/ + .windsurf/workflows/ + .windsurf/hooks.json",
        "agent-skills": ".agents/skills/ only (cross-client shared skills -- no agents, hooks, or commands)",
        "all": "AGENTS.md + CLAUDE.md + GEMINI.md + .github/copilot-instructions.md + .github/ + .claude/ + .cursor/ + .opencode/ + .codex/ + .gemini/ + .windsurf/ + .agents/",
        "minimal": "AGENTS.md only (create .github/, .claude/, or .gemini/ for full integration)",
    }
    return descriptions.get(normalized, "unknown target")
