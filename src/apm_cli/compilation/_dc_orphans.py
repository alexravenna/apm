"""Orphaned AGENTS.md file detection and cleanup utilities.

Extracted from ``distributed_compiler`` to keep that module ≤500 lines.
These helpers are standalone functions that accept ``base_dir`` as an
explicit parameter instead of relying on compiler instance state.
"""

import builtins
from pathlib import Path

from ..utils.paths import portable_relpath


def find_orphaned_agents_files(
    base_dir: Path,
    generated_paths: builtins.list[Path],
) -> builtins.list[Path]:
    """Find existing AGENTS.md files that weren't generated in the current compilation.

    Args:
        base_dir: Project root directory to search in.
        generated_paths: AGENTS.md files generated in the current run.

    Returns:
        List of orphaned AGENTS.md files that should be cleaned up.
    """
    orphaned_files = []
    generated_set = set(generated_paths)

    # Find all existing AGENTS.md files in the project
    for agents_file in base_dir.rglob("AGENTS.md"):
        # Skip files that are outside our project or in special directories
        try:
            relative_path = agents_file.resolve().relative_to(base_dir.resolve())

            # Skip files in certain directories that shouldn't be cleaned
            skip_dirs = {
                ".git",
                ".apm",
                "node_modules",
                "__pycache__",
                ".pytest_cache",
                "apm_modules",
            }
            if any(part in skip_dirs for part in relative_path.parts):
                continue

            # If this existing file wasn't generated in current run, it's orphaned
            if agents_file not in generated_set:
                orphaned_files.append(agents_file)

        except ValueError:
            # File is outside base_dir, skip it
            continue

    return orphaned_files


def generate_orphan_warnings(
    orphaned_files: builtins.list[Path],
    base_dir: Path,
) -> builtins.list[str]:
    """Generate warning messages for orphaned AGENTS.md files.

    Args:
        orphaned_files: List of orphaned files to warn about.
        base_dir: Project root directory (for relative path display).

    Returns:
        List of warning messages.
    """
    warning_messages = []

    if not orphaned_files:
        return warning_messages

    # Professional warning format with readable list for multiple files
    if len(orphaned_files) == 1:
        rel_path = portable_relpath(orphaned_files[0], base_dir)
        warning_messages.append(
            f"Orphaned AGENTS.md found: {rel_path} - run 'apm compile --clean' to remove"
        )
    else:
        # For multiple files, create a single multi-line warning message
        file_list = []
        for file_path in orphaned_files[:5]:  # Show first 5
            rel_path = portable_relpath(file_path, base_dir)
            file_list.append(f"  * {rel_path}")
        if len(orphaned_files) > 5:
            file_list.append(f"  * ...and {len(orphaned_files) - 5} more")

        # Create one cohesive warning message
        files_text = "\n".join(file_list)
        warning_messages.append(
            f"Found {len(orphaned_files)} orphaned AGENTS.md files:\n{files_text}\n"
            "  Run 'apm compile --clean' to remove orphaned files"
        )

    return warning_messages


def cleanup_orphaned_files(
    orphaned_files: builtins.list[Path],
    base_dir: Path,
    dry_run: bool = False,
) -> builtins.list[str]:
    """Remove orphaned AGENTS.md files.

    Args:
        orphaned_files: List of orphaned files to remove.
        base_dir: Project root directory (for relative path display).
        dry_run: If True, don't actually remove files, just report what would be removed.

    Returns:
        List of cleanup status messages.
    """
    cleanup_messages = []

    if not orphaned_files:
        return cleanup_messages

    if dry_run:
        # In dry-run mode, just report what would be cleaned
        cleanup_messages.append(f"Would clean up {len(orphaned_files)} orphaned AGENTS.md files")
        for file_path in orphaned_files:
            rel_path = portable_relpath(file_path, base_dir)
            cleanup_messages.append(f"  * {rel_path}")
    else:
        # Actually perform the cleanup
        cleanup_messages.append(f"Cleaning up {len(orphaned_files)} orphaned AGENTS.md files")
        for file_path in orphaned_files:
            try:
                rel_path = portable_relpath(file_path, base_dir)
                file_path.unlink()
                cleanup_messages.append(f"  + Removed {rel_path}")
            except Exception as e:
                cleanup_messages.append(f"  x Failed to remove {rel_path}: {e!s}")

    return cleanup_messages
