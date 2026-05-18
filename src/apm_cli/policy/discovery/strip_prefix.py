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
from pathlib import Path
from urllib.parse import urlparse

from .auto_discover import _extract_org_from_git_remote

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def _strip_source_prefix(src: str) -> str:
    """Strip 'org:' / 'url:' / 'file:' prefix from a PolicyFetchResult.source."""
    return src.removeprefix("org:").removeprefix("url:").removeprefix("file:")


def _derive_leaf_host(source: str, project_root: Path) -> str | None:
    """Derive the origin host of the leaf policy.

    The leaf host pins which host an ``extends:`` reference may resolve
    against (Security Finding F1 -- prevents credential leakage to
    attacker-controlled hosts via cross-host extends chains).

    Returns the host in lowercase, or None if it cannot be determined.

    Source forms:
    * ``url:https://<host>/...`` -> ``<host>``
    * ``org:<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>``
    * ``org:<owner>/<repo>`` (2 slash-segments) -> ``github.com`` (default)
    * ``file:<path>`` -> fall back to git remote of *project_root*
    """
    bare = _strip_source_prefix(source) if source else ""

    if source.startswith("url:") or bare.startswith("https://") or bare.startswith("http://"):
        try:
            hostname = urlparse(bare).hostname
        except Exception:
            hostname = None
        return hostname.lower() if hostname else None

    if source.startswith("org:") or (bare and "://" not in bare and bare.count("/") >= 1):
        parts = bare.split("/")
        if len(parts) >= 3:
            return parts[0].lower()
        if len(parts) == 2:
            # owner/repo shorthand defaults to github.com (matches
            # _fetch_github_contents convention).
            return "github.com"

    # File source (or unrecognized): fall back to project's git remote.
    org_and_host = _extract_org_from_git_remote(project_root)
    if org_and_host is not None:
        _, host = org_and_host
        if host:
            return host.lower()
    return None
