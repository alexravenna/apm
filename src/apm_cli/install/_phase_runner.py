"""Phase runner with verbose-only timing (F6, #1116).

Private helper extracted from :mod:`apm_cli.install.pipeline` to keep
``pipeline.py`` under 500 lines.  Import ``_run_phase`` from
``apm_cli.install.pipeline`` (it is re-exported there) rather than
importing from this module directly.
"""

from __future__ import annotations

import contextlib
import time


def _run_phase(name: str, phase, ctx):
    """Invoke ``phase.run(ctx)`` with verbose-only timing (F6, #1116).

    Returns whatever ``phase.run(ctx)`` returns (most phases return
    ``None``; ``finalize`` returns the :class:`InstallResult`).

    Best-effort: any failure to render the timing line is swallowed so
    it cannot mask the phase's own exception. The phase exception
    propagates after the timing attempt.

    Verbose mode shows ``[i] Phase: <name> -> 1.234s`` so users (and
    CI logs) can locate the phase responsible for a slow install
    without instrumenting individual sources.
    """
    logger = getattr(ctx, "logger", None)
    verbose = bool(getattr(ctx, "verbose", False))
    if not verbose or logger is None:
        return phase.run(ctx)
    started = time.perf_counter()
    try:
        return phase.run(ctx)
    finally:
        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            logger.verbose_detail(f"Phase: {name} -> {elapsed:.3f}s")
