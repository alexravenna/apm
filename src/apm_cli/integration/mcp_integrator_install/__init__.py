from __future__ import annotations

from .run import run_mcp_install  # noqa: F401

# Re-export contract for ruff --ignore-noqa.
__all__ = [
    "run_mcp_install",
]
