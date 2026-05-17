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
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def _extract_extends_host(ref: str) -> str | None:
    """Return the host an ``extends:`` ref resolves against, if explicit.

    * Full URL -> URL host (lowercase)
    * ``<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>`` (lowercase)
    * ``<owner>/<repo>`` shorthand -> None (intrinsically same-host)
    * ``<org>`` shorthand (no slash) -> None (intrinsically same-host)
    """
    if not ref:
        return None
    if ref.startswith("http://") or ref.startswith("https://"):
        try:
            parsed = urlparse(ref)
            if parsed.hostname:
                return parsed.hostname.lower()
        except Exception:
            return None
        return None
    if "/" not in ref:
        return None
    parts = ref.split("/")
    if len(parts) >= 3:
        return parts[0].lower()
    return None


def _validate_extends_host(leaf_host: str | None, extends_ref: str) -> None:
    """Reject ``extends:`` refs that point at a different host than the leaf.

    Raises :class:`PolicyInheritanceError` (imported lazily to avoid a
    module-level cycle) when the ``extends:`` ref names a host that does
    not match *leaf_host*. Pure shorthand refs (``owner/repo``, ``org``)
    are intrinsically same-host and always pass.

    See Security Finding F1: a malicious org policy author setting
    ``extends: "evil.example.com/org/.github"`` could otherwise route
    ``git credential fill`` against an attacker-controlled host.
    """
    from .. import inheritance as _inheritance_mod

    extends_host = _extract_extends_host(extends_ref)
    if extends_host is None:
        return  # shorthand: intrinsically same-host, allowed.

    if leaf_host is None:
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: <unknown>, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )

    if extends_host != leaf_host.lower():
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: {leaf_host}, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )
