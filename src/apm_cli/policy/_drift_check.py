"""Drift check for the baseline CI audit.

Extracted from :mod:`apm_cli.policy.ci_checks` to keep that module within
the line-count budget.  All symbols are re-exported from
``apm_cli.policy.ci_checks``; existing callers need no changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .models import CheckResult

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from ..install.drift import DriftFinding


#: Prefix used in the drift :class:`CheckResult` message when the check is
#: skipped due to a cold cache.  ``audit.py`` imports this to detect the
#: skip case without comparing against a raw string literal.
DRIFT_SKIP_PREFIX = "drift skipped"


def _check_drift(
    project_root: Path,
    lockfile: LockFile,
    targets: Sequence[str] | None = None,
    cache_only: bool = True,
    verbose: bool = False,
) -> tuple[CheckResult, list[DriftFinding]]:
    """Replay the install in a scratch dir and diff against the project.

    Returns the standard :class:`CheckResult` PLUS the list of
    :class:`DriftFinding` instances so callers can render them in the
    output format of their choice (text/json/sarif) without re-running
    the replay.

    Cache-only by default: a missing cache entry skips the check with
    an informational message rather than failing it.  Drift can only
    run once the local cache has been warmed by ``apm install``; until
    then the audit remains non-blocking so CI does not red-mark a
    fresh checkout that has never installed.
    """
    from ..deps.lockfile import get_lockfile_path
    from ..install.drift import (
        CacheMissError,
        CheckLogger,
        ReplayConfig,
        diff_scratch_against_project,
        run_replay,
    )
    from ..integration.targets import resolve_targets

    logger = CheckLogger(verbose=verbose)
    config = ReplayConfig(
        project_root=project_root,
        lockfile_path=get_lockfile_path(project_root),
        targets=frozenset(targets) if targets else None,
        cache_only=cache_only,
    )

    try:
        scratch = run_replay(config, logger)
    except CacheMissError:
        return (
            CheckResult(
                name="drift",
                passed=True,
                message=(
                    f"{DRIFT_SKIP_PREFIX}: install cache not populated "
                    "(run 'apm install' first or pass --no-drift)"
                ),
            ),
            [],
        )
    except NotImplementedError as exc:
        return (
            CheckResult(
                name="drift",
                passed=False,
                message=f"drift replay unsupported: {exc}",
            ),
            [],
        )

    logger.diff_start()
    resolved_targets = resolve_targets(project_root)
    if targets:
        resolved_targets = [t for t in resolved_targets if t.name in set(targets)]
    findings = diff_scratch_against_project(scratch, project_root, lockfile, resolved_targets)

    if not findings:
        logger.clean()
        return (
            CheckResult(
                name="drift",
                passed=True,
                message="no drift detected against lockfile",
            ),
            [],
        )

    logger.findings(len(findings))
    preview = ", ".join(f.path for f in findings[:3])
    suffix = "" if len(findings) <= 3 else f" (+{len(findings) - 3} more)"
    return (
        CheckResult(
            name="drift",
            passed=False,
            message=f"drift detected: {len(findings)} file(s): {preview}{suffix}",
            details=[f"{f.kind}: {f.path}" for f in findings],
        ),
        findings,
    )
