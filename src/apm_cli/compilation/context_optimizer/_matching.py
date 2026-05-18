"""Pattern-matching helpers for the Context Optimizer.

Extracted from analysis.py to keep that module under the 500-line limit.
All functions follow the delegation pattern: they accept ``self`` (a
``ContextOptimizer`` instance) as the first positional argument so that
``class_.py`` can route ``ContextOptimizer`` method calls here without
requiring inheritance or mixins.
"""

import builtins
import fnmatch
import re
from pathlib import Path

from ...utils.paths import portable_relpath

# CRITICAL: Shadow Click commands to prevent namespace collision.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _extract_intended_directory_from_pattern(self, pattern: str) -> Path | None:
    """Extract the intended directory from a pattern like 'docs/**/*.md' -> 'docs'.

    Args:
        pattern (str): File pattern to analyze.

    Returns:
        Optional[Path]: Intended directory path, or None if pattern is global.
    """
    if not pattern or pattern.startswith("**/"):
        return None  # Global pattern

    if "/" in pattern:
        # Extract the first directory component
        parts = pattern.split("/")
        first_part = parts[0]

        # Skip if it's a wildcard
        if "*" not in first_part and first_part:
            intended_dir = self.base_dir / first_part
            if intended_dir.exists() and intended_dir.is_dir():
                return intended_dir

    return None


def _expand_glob_pattern(self, pattern: str) -> builtins.list[str]:
    """Expand glob pattern with brace expansion, supporting multiple brace groups.

    Args:
        pattern (str): Pattern like '**/*.{css,scss}' or '**/*.{test,spec}.{ts,js}'

    Returns:
        List[str]: Expanded patterns like ['**/*.css', '**/*.scss']
                   or ['**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js']
    """
    # Handle brace expansion like {css,scss}
    brace_match = re.search(r"\{([^}]+)\}", pattern)
    if brace_match:
        alternatives = brace_match.group(1).split(",")
        prefix = pattern[: brace_match.start()]
        suffix = pattern[brace_match.end() :]
        # Recursively expand remaining brace groups in each result
        expanded = []
        for alt in alternatives:
            expanded.extend(self._expand_glob_pattern(prefix + alt + suffix))
        return expanded

    return [pattern]


def _file_matches_pattern(self, file_path: Path, pattern: str) -> bool:
    """Check if a file matches a given pattern with optimized performance.

    Args:
        file_path (Path): File path to check
        pattern (str): Glob pattern to match against

    Returns:
        bool: True if file matches pattern
    """
    # Expand any brace patterns
    expanded_patterns = self._expand_glob_pattern(pattern)

    for expanded_pattern in expanded_patterns:
        # For patterns with **, use cached glob results
        if "**" in expanded_pattern:
            try:
                # Resolve both paths to handle symlinks and path inconsistencies
                resolved_file = file_path.resolve()
                rel_path = resolved_file.relative_to(self.base_dir.resolve())

                # Use cached glob results instead of repeated glob calls
                matches = self._cached_glob(expanded_pattern)
                # Use cached Set[Path] to avoid recreating on every call
                if expanded_pattern not in self._glob_set_cache:
                    self._glob_set_cache[expanded_pattern] = {Path(match) for match in matches}
                if rel_path in self._glob_set_cache[expanded_pattern]:
                    return True
            except (ValueError, OSError):
                pass
        else:
            # For non-recursive patterns, use fnmatch as before
            try:
                rel_str = portable_relpath(file_path, self.base_dir)
                if fnmatch.fnmatch(rel_str, expanded_pattern):
                    return True
            except ValueError:
                pass

            # Only use filename match for patterns without directory structure
            # This prevents "docs/**/*.md" from matching any "*.md" file anywhere
            if "/" not in expanded_pattern:
                if fnmatch.fnmatch(file_path.name, expanded_pattern):
                    return True

    return False


def _find_matching_directories(self, pattern: str) -> builtins.set[Path]:
    """Find directories that contain files matching the pattern.

    Args:
        pattern (str): File pattern to match.

    Returns:
        Set[Path]: Set of directories with matching files.
    """
    # Use cached result if available
    if pattern in self._pattern_cache:
        return self._pattern_cache[pattern]

    matching_dirs: builtins.set[Path] = set()

    # Use the reliable approach for all patterns
    for directory, analysis in sorted(self._directory_cache.items()):
        try:
            files = [f for f in directory.iterdir() if f.is_file() and not f.name.startswith(".")]

            match_count = 0
            for file_path in files:
                if self._file_matches_pattern(file_path, pattern):
                    match_count += 1
                    matching_dirs.add(directory)

            if match_count > 0:
                analysis.pattern_matches[pattern] = match_count
        except (OSError, PermissionError):
            continue

    self._pattern_cache[pattern] = matching_dirs
    return matching_dirs
