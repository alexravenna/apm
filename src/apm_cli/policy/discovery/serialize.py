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

import hashlib
import logging

import yaml

from ..schema import ApmPolicy

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def _policy_to_dict(policy: ApmPolicy) -> dict:
    """Serialize an ApmPolicy to a dict matching the YAML schema."""

    def _opt_list(val: tuple[str, ...] | None) -> list | None:
        return None if val is None else list(val)

    return {
        "name": policy.name,
        "version": policy.version,
        "enforcement": policy.enforcement,
        "fetch_failure": policy.fetch_failure,
        "cache": {"ttl": policy.cache.ttl},
        "dependencies": {
            "allow": _opt_list(policy.dependencies.allow),
            "deny": _opt_list(policy.dependencies.deny),
            "require": _opt_list(policy.dependencies.require),
            "require_resolution": policy.dependencies.require_resolution,
            "max_depth": policy.dependencies.max_depth,
        },
        "mcp": {
            "allow": _opt_list(policy.mcp.allow),
            "deny": list(policy.mcp.deny),
            "transport": {
                "allow": _opt_list(policy.mcp.transport.allow),
            },
            "self_defined": policy.mcp.self_defined,
            "trust_transitive": policy.mcp.trust_transitive,
        },
        "compilation": {
            "target": {
                "allow": _opt_list(policy.compilation.target.allow),
                "enforce": policy.compilation.target.enforce,
            },
            "strategy": {
                "enforce": policy.compilation.strategy.enforce,
            },
            "source_attribution": policy.compilation.source_attribution,
        },
        "manifest": {
            "required_fields": list(policy.manifest.required_fields),
            "scripts": policy.manifest.scripts,
            "content_types": policy.manifest.content_types,
        },
        "unmanaged_files": {
            "action": policy.unmanaged_files.action,
            "directories": list(policy.unmanaged_files.directories or ()),
        },
    }


def _serialize_policy(policy: ApmPolicy) -> str:
    """Serialize an ApmPolicy to deterministic YAML for caching."""
    return yaml.dump(
        _policy_to_dict(policy), default_flow_style=False, sort_keys=True
    )  # yaml-io-exempt


def _policy_fingerprint(serialized: str) -> str:
    """Compute a fingerprint of a serialized policy."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
