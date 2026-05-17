from __future__ import annotations

from .core import DependencyReference  # noqa: F401

# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "DependencyReference",
]
