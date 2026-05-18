"""Shared data-types and errors for the drift-detection engine.

Kept in a private sibling module so both ``drift.py`` (replay orchestrator)
and ``_drift_diff.py`` (diff engine) can import these without creating a
circular dependency.

Public names are re-exported from ``apm_cli.install.drift`` for back-compat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayConfig:
    """Locked configuration for a drift replay run.

    Frozen so callers cannot mutate it mid-replay -- any change requires
    a new instance, which keeps the contract auditable.
    """

    project_root: Path
    lockfile_path: Path
    targets: frozenset[str] | None = None
    cache_only: bool = True
    no_hooks: bool = True
    parallel_downloads: int = 1


@dataclass(frozen=True)
class DriftFinding:
    """A single divergence between the replay scratch tree and the project."""

    path: str
    kind: str  # one of "modified" | "unintegrated" | "orphaned"
    package: str = ""
    inline_diff: str = ""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CacheMissError(RuntimeError):
    """Raised when ``cache_only=True`` but a package is not in the cache."""


__all__ = [
    "CacheMissError",
    "DriftFinding",
    "ReplayConfig",
]
