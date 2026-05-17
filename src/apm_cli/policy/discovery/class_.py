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
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import ApmPolicy

logger = logging.getLogger(__name__)


def _split_hash_pin(expected_hash: str) -> tuple[str, str]:
    return _hash_verify_mod._split_hash_pin(expected_hash)


def _compute_hash_normalized(content: str, expected_hash: str | None) -> str:
    return _hash_verify_mod._compute_hash_normalized(content, expected_hash)


def _verify_hash_pin(
    content: object, expected_hash: str | None, source_label: str
) -> PolicyFetchResult | None:
    return _hash_verify_mod._verify_hash_pin(content, expected_hash, source_label)


# Cache location: apm_modules/.policy-cache/<hash>.yml + <hash>.meta.json
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


@dataclass
class PolicyFetchResult:
    """Result of a policy fetch attempt.

    The ``outcome`` field discriminates the 9 discovery outcomes defined in
    the plan (section B):

    * ``found``               -- valid policy, enforce per ``enforcement``
    * ``absent``              -- no policy published (404 / empty repo)
    * ``cached_stale``        -- served from cache past TTL on refresh failure
    * ``cache_miss_fetch_fail`` -- no cache, fetch failed
    * ``malformed``           -- YAML valid but schema invalid (fail-closed)
    * ``disabled``            -- ``--no-policy`` / ``APM_POLICY_DISABLE=1``
    * ``garbage_response``    -- 200 OK but body is not valid YAML
    * ``no_git_remote``       -- cannot determine org from git remote
    * ``empty``               -- valid policy with no actionable rules
    * ``hash_mismatch``       -- ``policy.hash`` pin in apm.yml does not match
                                 the fetched policy bytes (always fail-closed)
    """

    policy: ApmPolicy | None = None
    source: str = ""  # "org:contoso/.github", "file:/path", "url:https://..."
    cached: bool = False  # True if served from cache
    error: str | None = None  # Error message if fetch failed

    # -- Outcome-matrix fields (W1-cache-redesign) --
    cache_age_seconds: int | None = None  # Age of cache entry in seconds
    cache_stale: bool = False  # True if cache was served past TTL
    fetch_error: str | None = None  # Network/parse error on refresh attempt
    outcome: str = ""  # See docstring for valid values

    # -- Hash-pin fields (#827 supply-chain hardening) --
    # raw_bytes_hash is the digest of the leaf policy bytes off the wire,
    # in canonical "<algo>:<hex>" form. Persisted to the cache so subsequent
    # cached reads can verify against the project's pin without re-fetching.
    raw_bytes_hash: str | None = None
    expected_hash: str | None = None  # The pin that was checked, if any

    @property
    def found(self) -> bool:
        return self.policy is not None


def discover_policy_with_chain(
    project_root: Path, *, expected_hash: str | None = None
) -> PolicyFetchResult:
    return _chain_mod.discover_policy_with_chain(project_root, expected_hash=expected_hash)


def _strip_source_prefix(src: str) -> str:
    return _strip_prefix_mod._strip_source_prefix(src)


def _derive_leaf_host(source: str, project_root: Path) -> str | None:
    return _strip_prefix_mod._derive_leaf_host(source, project_root)


def _extract_extends_host(ref: str) -> str | None:
    return _extends_host_mod._extract_extends_host(ref)


def _validate_extends_host(leaf_host: str | None, extends_ref: str) -> None:
    return _extends_host_mod._validate_extends_host(leaf_host, extends_ref)


def _resolve_and_persist_chain(fetch_result: PolicyFetchResult, project_root: Path) -> None:
    return _chain_mod._resolve_and_persist_chain(fetch_result, project_root)


def discover_policy(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    return _chain_mod.discover_policy(
        project_root,
        policy_override=policy_override,
        no_cache=no_cache,
        expected_hash=expected_hash,
    )


def _load_from_file(path: Path, *, expected_hash: str | None = None) -> PolicyFetchResult:
    return _chain_mod._load_from_file(path, expected_hash=expected_hash)


def _auto_discover(
    project_root: Path, *, no_cache: bool = False, expected_hash: str | None = None
) -> PolicyFetchResult:
    return _auto_discover_mod._auto_discover(
        project_root, no_cache=no_cache, expected_hash=expected_hash
    )


def _extract_org_from_git_remote(project_root: Path) -> tuple[str, str] | None:
    return _auto_discover_mod._extract_org_from_git_remote(project_root)


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    return _auto_discover_mod._parse_remote_url(url)


def _fetch_from_url(
    url: str, project_root: Path, *, no_cache: bool = False, expected_hash: str | None = None
) -> PolicyFetchResult:
    return _fetch_url_mod._fetch_from_url(
        url, project_root, no_cache=no_cache, expected_hash=expected_hash
    )


def _fetch_from_repo(
    repo_ref: str, project_root: Path, *, no_cache: bool = False, expected_hash: str | None = None
) -> PolicyFetchResult:
    return _fetch_url_mod._fetch_from_repo(
        repo_ref, project_root, no_cache=no_cache, expected_hash=expected_hash
    )


def _fetch_github_contents(repo_ref: str, file_path: str) -> tuple[str | None, str | None]:
    return _fetch_url_mod._fetch_github_contents(repo_ref, file_path)


def _is_github_host(host: str) -> bool:
    return _github_token_mod._is_github_host(host)


def _get_token_for_host(host: str) -> str | None:
    return _github_token_mod._get_token_for_host(host)


# -- Cache ----------------------------------------------------------


@dataclass
class _CacheEntry:
    """Internal representation of a cached policy read."""

    policy: ApmPolicy
    source: str
    age_seconds: int
    stale: bool  # True if past TTL (but within MAX_STALE_TTL)
    chain_refs: list[str] = field(default_factory=list)
    fingerprint: str = ""
    raw_bytes_hash: str = ""  # "<algo>:<hex>" of leaf bytes off wire (#827)


def _get_cache_dir(project_root: Path) -> Path:
    return _cache_mod._get_cache_dir(project_root)


def _cache_key(repo_ref: str) -> str:
    return _cache_mod._cache_key(repo_ref)


def _policy_to_dict(policy: ApmPolicy) -> dict:
    return _serialize_mod._policy_to_dict(policy)


def _serialize_policy(policy: ApmPolicy) -> str:
    return _serialize_mod._serialize_policy(policy)


def _policy_fingerprint(serialized: str) -> str:
    return _serialize_mod._policy_fingerprint(serialized)


def _is_policy_empty(policy: ApmPolicy) -> bool:
    return _cache_mod._is_policy_empty(policy)


def _stale_fallback_or_error(
    cache_entry: _CacheEntry | None, fetch_error_msg: str, source_label: str, outcome_on_miss: str
) -> PolicyFetchResult:
    return _cache_mod._stale_fallback_or_error(
        cache_entry, fetch_error_msg, source_label, outcome_on_miss
    )


def _detect_garbage(
    content: str | None, identifier: str, source_label: str, cache_entry: _CacheEntry | None
) -> PolicyFetchResult | None:
    return _cache_mod._detect_garbage(content, identifier, source_label, cache_entry)


def _read_cache_entry(
    repo_ref: str,
    project_root: Path,
    ttl: int = DEFAULT_CACHE_TTL,
    *,
    expected_hash: str | None = None,
) -> _CacheEntry | None:
    return _cache_mod._read_cache_entry(repo_ref, project_root, ttl, expected_hash=expected_hash)


def _read_cache(
    repo_ref: str, project_root: Path, ttl: int = DEFAULT_CACHE_TTL
) -> PolicyFetchResult | None:
    return _cache_mod._read_cache(repo_ref, project_root, ttl)


def _write_cache(
    repo_ref: str,
    policy: ApmPolicy,
    project_root: Path,
    *,
    chain_refs: list[str] | None = None,
    raw_bytes_hash: str | None = None,
) -> None:
    return _cache_mod._write_cache(
        repo_ref, policy, project_root, chain_refs=chain_refs, raw_bytes_hash=raw_bytes_hash
    )


from . import auto_discover as _auto_discover_mod
from . import cache as _cache_mod
from . import chain as _chain_mod
from . import extends_host as _extends_host_mod
from . import fetch_url as _fetch_url_mod
from . import github_token as _github_token_mod
from . import hash_verify as _hash_verify_mod
from . import serialize as _serialize_mod
from . import strip_prefix as _strip_prefix_mod
