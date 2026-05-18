"""File-removal sync helpers for BaseIntegrator.

Extracted from :mod:`apm_cli.integration.base_integrator` to keep
that module under the 500-line ceiling while preserving all behaviour.

``BaseIntegrator`` re-exports these as thin ``@staticmethod`` wrappers
so all call-sites remain unchanged.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.utils.console import _rich_warning


def cleanup_empty_parents(
    deleted_paths: list[Path],
    stop_at: Path,
) -> None:
    """Remove empty parent directories in a single bottom-up pass.

    Collects all parent directories of *deleted_paths*, sorts by
    depth descending, and removes each if empty -- O(H+D) syscalls
    instead of the per-file O(HxD) approach.

    Args:
        deleted_paths: Paths that were deleted (files or dirs).
        stop_at: Do not remove this directory or any ancestor.
    """
    if not deleted_paths:
        return
    stop_resolved = stop_at.resolve()
    # Collect unique parents (skip stop_at itself)
    candidates: set = set()
    for p in deleted_paths:
        parent = p.parent
        while parent != stop_at and parent.resolve() != stop_resolved:
            candidates.add(parent)
            parent = parent.parent
    # Sort deepest-first for safe bottom-up removal
    for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def sync_remove_files(
    project_root: Path,
    managed_files: set[str] | None,
    prefix: str,
    legacy_glob_dir: Path | None = None,
    legacy_glob_pattern: str | None = None,
    targets=None,
    logger=None,
    _warn_fn=None,
) -> dict[str, int]:
    """Remove APM-managed files matching *prefix* from *managed_files*.

    Falls back to a legacy glob when *managed_files* is ``None``.

    Args:
        project_root: Workspace root.
        managed_files: Set of workspace-relative paths.
        prefix: Only process paths that start with this prefix
                (e.g. ``".github/prompts/"``).
        legacy_glob_dir: Directory to glob inside for the legacy fallback.
        legacy_glob_pattern: Glob pattern for legacy fallback
                             (e.g. ``"*-apm.prompt.md"``).
        targets: Optional target profiles for path validation.
                 Passed through to ``validate_deploy_path()`` so
                 user-scope prefixes are recognised.
        logger: Optional logger for diagnostic messages.
        _warn_fn: Optional callable used instead of ``_rich_warning`` when
                  ``logger`` is ``None``.  Injected by
                  ``BaseIntegrator.sync_remove_files`` so that test patches
                  on ``apm_cli.integration.base_integrator._rich_warning``
                  propagate correctly into this helper.

    Returns:
        ``{"files_removed": int, "errors": int}``
    """
    # Import here to avoid a circular dependency at module load time;
    # validate_deploy_path lives on BaseIntegrator which imports _sync.
    from apm_cli.integration.base_integrator import BaseIntegrator

    if _warn_fn is None:
        _warn_fn = _rich_warning

    stats: dict[str, int] = {"files_removed": 0, "errors": 0}

    if managed_files is not None:
        # Lazy-resolve cowork root at most once per invocation.
        _cowork_root_resolved: bool = False
        _cowork_root_cached: Path | None = None
        _cowork_orphans_skipped: int = 0

        for rel_path in managed_files:
            # managed_files is pre-normalized  -- no .replace() needed
            if not rel_path.startswith(prefix):
                continue
            if not BaseIntegrator.validate_deploy_path(rel_path, project_root, targets=targets):
                continue
            # Resolve cowork:// paths to absolute before filesystem ops.
            from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

            if rel_path.startswith(COWORK_URI_SCHEME):
                try:
                    if not _cowork_root_resolved:
                        from apm_cli.integration.copilot_cowork_paths import (
                            resolve_copilot_cowork_skills_dir,
                        )

                        _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                        _cowork_root_resolved = True
                    if _cowork_root_cached is None:
                        _cowork_orphans_skipped += 1
                        continue
                    from apm_cli.integration.copilot_cowork_paths import (
                        from_lockfile_path,
                    )

                    target = from_lockfile_path(rel_path, _cowork_root_cached)
                except Exception:  # noqa: S112
                    continue
            else:
                target = project_root / rel_path
            if target.exists():
                try:
                    target.unlink()
                    stats["files_removed"] += 1
                except Exception:
                    stats["errors"] += 1

        # Emit a one-time warning when cowork orphans were skipped.
        if _cowork_orphans_skipped > 0:
            _orphan_msg = (
                f"Cowork: skipping {_cowork_orphans_skipped} orphaned lockfile "
                f"{'entry' if _cowork_orphans_skipped == 1 else 'entries'}"
                " -- OneDrive path not detected.\n"
                "Run: apm config set copilot-cowork-skills-dir <path>  "
                "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                "to clean up these entries on the next install/uninstall."
            )
            if logger:
                logger.warning(_orphan_msg, symbol="warning")
            else:
                _warn_fn(_orphan_msg, symbol="warning")
    elif legacy_glob_dir and legacy_glob_pattern and legacy_glob_dir.exists():
        for f in legacy_glob_dir.glob(legacy_glob_pattern):
            try:
                f.unlink()
                stats["files_removed"] += 1
            except Exception:
                stats["errors"] += 1

    return stats
