"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import fnmatch
import os
import time
from pathlib import Path

from ...output.models import (
    CompilationResults,
    OptimizationStats,
    PlacementSummary,
    ProjectAnalysis,
)
from ...primitives.models import Instruction
from ...utils.exclude import should_exclude
from ...utils.paths import portable_relpath
from .class_ import DirectoryAnalysis, InheritanceAnalysis

set = builtins.set
list = builtins.list
dict = builtins.dict
DEFAULT_EXCLUDED_DIRNAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".git",
        "dist",
        "build",
        "apm_modules",
    }
)


def analyze_context_inheritance(
    self,
    working_directory: Path,
    placement_map: builtins.dict[Path, builtins.list[Instruction]],
) -> InheritanceAnalysis:
    """Analyze context inheritance chain for a working directory.

    Args:
        working_directory (Path): Directory where agent is working.
        placement_map (Dict[Path, List[Instruction]]): Current placement mapping.

    Returns:
        InheritanceAnalysis: Analysis of inheritance efficiency.
    """
    inheritance_chain = self._get_inheritance_chain(working_directory)

    total_context = 0
    relevant_context = 0

    for directory in inheritance_chain:
        if directory in placement_map:
            instructions = placement_map[directory]
            total_context += len(instructions)

            # Count relevant instructions for working directory
            for instruction in instructions:
                if self._is_instruction_relevant(instruction, working_directory):
                    relevant_context += 1

    pollution_score = 1.0 - (relevant_context / total_context) if total_context > 0 else 0.0

    return InheritanceAnalysis(
        working_directory=working_directory,
        inheritance_chain=inheritance_chain,
        total_context_load=total_context,
        relevant_context_load=relevant_context,
        pollution_score=pollution_score,
    )


def get_optimization_stats(
    self, placement_map: builtins.dict[Path, builtins.list[Instruction]]
) -> OptimizationStats:
    """Calculate optimization statistics for the placement map."""
    if not placement_map:
        return OptimizationStats(
            average_context_efficiency=0.0,
            total_agents_files=0,
            directories_analyzed=len(self._directory_cache),
        )

    # Calculate average context efficiency across all directories with files
    all_directories = set(self._directory_cache.keys())
    efficiency_scores = []

    for directory in all_directories:
        if self._directory_cache[directory].total_files > 0:
            inheritance = self.analyze_context_inheritance(directory, placement_map)
            efficiency_scores.append(inheritance.get_efficiency_ratio())

    average_efficiency = (
        sum(efficiency_scores) / len(efficiency_scores) if efficiency_scores else 0.0
    )

    return OptimizationStats(
        average_context_efficiency=average_efficiency,
        total_agents_files=len(placement_map),
        directories_analyzed=len(self._directory_cache),
    )


def get_compilation_results(
    self,
    placement_map: builtins.dict[Path, builtins.list[Instruction]],
    is_dry_run: bool = False,
) -> CompilationResults:
    """Generate comprehensive compilation results for output formatting.

    Args:
        placement_map: Final instruction placement mapping.
        is_dry_run: Whether this is a dry run.

    Returns:
        CompilationResults with all analysis data.
    """
    # Calculate generation time
    generation_time_ms = None
    if self._start_time is not None:
        generation_time_ms = int((time.time() - self._start_time) * 1000)

    # Create project analysis
    file_types = set()
    total_files = 0

    for analysis in self._directory_cache.values():
        file_types.update(analysis.file_types)
        total_files += analysis.total_files

    # Check for constitution
    from ..constitution import find_constitution

    constitution_path = find_constitution(Path(self.base_dir))
    constitution_detected = constitution_path.exists()

    project_analysis = ProjectAnalysis(
        directories_scanned=len(self._directory_cache),
        files_analyzed=total_files,
        file_types_detected=file_types,
        instruction_patterns_detected=len(self._optimization_decisions),
        max_depth=max((a.depth for a in self._directory_cache.values()), default=0),
        constitution_detected=constitution_detected,
        constitution_path=portable_relpath(constitution_path, self.base_dir)
        if constitution_detected
        else None,
    )

    # Create placement summaries
    placement_summaries = []

    # Special case: if no instructions but constitution exists, create root placement
    if not placement_map and constitution_detected:
        # Create a root placement for constitution-only projects
        root_sources = {"constitution.md"}
        summary = PlacementSummary(
            path=Path(self.base_dir),
            instruction_count=0,
            source_count=len(root_sources),
            sources=list(root_sources),
        )
        placement_summaries.append(summary)
    else:
        # Normal case: create summaries for each placement in the map
        for directory, instructions in placement_map.items():
            # Count unique sources
            sources = set()
            for instruction in instructions:
                if hasattr(instruction, "source_file") and instruction.source_file:
                    sources.add(instruction.source_file)
                elif hasattr(instruction, "source") and instruction.source:
                    sources.add(str(instruction.source))

            # Add constitution as a source if it exists and will be injected
            if constitution_detected:
                sources.add("constitution.md")

            summary = PlacementSummary(
                path=directory,
                instruction_count=len(instructions),
                source_count=len(sources),
                sources=list(sources),
            )
            placement_summaries.append(summary)

    # Get optimization statistics
    optimization_stats = self.get_optimization_stats(placement_map)
    optimization_stats.generation_time_ms = generation_time_ms

    return CompilationResults(
        project_analysis=project_analysis,
        optimization_decisions=self._optimization_decisions.copy(),
        placement_summaries=placement_summaries,
        optimization_stats=optimization_stats,
        warnings=self._warnings.copy(),
        errors=self._errors.copy(),
        is_dry_run=is_dry_run,
    )


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
    import re

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


def _calculate_inheritance_pollution(self, directory: Path, pattern: str) -> float:
    """Calculate inheritance pollution score for placing instruction at directory.

    Args:
        directory (Path): Candidate placement directory.
        pattern (str): Instruction pattern.

    Returns:
        float: Pollution score (higher = more pollution).
    """
    pollution_score = 0.0

    # Optimization: Only check direct children instead of all directories
    # This prevents O(n2) complexity with unlimited depth analysis
    try:
        direct_children = [
            child
            for child in directory.iterdir()
            if child.is_dir() and child in self._directory_cache
        ]

        # Check only direct child directories for pollution
        for child_dir in direct_children:
            analysis = self._directory_cache[child_dir]

            # If child has no matching files, this creates pollution
            child_relevance = analysis.get_relevance_score(pattern)
            if child_relevance == 0.0:
                pollution_score += 0.5  # Strong pollution penalty
            elif child_relevance < 0.1:  # Weak relevance threshold
                pollution_score += 0.2  # Weak pollution penalty
    except (OSError, PermissionError):
        # Skip directories we can't read
        pass

    return pollution_score


def _calculate_distribution_score(self, matching_directories: builtins.set[Path]) -> float:
    """Calculate distribution score with diversity factor.

    Args:
        matching_directories: Set of directories with pattern matches.

    Returns:
        float: Distribution score accounting for spread and depth diversity.
    """
    total_dirs_with_files = len([d for d in self._directory_cache.values() if d.total_files > 0])
    if total_dirs_with_files == 0:
        return 0.0

    base_ratio = len(matching_directories) / total_dirs_with_files

    # Calculate diversity factor based on depth distribution
    depths = [self._directory_cache[d].depth for d in matching_directories]
    if not depths:
        return base_ratio

    depth_variance = sum((d - sum(depths) / len(depths)) ** 2 for d in depths) / len(depths)
    diversity_factor = 1.0 + (depth_variance * self.DIVERSITY_FACTOR_BASE)

    return base_ratio * diversity_factor


def _calculate_hierarchical_coverage(
    self, placements: builtins.list[Path], target_directories: builtins.set[Path]
) -> builtins.set[Path]:
    """Calculate which target directories are covered by the given placements through hierarchical inheritance.

    Args:
        placements: List of directories where AGENTS.md files will be placed
        target_directories: Directories that need to be covered

    Returns:
        Set of target directories that are covered by the placements
    """
    covered = set()

    for target in target_directories:
        for placement in placements:
            if self._is_hierarchically_covered(target, placement):
                covered.add(target)
                break

    return covered


def _is_hierarchically_covered(self, target_dir: Path, placement_dir: Path) -> bool:
    """Check if target_dir can inherit instructions from placement_dir through hierarchy.

    This is true if placement_dir is target_dir itself or any parent of target_dir.
    """
    try:
        # Check if target is the same as placement or is a subdirectory of placement
        target_dir.resolve().relative_to(placement_dir.resolve())
        return True
    except ValueError:
        # target_dir is not under placement_dir
        return False


def _calculate_coverage_efficiency(self, directory: Path, pattern: str) -> float:
    """Calculate how well placement covers actual usage."""
    analysis = self._directory_cache[directory]
    return analysis.get_relevance_score(pattern)


def _calculate_pollution_minimization(self, directory: Path, pattern: str) -> float:
    """Calculate pollution score (higher = more pollution)."""
    return self._calculate_inheritance_pollution(directory, pattern)


def _calculate_maintenance_locality(self, directory: Path, pattern: str) -> float:
    """Calculate maintenance locality score."""
    # Simple heuristic: prefer directories with more related files
    analysis = self._directory_cache[directory]
    pattern_matches = analysis.pattern_matches.get(pattern, 0)

    if analysis.total_files == 0:
        return 0.0

    return min(1.0, pattern_matches / analysis.total_files)


def _get_inheritance_chain(self, working_directory: Path) -> builtins.list[Path]:
    """Get inheritance chain from working directory to root.

    Args:
        working_directory (Path): Starting directory.

    Returns:
        List[Path]: Inheritance chain (most specific to root).
    """
    cached = self._inheritance_cache.get(working_directory)
    if cached is not None:
        return cached

    chain = []
    # Resolve the starting directory to ensure consistent path comparison
    try:
        current = working_directory.resolve()
    except (OSError, ValueError):
        current = working_directory.absolute()

    seen_paths = set()  # Track visited paths to prevent infinite loops

    # Build chain from working directory up to (and including) base_dir
    while current not in seen_paths:
        seen_paths.add(current)
        chain.append(current)

        # Stop at base_dir
        if current == self.base_dir:
            break

        # Stop if we can't go higher or hit filesystem root
        try:
            parent = current.parent
            if parent == current:  # We've hit filesystem root
                break
            current = parent
        except (OSError, ValueError):
            break

    self._inheritance_cache[working_directory] = chain
    return chain


def _is_child_directory(self, child: Path, parent: Path) -> bool:
    """Check if child is a subdirectory of parent.

    Args:
        child (Path): Potential child directory.
        parent (Path): Potential parent directory.

    Returns:
        bool: True if child is subdirectory of parent.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return child.resolve() != parent.resolve()
    except ValueError:
        return False


def _is_instruction_relevant(self, instruction: Instruction, working_directory: Path) -> bool:
    """Check if instruction is relevant for the working directory.

    Args:
        instruction (Instruction): Instruction to check.
        working_directory (Path): Directory where agent is working.

    Returns:
        bool: True if instruction is relevant.
    """
    if not instruction.apply_to:
        return True  # Global instructions are always relevant

    pattern = instruction.apply_to

    # Resolve working directory to handle path inconsistencies
    try:
        resolved_working_dir = working_directory.resolve()
    except (OSError, ValueError):
        resolved_working_dir = working_directory.absolute()

    # Check if working directory has files matching the pattern
    analysis = self._directory_cache.get(resolved_working_dir)
    if not analysis:
        return False

    # If pattern already analyzed, use cached result
    if pattern in analysis.pattern_matches:
        return analysis.pattern_matches[pattern] > 0

    # Otherwise, analyze this specific directory for the pattern
    # Only check direct files in this directory (not subdirectories for simplicity)
    matching_files = 0

    try:
        for file in os.listdir(resolved_working_dir):
            if file.startswith("."):
                continue

            file_path = resolved_working_dir / file
            if file_path.is_file():
                if self._file_matches_pattern(file_path, pattern):
                    matching_files += 1
    except (OSError, PermissionError):
        # Handle case where directory doesn't exist or can't be read
        pass

    # Cache the result
    analysis.pattern_matches[pattern] = matching_files

    return matching_files > 0
