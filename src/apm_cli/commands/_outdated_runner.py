"""Parallel and progress-aware runner for ``apm outdated`` dependency checks.

Extracted from ``outdated.py`` to keep that module under the 500-line limit.
All three functions accept a *check_fn* callable so they remain decoupled from
the ``_check_one_dep`` implementation and carry no top-level import back into
``outdated.py`` (avoiding a circular-import).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _check_deps_with_progress(
    checkable, downloader, verbose, parallel_checks, logger_obj, check_fn
):
    """Check all deps with Rich progress bar and optional parallelism.

    Parameters
    ----------
    checkable:
        List of ``LockedDependency`` objects to check.
    downloader:
        ``GitHubPackageDownloader`` instance (or compatible mock).
    verbose:
        Whether verbose output was requested.
    parallel_checks:
        Maximum number of concurrent remote checks (0 = sequential).
    logger_obj:
        A ``CommandLogger`` (or compatible) instance for plain-text progress.
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow`` — typically
        ``_check_one_dep`` from ``outdated.py``.
    """
    rows = []
    total = len(checkable)

    try:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,
        ) as progress:
            if parallel_checks > 0 and total > 1:
                rows = _check_parallel(
                    checkable,
                    downloader,
                    verbose,
                    parallel_checks,
                    progress,
                    check_fn,
                )
            else:
                task_id = progress.add_task(
                    f"Checking {total} dependencies",
                    total=total,
                )
                for dep in checkable:
                    short = dep.get_unique_key().split("/")[-1]
                    progress.update(task_id, description=f"Checking {short}")
                    result = check_fn(dep, downloader, verbose)
                    rows.append(result)
                    progress.advance(task_id)
    except ImportError:
        # No Rich -- plain text feedback
        logger_obj.progress(f"Checking {total} dependencies...")
        if parallel_checks > 0 and total > 1:
            rows = _check_parallel_plain(
                checkable,
                downloader,
                verbose,
                parallel_checks,
                check_fn,
            )
        else:
            for dep in checkable:
                rows.append(check_fn(dep, downloader, verbose))

    return rows


def _check_parallel(checkable, downloader, verbose, max_workers, progress, check_fn):
    """Run checks in parallel with Rich progress display.

    Parameters
    ----------
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Lazy import avoids a circular dependency at module-load time.
    from .outdated import OutdatedRow

    total = len(checkable)
    max_workers = min(max_workers, total)
    overall_id = progress.add_task(
        f"Checking {total} dependencies",
        total=total,
    )

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for dep in checkable:
            short = dep.get_unique_key().split("/")[-1]
            task_id = progress.add_task(f"Checking {short}", total=None)
            fut = executor.submit(check_fn, dep, downloader, verbose)
            futures[fut] = (dep, task_id)

        for fut in as_completed(futures):
            dep, task_id = futures[fut]
            try:
                result = fut.result()
            except Exception:
                pkg = dep.get_unique_key()
                result = OutdatedRow(package=pkg, current="(none)", latest="-", status="unknown")
            results[dep.get_unique_key()] = result
            progress.update(task_id, visible=False)
            progress.advance(overall_id)

    # Preserve original order
    return [results[dep.get_unique_key()] for dep in checkable if dep.get_unique_key() in results]


def _check_parallel_plain(checkable, downloader, verbose, max_workers, check_fn):
    """Run checks in parallel without Rich (plain fallback).

    Parameters
    ----------
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Lazy import avoids a circular dependency at module-load time.
    from .outdated import OutdatedRow

    max_workers = min(max_workers, len(checkable))
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(check_fn, dep, downloader, verbose): dep for dep in checkable}
        for fut in as_completed(futures):
            dep = futures[fut]
            try:
                result = fut.result()
            except Exception:
                pkg = dep.get_unique_key()
                result = OutdatedRow(package=pkg, current="(none)", latest="-", status="unknown")
            results[dep.get_unique_key()] = result

    return [results[dep.get_unique_key()] for dep in checkable if dep.get_unique_key() in results]
