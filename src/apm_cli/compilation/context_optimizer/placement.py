"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import time
from collections import defaultdict
from pathlib import Path

from ...output.models import (
    OptimizationDecision,
    PlacementStrategy,
)
from ...primitives.models import Instruction
from ...utils.paths import portable_relpath
from .class_ import PlacementCandidate

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


def optimize_instruction_placement(
    self,
    instructions: builtins.list[Instruction],
    verbose: bool = False,
    enable_timing: bool = False,
) -> builtins.dict[Path, builtins.list[Instruction]]:
    """Optimize placement of instructions across directories with performance timing.

    Args:
        instructions (List[Instruction]): Instructions to optimize.
        verbose (bool): Collect verbose analysis data.
        enable_timing (bool): Enable detailed timing measurements.

    Returns:
        Dict[Path, List[Instruction]]: Optimized placement mapping.
    """
    self._start_time = time.time()
    self._timing_enabled = enable_timing
    self._verbose = verbose  # Store verbose mode for timing display

    # Don't show the "timing enabled" message - it's not professional
    if enable_timing and verbose:
        self._compilation_start_time = time.time()

    self.enable_timing(verbose)
    self._optimization_decisions.clear()
    self._warnings.clear()
    self._errors.clear()

    # Phase 1: Analyze project structure
    self._time_phase("Project Analysis", self._analyze_project_structure)

    # Phase 2: Analyze each instruction for optimal placement
    placement_map: builtins.dict[Path, builtins.list[Instruction]] = defaultdict(list)

    def process_instructions():
        for instruction in instructions:
            if not instruction.apply_to:
                # Instructions without patterns go to root
                placement_map[self.base_dir].append(instruction)

                # Record global instruction decision
                # Global instructions have maximum relevance since they apply everywhere
                global_relevance = 1.0

                self._optimization_decisions.append(
                    OptimizationDecision(
                        instruction=instruction,
                        pattern="(global)",
                        matching_directories=1,
                        total_directories=len(self._directory_cache),
                        distribution_score=1.0,
                        strategy=PlacementStrategy.DISTRIBUTED,
                        placement_directories=[self.base_dir],
                        reasoning="Global instruction placed at project root",
                        relevance_score=global_relevance,
                    )
                )
                continue

            optimal_placements = self._find_optimal_placements(instruction, verbose)

            # Add instruction to optimal placement(s)
            for directory in optimal_placements:
                placement_map[directory].append(instruction)

    self._time_phase("Instruction Processing", process_instructions)

    return dict(placement_map)


def _find_optimal_placements(
    self, instruction: Instruction, verbose: bool = False
) -> builtins.list[Path]:
    """Find optimal placement(s) for an instruction using mathematical optimization.

    This implements constraint satisfaction optimization that guarantees every
    instruction gets placed at its mathematically optimal location(s).

    Args:
        instruction (Instruction): Instruction to place.
        verbose (bool): Collect verbose analysis data.

    Returns:
        List[Path]: List of optimal directory placements.
    """
    return self._solve_placement_optimization(instruction, verbose)


def _solve_placement_optimization(
    self, instruction: Instruction, verbose: bool = False
) -> builtins.list[Path]:
    """Mathematical optimization solver for instruction placement.

    Implements the mathematician's objective function:
    minimize: sum(context_pollution x directory_weight)
    subject to: for_all instruction -> exists placement

    Args:
        instruction (Instruction): Instruction to optimize placement for.
        verbose (bool): Collect verbose analysis data.

    Returns:
        List[Path]: Mathematically optimal placement(s).
    """
    pattern = instruction.apply_to

    # Find all directories with matching files
    matching_directories = self._find_matching_directories(pattern)

    if not matching_directories:
        # Smart fallback: Try to place in semantically appropriate directory
        intended_dir = self._extract_intended_directory_from_pattern(pattern)

        if intended_dir:
            # Place in the intended directory (e.g., docs/ for docs/**/*.md)
            placement = intended_dir
            reasoning = f"No matching files found, placed in intended directory '{portable_relpath(intended_dir, self.base_dir)}'"
            self._warnings.append(
                f"Pattern '{pattern}' matches no files - placing in intended directory '{portable_relpath(intended_dir, self.base_dir)}'"
            )
        else:
            # Fallback to root for global patterns
            placement = self.base_dir
            reasoning = "No matching files found, fallback to root placement"
            self._warnings.append(f"Pattern '{pattern}' matches no files - placing at project root")

        # Calculate relevance score for the fallback placement
        relevance_score = 0.0  # No matches means no relevance
        if placement in self._directory_cache:
            relevance_score = self._calculate_coverage_efficiency(placement, pattern)

        decision = OptimizationDecision(
            instruction=instruction,
            pattern=pattern,
            matching_directories=0,
            total_directories=len(self._directory_cache),
            distribution_score=0.0,
            strategy=PlacementStrategy.DISTRIBUTED,
            placement_directories=[placement],
            reasoning=reasoning,
            relevance_score=relevance_score,
        )
        self._optimization_decisions.append(decision)

        return [placement]

    # Calculate distribution score with diversity factor
    distribution_score = self._calculate_distribution_score(matching_directories)

    # Apply three-tier placement strategy based on mathematical analysis
    if distribution_score < self.LOW_DISTRIBUTION_THRESHOLD:
        # Low distribution: Single Point Placement
        strategy = PlacementStrategy.SINGLE_POINT
        placements = self._optimize_single_point_placement(
            matching_directories, instruction, verbose
        )
        reasoning = "Low distribution pattern optimized for minimal pollution"
    elif distribution_score > self.HIGH_DISTRIBUTION_THRESHOLD:
        # High distribution: Distributed Placement
        strategy = PlacementStrategy.DISTRIBUTED
        placements = self._optimize_distributed_placement(
            matching_directories, instruction, verbose
        )
        reasoning = "High distribution pattern placed at root to minimize duplication"
    else:
        # Medium distribution: Selective Multi-Placement
        strategy = PlacementStrategy.SELECTIVE_MULTI
        placements = self._optimize_selective_placement(matching_directories, instruction, verbose)
        reasoning = "Medium distribution pattern with selective high-relevance placement"

    # Calculate relevance score for the primary placement directory
    relevance_score = 0.0
    if placements:
        primary_placement = placements[0]  # Use first placement as representative
        if primary_placement in self._directory_cache:
            relevance_score = self._calculate_coverage_efficiency(primary_placement, pattern)

    # Record optimization decision
    decision = OptimizationDecision(
        instruction=instruction,
        pattern=pattern,
        matching_directories=len(matching_directories),
        total_directories=len(self._directory_cache),
        distribution_score=distribution_score,
        strategy=strategy,
        placement_directories=placements,
        reasoning=reasoning,
        relevance_score=relevance_score,
    )
    self._optimization_decisions.append(decision)

    return placements


def _optimize_single_point_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for low distribution patterns (< 0.3 ratio).

    Strategy: Ensure mandatory coverage constraint first, then optimize for minimal pollution.
    Coverage guarantee takes priority over efficiency optimization.
    """
    candidates = self._generate_all_candidates(matching_directories, instruction)

    if not candidates:
        return [self.base_dir]

    # CRITICAL: Mandatory coverage constraint - filter candidates that provide complete coverage
    coverage_candidates = []
    for candidate in candidates:
        # Verify this placement can provide hierarchical coverage for ALL matching directories
        covered_directories = self._calculate_hierarchical_coverage(
            [candidate.directory], matching_directories
        )
        if covered_directories == matching_directories:
            # This candidate satisfies the mandatory coverage constraint
            coverage_candidates.append(candidate)

    # If no single candidate provides complete coverage, find minimal coverage placement
    if not coverage_candidates:
        minimal_coverage = self._find_minimal_coverage_placement(matching_directories)
        if minimal_coverage:
            return [minimal_coverage]
        else:
            # Ultimate fallback to root to guarantee coverage
            return [self.base_dir]

    # Among coverage-compliant candidates, select the one with best efficiency/pollution ratio
    best_candidate = max(
        coverage_candidates, key=lambda c: c.coverage_efficiency - c.pollution_score
    )

    return [best_candidate.directory]


def _optimize_distributed_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for high distribution patterns (> 0.7 ratio).

    Strategy: Place at root to minimize duplication while maintaining accessibility.
    """
    return [self.base_dir]


def _optimize_selective_placement(
    self,
    matching_directories: builtins.set[Path],
    instruction: Instruction,
    verbose: bool = False,
) -> builtins.list[Path]:
    """Optimize placement for medium distribution patterns (0.3-0.7 ratio).

    Strategy: Ensure hierarchical coverage - all matching files must be able
    to inherit the instruction through the hierarchical AGENTS.md system.
    """
    # First check if we can achieve complete coverage with a single high-level placement
    coverage_placement = self._find_minimal_coverage_placement(matching_directories)
    if coverage_placement:
        return [coverage_placement]

    # If single placement doesn't work, use multi-placement strategy
    candidates = self._generate_all_candidates(matching_directories, instruction)

    if not candidates:
        return [self.base_dir]

    # Filter for high-relevance candidates (top 20% or relevance > 0.8)
    high_relevance_threshold = max(
        0.8,
        sorted([c.coverage_efficiency for c in candidates], reverse=True)[
            max(0, len(candidates) // 5)
        ],
    )

    high_relevance_candidates = [
        c for c in candidates if c.coverage_efficiency >= high_relevance_threshold
    ]

    if not high_relevance_candidates:
        # Fallback: use best candidate
        high_relevance_candidates = [max(candidates, key=lambda c: c.total_score)]

    optimal_placements = [c.directory for c in high_relevance_candidates]

    # CRITICAL: Verify hierarchical coverage
    covered_directories = self._calculate_hierarchical_coverage(
        optimal_placements, matching_directories
    )
    uncovered_directories = matching_directories - covered_directories

    if uncovered_directories:
        # Coverage violation! Find minimal placement that covers everything
        minimal_coverage = self._find_minimal_coverage_placement(matching_directories)
        if minimal_coverage:
            return [minimal_coverage]
        else:
            # Fallback to root to ensure no coverage gaps
            return [self.base_dir]

    return optimal_placements


def _generate_all_candidates(
    self, matching_directories: builtins.set[Path], instruction: Instruction
) -> builtins.list[PlacementCandidate]:
    """Generate all placement candidates with optimization scores.

    This includes both matching directories AND their common ancestors to ensure
    the mandatory coverage constraint can be satisfied.
    """
    candidates = []
    pattern = instruction.apply_to

    # Collect all potential placement directories:
    # 1. The matching directories themselves
    # 2. Their common ancestors (for coverage guarantee)
    potential_directories = set(matching_directories)

    # Add common ancestor directories to ensure coverage options exist
    if len(matching_directories) > 1:
        # Find common ancestors that could provide coverage
        common_ancestor = self._find_minimal_coverage_placement(matching_directories)
        if common_ancestor:
            potential_directories.add(common_ancestor)

        # Also add any intermediate directories in the inheritance chains
        for directory in matching_directories:
            chain = self._get_inheritance_chain(directory)
            # Add intermediate directories that could provide coverage
            for intermediate in chain:
                if intermediate != directory and intermediate in self._directory_cache:
                    potential_directories.add(intermediate)

    # Generate candidates for all potential directories
    for directory in sorted(potential_directories):
        if directory not in self._directory_cache:
            continue

        analysis = self._directory_cache[directory]

        # Calculate the three optimization objectives
        coverage_efficiency = self._calculate_coverage_efficiency(directory, pattern)
        pollution_score = self._calculate_pollution_minimization(directory, pattern)
        maintenance_locality = self._calculate_maintenance_locality(directory, pattern)

        # Apply depth penalty for excessive nesting
        depth_penalty = max(0, (analysis.depth - 3) * self.DEPTH_PENALTY_FACTOR)

        # Calculate total objective function score
        total_score = (
            coverage_efficiency * self.COVERAGE_EFFICIENCY_WEIGHT
            + (1.0 - pollution_score) * self.POLLUTION_MINIMIZATION_WEIGHT
            + maintenance_locality * self.MAINTENANCE_LOCALITY_WEIGHT
            - depth_penalty
        )

        candidate = PlacementCandidate(
            instruction=instruction,
            directory=directory,
            direct_relevance=coverage_efficiency,  # Legacy field
            inheritance_pollution=pollution_score,  # Legacy field
            depth_specificity=analysis.depth * 0.1,  # Legacy field
            total_score=0.0,  # Temporary value, will be overwritten
        )

        # Add new optimization fields
        candidate.coverage_efficiency = coverage_efficiency
        candidate.pollution_score = pollution_score
        candidate.maintenance_locality = maintenance_locality

        # Set the mathematical optimization score (after __post_init__ has run)
        candidate.total_score = total_score

        candidates.append(candidate)

    return candidates


def _find_minimal_coverage_placement(self, matching_directories: builtins.set[Path]) -> Path | None:
    """Find the highest directory that can provide hierarchical coverage for all matching directories.

    Args:
        matching_directories: Directories that contain files matching the pattern

    Returns:
        Path to the minimal covering directory, or None if no single placement works
    """
    if not matching_directories:
        return None

    # Convert to relative paths for easier analysis
    relative_dirs = [d.resolve().relative_to(self.base_dir.resolve()) for d in matching_directories]

    # Find the lowest common ancestor that covers all directories
    if len(relative_dirs) == 1:
        # Single directory - we can place instruction in that directory or any parent
        return list(matching_directories)[0]

    # Find common path prefix for all directories
    common_parts = []
    min_depth = min(len(d.parts) for d in relative_dirs)

    for i in range(min_depth):
        parts_at_level = [d.parts[i] for d in relative_dirs]
        if len(set(parts_at_level)) == 1:
            # All directories share this path component
            common_parts.append(parts_at_level[0])
        else:
            break

    if common_parts:
        # Found common ancestor
        common_ancestor = self.base_dir / Path(*common_parts)
        return common_ancestor
    else:
        # No common ancestor beyond root - place at root
        return self.base_dir


def _select_clean_separation_placements(
    self, candidates: builtins.list[PlacementCandidate], pattern: str
) -> builtins.list[Path]:
    """Select placements that provide clean separation of concerns.

    Args:
        candidates (List[PlacementCandidate]): Sorted placement candidates.
        pattern (str): Instruction pattern.

    Returns:
        List[Path]: List of directories for clean separation.
    """
    # Look for distinct clusters of files
    clusters = []

    for candidate in candidates:
        # Check if this directory is isolated (not a parent/child of others)
        is_isolated = True

        for other in candidates:
            if candidate.directory == other.directory:
                continue

            if self._is_child_directory(
                candidate.directory, other.directory
            ) or self._is_child_directory(other.directory, candidate.directory):
                is_isolated = False
                break

        if is_isolated and candidate.direct_relevance >= 0.1:  # Use fixed threshold
            clusters.append(candidate.directory)

    # If we found clean clusters, use them
    if len(clusters) > 1:
        return clusters

    # Otherwise, return single best placement
    return []
