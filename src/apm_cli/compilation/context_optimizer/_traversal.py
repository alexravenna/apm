"""Filesystem traversal helpers for the Context Optimizer.

Extracted from analysis.py to keep that module under the 500-line limit.
All functions follow the delegation pattern: they accept ``self`` (a
``ContextOptimizer`` instance) as the first positional argument so that
``class_.py`` can route ``ContextOptimizer`` method calls here without
requiring inheritance or mixins.
"""

import builtins
import os
from pathlib import Path

from ...utils.exclude import should_exclude
from .class_ import DEFAULT_EXCLUDED_DIRNAMES, DirectoryAnalysis

# CRITICAL: Shadow Click commands to prevent namespace collision.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _analyze_project_structure(self) -> None:
    """Analyze the project structure and cache results."""
    self._directory_cache.clear()
    self._pattern_cache.clear()  # Also clear pattern cache for deterministic behavior

    # Track visited directories to prevent infinite loops
    visited_dirs = set()

    for root, dirs, files in os.walk(self.base_dir):
        current_path = Path(root)

        # Safety check for infinite loops
        if current_path in visited_dirs:
            continue
        visited_dirs.add(current_path)

        # Calculate depth for analysis
        try:
            relative_path = current_path.resolve().relative_to(self.base_dir.resolve())
            depth = len(relative_path.parts)
        except ValueError:
            depth = 0

        # Skip hidden directories and common ignore patterns
        if any(part.startswith(".") for part in current_path.parts[len(self.base_dir.parts) :]):
            continue

        # Default hardcoded exclusions  -- match on exact path components
        if any(part in DEFAULT_EXCLUDED_DIRNAMES for part in relative_path.parts):
            continue

        # Apply configurable exclusion patterns
        if self._should_exclude_path(current_path):
            continue

        # Prune subdirectories from os.walk to avoid descending into excluded paths
        # This significantly improves performance by avoiding expensive traversal
        # Note: Modifying dirs[:] (slice assignment) is the standard Python idiom
        # to control which subdirectories os.walk will descend into
        dirs[:] = [d for d in dirs if not self._should_exclude_subdir(current_path / d)]

        # Analyze files in this directory
        total_files = len([f for f in files if not f.startswith(".")])
        if total_files == 0:
            continue

        analysis = DirectoryAnalysis(directory=current_path, depth=depth, total_files=total_files)

        # Analyze file types
        for file in files:
            if file.startswith("."):
                continue

            file_path = current_path / file
            analysis.file_types.add(file_path.suffix)

        self._directory_cache[current_path] = analysis


def _should_exclude_subdir(self, path: Path) -> bool:
    """Check if a subdirectory should be pruned from os.walk traversal.

    This is an optimization to avoid descending into excluded directories,
    which significantly improves performance in large monorepos.

    Args:
        path: Subdirectory path to check

    Returns:
        True if subdirectory should be pruned from traversal
    """
    # Check if the subdirectory itself matches an exclusion pattern
    if self._should_exclude_path(path):
        return True

    # Also check if subdirectory is a default exclusion
    dir_name = path.name
    if dir_name in DEFAULT_EXCLUDED_DIRNAMES:
        return True

    # Skip hidden directories
    if dir_name.startswith("."):  # noqa: SIM103
        return True

    return False


def _should_exclude_path(self, path: Path) -> bool:
    """Check if a path matches any exclusion pattern.

    Args:
        path: Path to check against exclusion patterns

    Returns:
        True if path should be excluded, False otherwise
    """
    return should_exclude(path, self.base_dir, self._exclude_patterns)
