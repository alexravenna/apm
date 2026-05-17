"""Auto-discover and fetch org-level apm-policy.yml files.

Discovery flow:
1. Extract org from git remote (github.com/contoso/my-project -> "contoso")
2. Fetch <org>/.github/apm-policy.yml via GitHub API (Contents API)
3. Resolve inheritance chain via resolve_policy_chain
4. Cache the **merged effective policy** with chain metadata
5. Parse and return ApmPolicy

Supports:
- GitHub.com and GitHub Enterprise (*.ghe.com)
- Manual override via --policy <path|url>
- Cache with TTL (default 1 hour), stale fallback up to MAX_STALE_TTL
- Atomic cache writes (temp file + os.replace)
- Garbage-response detection (200 OK with non-YAML body)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from ...cache.url_normalize import SCP_LIKE_RE
from .class_ import PolicyFetchResult
from .fetch_url import _fetch_from_repo

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"


def _pkg():
    return sys.modules[__package__]


DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def _auto_discover(
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Auto-discover policy from org's .github repo.

    1. Run git remote get-url origin
    2. Parse org from URL
    3. Fetch <org>/.github/apm-policy.yml
    """
    org_and_host = _pkg()._extract_org_from_git_remote(project_root)
    if org_and_host is None:
        return PolicyFetchResult(
            error="Could not determine org from git remote",
            outcome="no_git_remote",
        )

    org, host = org_and_host
    repo_ref = f"{org}/.github"
    if host and host != "github.com":
        repo_ref = f"{host}/{repo_ref}"

    return _pkg()._fetch_from_repo(
        repo_ref, project_root, no_cache=no_cache, expected_hash=expected_hash
    )


def _extract_org_from_git_remote(
    project_root: Path,
) -> tuple[str, str] | None:
    """Extract (org, host) from git remote origin URL.

    Handles:
    - https://github.com/contoso/my-project.git -> ("contoso", "github.com")
    - git@github.com:contoso/my-project.git -> ("contoso", "github.com")
    - https://github.example.com/contoso/my-project.git -> ("contoso", "github.example.com")
    """
    try:
        result = _pkg().subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=project_root,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return _parse_remote_url(result.stdout.strip())
    except (_pkg().subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into (org, host).

    Accepts SCP-style SSH URLs with any username (not just ``git@``), so
    EMU/GHE deployments that use a non-``git`` SSH user
    (e.g. ``enterprise-user@ghe.corp.com:org/repo.git``) parse correctly.
    Also handles Azure DevOps SSH URLs which carry an extra ``v3/``
    path prefix (``git@ssh.dev.azure.com:v3/<org>/<project>/<repo>``).

    Returns None if URL can't be parsed.
    """
    if not url:
        return None

    # SCP-like SSH: <user>@<host>:<path> -- any user, not just `git`.
    # Closes #1159 for non-`git` SSH users (EMU, custom GHE accounts).
    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        host = scp_match.group("host")
        path_part = scp_match.group("path")
        try:
            parts = path_part.rstrip("/").removesuffix(".git").split("/")
            parts = [p for p in parts if p]
            if not parts:
                return None
            # Azure DevOps SSH carries a leading 'v3/' segment that is
            # NOT the org. The org is the second segment.
            if host == "ssh.dev.azure.com" and parts[0] == "v3" and len(parts) >= 2:
                return (parts[1], host)
            return (parts[0], host)
        except (ValueError, IndexError):
            return None

    # HTTPS: https://github.com/owner/repo.git
    # ADO:   https://dev.azure.com/org/project/_git/repo
    if "://" in url:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path_parts = parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/")
            if host and path_parts and path_parts[0]:
                return (path_parts[0], host)
        except Exception:
            return None

    return None
