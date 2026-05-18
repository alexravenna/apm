"""materialize_from_bare: per-consumer working-tree checkout from a shared bare."""

from __future__ import annotations

import subprocess
from pathlib import Path


def materialize_from_bare(
    bare_path: Path,
    consumer_dir: Path,
    *,
    ref: str | None,
    env: dict[str, str],
    known_sha: str | None = None,
) -> str:
    """Create a working-tree checkout from a bare repo via local-shared clone.

    Mirrors :class:`GitCache`'s ``_create_checkout`` pattern: each
    consumer gets its own working tree backed by the shared bare's
    object database (via ``objects/info/alternates``). Hardlink-cheap
    and concurrent-safe (consumer dirs are unique per call).

    SHA resolution policy (lifetime invariant 5.2.1):
      - If ``known_sha`` is provided (caller passed a 40-char SHA
        ref), use it directly. Avoids ``rev-parse HEAD`` which is
        ambiguous on init+fetch bares before update-ref runs.
      - Otherwise, resolve from the BARE via ``git --git-dir
        <bare> rev-parse HEAD``. NOT from the consumer - opening
        ``Repo(consumer_dir)`` leaks a Windows file handle that
        blocks downstream rmtree.

    CRLF + LFS pinning before checkout:
      - ``core.autocrlf=false`` guarantees byte-identical content
        across consumers regardless of the user's global git config.
      - ``filter.lfs.smudge=""`` + ``filter.lfs.required=false``
        disables LFS smudge cross-platform (the empty string trick
        works everywhere; ``cat`` is not on Windows PATH).

    Returns:
        The resolved commit SHA. Caller threads this into
        ``resolved_commit`` for the lockfile.
    """
    from ...utils.git_env import get_git_executable

    git_exe = get_git_executable()

    if known_sha:
        resolved_sha = known_sha
    else:
        sha_result = subprocess.run(
            [git_exe, "--git-dir", str(bare_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=True,
        )
        resolved_sha = sha_result.stdout.strip()

    consumer_dir.parent.mkdir(parents=True, exist_ok=True)
    # --no-checkout because we want to set core.autocrlf and disable
    # LFS smudge BEFORE checkout writes any file content.
    subprocess.run(
        [
            git_exe,
            "clone",
            "--local",
            "--shared",
            "--no-checkout",
            str(bare_path),
            str(consumer_dir),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    # CRLF determinism (panel: byte-identical across users).
    subprocess.run(
        [git_exe, "-C", str(consumer_dir), "config", "core.autocrlf", "false"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=True,
    )
    # Disable LFS smudge cross-platform: empty-string smudge is the
    # portable equivalent of `git lfs smudge --skip`. The `cat`
    # alternative is not on Windows PATH.
    for key, val in (
        ("filter.lfs.smudge", ""),
        ("filter.lfs.clean", ""),
        ("filter.lfs.process", ""),
        ("filter.lfs.required", "false"),
    ):
        subprocess.run(
            [git_exe, "-C", str(consumer_dir), "config", key, val],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    checkout_target = known_sha or "HEAD"
    subprocess.run(
        [git_exe, "-C", str(consumer_dir), "checkout", checkout_target],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    return resolved_sha
