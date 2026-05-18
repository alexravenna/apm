"""v2 target resolution algorithm (#1154).

Private module — do not import directly outside of ``apm_cli.core``.
All public symbols are re-exported from :mod:`apm_cli.core.target_detection`.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Signal:
    """A filesystem marker that indicates a harness is present."""

    target: str  # canonical target name: 'claude', 'copilot', etc.
    source: str  # human-readable: 'CLAUDE.md', '.github/copilot-instructions.md'


@dataclass(frozen=True)
class ResolvedTargets:
    """Result of target resolution -- the single source of truth."""

    targets: list[str]  # sorted canonical target names
    source: str  # '--target flag' | 'apm.yml' | 'auto-detect from <csv>'
    auto_create: bool  # always True after resolution (three-guard collapse)


# Detection signal whitelist.
# (target, check_type, path)
# check_type: 'dir' = is_dir(), 'file' = is_file()
SIGNAL_WHITELIST: list[tuple[str, str, str]] = [
    ("claude", "dir", ".claude"),
    ("claude", "file", "CLAUDE.md"),
    ("cursor", "dir", ".cursor"),
    ("cursor", "file", ".cursorrules"),  # legacy; .cursor/ is canonical
    ("copilot", "file", ".github/copilot-instructions.md"),
    ("codex", "dir", ".codex"),
    ("gemini", "dir", ".gemini"),
    ("gemini", "file", "GEMINI.md"),
    ("opencode", "dir", ".opencode"),
    ("windsurf", "dir", ".windsurf"),
]


def detect_signals(project_root: Path) -> list[Signal]:
    """Scan project_root for harness markers per SIGNAL_WHITELIST."""
    found: list[Signal] = []
    for target, check_type, rel_path in SIGNAL_WHITELIST:
        full = project_root / rel_path
        if check_type == "dir" and full.is_dir():
            found.append(Signal(target=target, source=rel_path + "/"))
        elif check_type == "file" and full.is_file():
            found.append(Signal(target=target, source=rel_path))
    return found


def _validate_canonical_v2(tokens: list[str]) -> None:
    """Validate every token is a known canonical target."""
    from apm_cli.core.apm_yml import CANONICAL_TARGETS
    from apm_cli.core.errors import UnknownTargetError, render_unknown_target_error

    for token in tokens:
        if token not in CANONICAL_TARGETS:
            raise UnknownTargetError(render_unknown_target_error(token, sorted(CANONICAL_TARGETS)))


def resolve_targets(
    project_root: Path,
    *,
    flag: str | list[str] | None = None,
    yaml_targets: list[str] | None = None,
) -> ResolvedTargets:
    """Resolve effective targets. Raises on error.

    Priority: flag > yaml_targets > auto-detect signals.
    """
    from apm_cli.core.errors import (
        AmbiguousHarnessError,
        NoHarnessError,
        render_ambiguous_error,
        render_no_harness_error,
    )

    # Priority 1: --target flag
    if flag is not None:
        tokens = [flag] if isinstance(flag, str) else list(flag)
        _validate_canonical_v2(tokens)
        return ResolvedTargets(
            targets=sorted(tokens),
            source="--target flag",
            auto_create=True,
        )

    # Priority 2: apm.yml targets (already validated by parse_targets_field)
    if yaml_targets is not None and len(yaml_targets) > 0:
        return ResolvedTargets(
            targets=sorted(yaml_targets),
            source="apm.yml",
            auto_create=True,
        )

    # Priority 3: auto-detect from signals
    signals = detect_signals(project_root)

    # Dedupe by target (e.g. .claude/ + CLAUDE.md both -> 'claude')
    target_set = sorted({s.target for s in signals})
    signal_sources = sorted({s.source for s in signals})

    if len(target_set) == 0:
        raise NoHarnessError(render_no_harness_error(project_root))

    if len(target_set) >= 2:
        raise AmbiguousHarnessError(render_ambiguous_error(project_root, target_set))

    # Exactly 1 target detected
    return ResolvedTargets(
        targets=target_set,
        source=f"auto-detect from {', '.join(signal_sources)}",
        auto_create=True,
    )


def expand_all_targets(
    project_root: Path,
    *,
    yaml_targets: list[str] | None = None,
) -> list[str]:
    """Expand 'all' to (signals union yaml_targets). Raises NoHarnessError if empty."""
    from apm_cli.core.errors import NoHarnessError, render_no_harness_error

    signals = detect_signals(project_root)
    signal_set = {s.target for s in signals}

    yaml_set = set(yaml_targets) if yaml_targets else set()

    combined = sorted(signal_set | yaml_set)

    if not combined:
        raise NoHarnessError(render_no_harness_error(project_root))

    return combined


def format_provenance(resolved: ResolvedTargets) -> str:
    """Format provenance line for CLI output.

    Returns the message portion (without the [i] prefix, since
    _rich_info adds it).

    # Double-space between target list and metadata is intentional and
    # canonical. Test assertions match this exact spacing. Do not collapse.
    """
    targets_csv = ", ".join(resolved.targets)
    return f"Targets: {targets_csv}  (source: {resolved.source})"
