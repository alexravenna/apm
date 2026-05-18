"""Private git helpers shared within the cache package.

Contains:
- ``_SHA_RE``            – 40-hex-char commit-SHA pattern
- ``_sanitize_url``      – strip credentials from a URL for safe logging
- ``_ls_remote_resolve`` – resolve a git ref to a full SHA via ``git ls-remote``
- ``_dir_size``          – recursive directory size (symlink-safe)

These were extracted from :mod:`apm_cli.cache.git_cache` to keep that
module within the 500-line budget while preserving all public behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Full SHA pattern: 40 hex characters
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _sanitize_url(url: str) -> str:
    """Strip credentials from URL for safe logging."""
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.password:
            # Replace password with ***
            netloc = parsed.hostname or ""
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url


def _ls_remote_resolve(
    url: str,
    ref: str | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Resolve a ref to SHA via git ls-remote.

    Args:
        url: Repository URL.
        ref: Ref to resolve (branch, tag, or None for HEAD).
        env: Environment for subprocess.

    Returns:
        40-char lowercase hex SHA.

    Raises:
        RuntimeError: If resolution fails.
    """
    from ..utils.git_env import get_git_executable, git_subprocess_env

    git_exe = get_git_executable()
    # auth-delegated: cache-layer ref resolution runs after lockfile
    # already pinned the commit; no PAT->bearer fallback applies here
    # (env is sanitized, no embedded creds).
    cmd = [git_exe, "ls-remote", url]
    if ref:
        cmd.append(ref)

    subprocess_env = env if env is not None else git_subprocess_env()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=subprocess_env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            f"Failed to resolve ref '{ref}' for {_sanitize_url(url)}: {exc}"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-remote failed for {_sanitize_url(url)}: {result.stderr.strip()}"
        )

    # Parse ls-remote output: first column is SHA
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) >= 1 and _SHA_RE.match(parts[0]):
            sha = parts[0].lower()
            # If no ref specified, return HEAD (first line)
            if not ref:
                return sha
            # Match exact ref or refs/heads/ref or refs/tags/ref
            if len(parts) == 2:
                remote_ref = parts[1]
                if remote_ref in (
                    ref,
                    f"refs/heads/{ref}",
                    f"refs/tags/{ref}",
                ):
                    return sha
    # If we have any SHA from output, use the first one
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) >= 1 and _SHA_RE.match(parts[0]):
            return parts[0].lower()

    raise RuntimeError(f"Could not resolve ref '{ref}' for {_sanitize_url(url)}")


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory (non-recursive symlink-safe)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                    total += st.st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
