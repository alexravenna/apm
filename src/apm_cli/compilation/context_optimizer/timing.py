"""Context Optimizer for APM distributed compilation system.

This module implements the Context Optimization Engine that minimizes
irrelevant context loaded by agents working in specific directories,
following the Minimal Context Principle.
"""

import builtins
import time

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


def enable_timing(self, verbose: bool = False):
    """Enable performance timing instrumentation."""
    self._timing_enabled = verbose
    self._phase_timings.clear()


def _time_phase(self, phase_name: str, operation_func, *args, **kwargs):
    """Time a phase of optimization and optionally log it."""
    if not self._timing_enabled:
        return operation_func(*args, **kwargs)

    start_time = time.time()
    result = operation_func(*args, **kwargs)
    duration = time.time() - start_time
    self._phase_timings[phase_name] = duration

    # Only show timing in verbose mode with professional formatting
    if self._timing_enabled and hasattr(self, "_verbose") and self._verbose:
        print(f"  {phase_name}: {duration * 1000:.1f}ms")
    return result
