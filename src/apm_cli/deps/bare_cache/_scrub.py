"""Bare-cache scrub helpers: rmtree and remote-URL/FETCH_HEAD redaction."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)


def _rmtree(path: Path) -> None:
    """Remove a directory tree, handling read-only files and brief Windows locks.

    Delegates to :func:`robust_rmtree` which retries with exponential backoff
    on transient lock errors and chmod-resets read-only ``.git/objects/pack``
    files (Windows portability finding from the #1126 paper audit).
    Duplicated from :mod:`github_downloader` to avoid a circular import
    (``github_downloader`` imports from this module).
    """
    from ...utils.file_ops import robust_rmtree

    robust_rmtree(path, ignore_errors=True)


def _scrub_bare_remote_url(bare_path: Path, git_exe: str, env: dict[str, str]) -> None:
    """Redact ``remote.origin.url`` in a bare repo's ``.git/config``.

    After a successful bare clone, ``remote.origin.url`` retains the
    tokenized URL (e.g. ``https://oauth2:<TOKEN>@github.com/...``). The
    bare is read-only after this point in the WS2 dedup pipeline (no
    further fetches), so the URL is dead weight that just persists the
    token on disk. Replace with ``redacted://`` to eliminate the
    on-disk token footprint.

    Defense-in-depth: tier-1 (init + remote add + fetch) leaves
    ``FETCH_HEAD`` containing the tokenized URL on disk even after the
    config scrub. Truncate it to empty so the token does not survive
    in any on-disk artifact. Best-effort (non-fatal on OSError).

    Best-effort: ``check=False`` so a config-set failure does not abort
    the clone (the bare is still functionally correct without the
    redaction). Convergent panel finding (auth + supply-chain MAJOR).
    On exception, log at WARNING so token-leak-aware operators have an
    audit trail (supply-chain reviewer follow-up: security mechanisms
    must not fail silently).
    """
    try:
        result = subprocess.run(
            [git_exe, "--git-dir", str(bare_path), "remote", "set-url", "origin", "redacted://"],
            env=env,
            check=False,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            _log.warning(
                "Failed to redact remote URL from bare repo config at %s "
                "(git exit=%d). Tokenized URL may persist on disk until "
                "shared cache cleanup.",
                bare_path,
                result.returncode,
            )
    except Exception as exc:
        _log.warning(
            "Exception while redacting remote URL from bare repo config "
            "at %s: %s. Tokenized URL may persist on disk until shared "
            "cache cleanup.",
            bare_path,
            exc,
        )

    # Defense-in-depth: truncate FETCH_HEAD which retains the tokenized
    # URL after tier-1 init+fetch (supply-chain panel follow-up).
    fetch_head = bare_path / "FETCH_HEAD"
    try:
        if fetch_head.exists():
            fetch_head.write_text("")
    except OSError as exc:
        _log.warning(
            "Failed to truncate FETCH_HEAD at %s: %s. Tokenized URL "
            "may persist on disk until shared cache cleanup.",
            fetch_head,
            exc,
        )
