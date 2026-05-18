"""Data-model dataclasses for distributed AGENTS.md compilation.

Kept in a private sibling module so that ``distributed_compiler`` can stay
under 500 lines while the public surface (imports from
``distributed_compiler``) is unchanged via re-export.
"""

import builtins
from dataclasses import dataclass, field
from pathlib import Path

from ..primitives.models import Instruction


@dataclass
class DirectoryMap:
    """Mapping of directory structure analysis."""

    directories: builtins.dict[
        Path, builtins.set[str]
    ]  # directory -> set of applicable file patterns
    depth_map: builtins.dict[Path, int]  # directory -> depth level
    parent_map: builtins.dict[Path, Path | None]  # directory -> parent directory

    def get_max_depth(self) -> int:
        """Get maximum depth in the directory structure."""
        return max(self.depth_map.values()) if self.depth_map else 0


@dataclass
class PlacementResult:
    """Result of AGENTS.md placement analysis."""

    agents_path: Path
    instructions: builtins.list[Instruction]
    inherited_instructions: builtins.list[Instruction] = field(default_factory=list)
    coverage_patterns: builtins.set[str] = field(default_factory=set)
    source_attribution: builtins.dict[str, str] = field(
        default_factory=dict
    )  # instruction_id -> source


@dataclass
class CompilationResult:
    """Result of distributed AGENTS.md compilation."""

    success: bool
    placements: builtins.list[PlacementResult]
    content_map: builtins.dict[Path, str]  # agents_path -> content
    warnings: builtins.list[str] = field(default_factory=list)
    errors: builtins.list[str] = field(default_factory=list)
    stats: builtins.dict[str, float] = field(default_factory=dict)  # Support optimization metrics
