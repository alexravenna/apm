"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import glob
import os
from pathlib import Path

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


def _cached_glob(self, pattern: str) -> builtins.list[str]:
    """Cache glob results to avoid repeated filesystem scans."""
    if pattern not in self._glob_cache:
        old_cwd = os.getcwd()
        try:
            os.chdir(str(self.base_dir))  # Convert Path to string for os.chdir
            self._glob_cache[pattern] = glob.glob(pattern, recursive=True)
        finally:
            os.chdir(old_cwd)
    return self._glob_cache[pattern]


def _get_all_files(self) -> builtins.list[Path]:
    """Get cached list of all files in project."""
    if self._file_list_cache is None:
        self._file_list_cache = []
        for root, dirs, files in os.walk(self.base_dir):
            # Skip hidden and excluded directories for performance
            # Sort to guarantee deterministic traversal order across filesystems
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and d not in DEFAULT_EXCLUDED_DIRNAMES
            )
            for file in sorted(files):
                if not file.startswith("."):
                    self._file_list_cache.append(Path(root) / file)
    return self._file_list_cache
