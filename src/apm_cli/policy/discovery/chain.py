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
import os
import sys
from pathlib import Path

from ..parser import PolicyValidationError, load_policy
from ..project_config import (
    ProjectPolicyConfigError,
    read_project_policy_hash_pin,
)
from ..schema import ApmPolicy
from .auto_discover import _auto_discover
from .cache import _is_policy_empty, _write_cache
from .class_ import PolicyFetchResult
from .extends_host import _validate_extends_host
from .fetch_url import _fetch_from_repo, _fetch_from_url
from .hash_verify import _compute_hash_normalized, _verify_hash_pin
from .strip_prefix import _derive_leaf_host, _strip_source_prefix

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"


def _pkg():
    return sys.modules[__package__]


DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def discover_policy_with_chain(
    project_root: Path,
    *,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Discover policy with full inheritance chain resolution.

    This is the **shared entry point** for all command sites that need
    chain-aware policy discovery (gate phase, ``--mcp`` preflight,
    ``--dry-run`` preflight).  It ensures every path resolves the same
    merged effective policy with real ``chain_refs``.

    Parameters
    ----------
    project_root:
        Project root directory (used for git-remote org extraction and cache).
    expected_hash:
        Optional pin in ``"<algo>:<hex>"`` form (sourced from
        ``policy.hash`` in the project's ``apm.yml``). When set, the
        digest of the leaf policy bytes must match exactly; otherwise the
        result outcome is set to ``"hash_mismatch"`` and ``policy`` is
        cleared. The pin applies only to the **leaf** -- parent policies
        in an ``extends:`` chain are the leaf author's responsibility.

    Notes
    -----
    The escape hatch (``--no-policy`` flag, ``APM_POLICY_DISABLE=1``
    env var) is enforced by the **callers** (the install pipeline gate
    and the preflight helpers in ``install_preflight``) **before** this
    function is invoked, so neither needs a ``no_policy`` parameter
    here.  The env-var check below remains as a defence-in-depth so
    third-party callers cannot accidentally bypass the disable switch.

    Returns
    -------
    PolicyFetchResult
        With merged effective policy and real chain_refs when inheritance
        is present.  Outcome follows the 9-outcome matrix (section B).
    """
    # -- Escape hatch (defence-in-depth) -------------------------------
    # The CLI's --no-policy flag is handled by callers; this env-var
    # check stays so third-party use of the API still respects the
    # global disable switch.
    if os.environ.get("APM_POLICY_DISABLE") == "1":
        return PolicyFetchResult(outcome="disabled")

    # -- Resolve project-side hash pin (#827) --------------------------
    # An explicit *expected_hash* argument always wins (test seam, future
    # CLI override). Otherwise fall back to ``policy.hash`` in the
    # project's apm.yml. A malformed pin surfaces as ``hash_mismatch``
    # rather than a crash so install fails closed with a clear error.
    if expected_hash is None:
        try:
            pin = read_project_policy_hash_pin(project_root)
        except ProjectPolicyConfigError as exc:
            return PolicyFetchResult(
                outcome="hash_mismatch",
                source="apm.yml",
                error=f"Invalid policy.hash in apm.yml: {exc}",
            )
        if pin is not None:
            expected_hash = pin.normalized

    # -- Base discovery ------------------------------------------------
    fetch_result = _pkg().discover_policy(project_root, expected_hash=expected_hash)

    # -- Chain resolution if leaf has extends: -------------------------
    if (
        fetch_result.policy is not None
        and fetch_result.outcome in ("found", "cached_stale")
        and fetch_result.policy.extends is not None
        and not fetch_result.cached  # Don't re-resolve if served from cache
    ):
        _resolve_and_persist_chain(fetch_result, project_root)

    return fetch_result


def _resolve_and_persist_chain(
    fetch_result: PolicyFetchResult,
    project_root: Path,
) -> None:
    """Resolve inheritance chain and update cache with merged policy + chain_refs.

    Walks the ``extends:`` chain depth-first, fetching each parent via the
    single-policy ``discover_policy`` (so each fetch still hits the
    well-tested fetch path).  Cycle detection on normalized ``extends:``
    refs and ``MAX_CHAIN_DEPTH`` enforcement protect against runaway or
    self-referential chains.

    Partial-chain policy: if any parent fetch fails, emit a warning via
    ``_rich_warning`` and merge whatever was resolved so far -- never
    silently drop ancestors.

    Mutates *fetch_result*.policy in-place with the merged effective policy.
    Called by :func:`discover_policy_with_chain` -- not intended for direct
    use.
    """
    from ...utils.console import _rich_warning
    from .. import inheritance as _inheritance_mod

    leaf_policy = fetch_result.policy
    leaf_source = fetch_result.source

    # Host pin: extends: refs may only resolve against the leaf's origin
    # host. Prevents credential leakage to attacker-controlled hosts via
    # cross-host extends chains (Security Finding F1).
    leaf_host = _derive_leaf_host(leaf_source, project_root)

    # Ordered ancestors collected as we walk parents.  Built leaf-first
    # for traversal convenience; reversed before merging.
    chain_policies: list[ApmPolicy] = [leaf_policy]
    chain_sources: list[str] = [leaf_source]

    # Track normalized refs we've already followed to break cycles.
    # We seed with the leaf's source so an extends pointing back at the
    # leaf is also detected.
    visited: list[str] = [_strip_source_prefix(leaf_source)] if leaf_source else []

    current = leaf_policy
    partial_warning: tuple[str, int, int] | None = None

    while current.extends:
        next_ref = current.extends

        # Host pin enforcement: must validate BEFORE any fetch so we never
        # call git credential fill against an attacker-controlled host.
        _pkg()._validate_extends_host(leaf_host, next_ref)

        if _inheritance_mod.detect_cycle(visited, next_ref):
            raise _inheritance_mod.PolicyInheritanceError(
                f"Cycle detected in policy extends chain: {' -> '.join(visited)} -> {next_ref}"
            )

        # Depth check: chain_policies already has len() entries; next fetch
        # would push us to len()+1.  resolve_policy_chain enforces this
        # afterwards, but failing here gives a clearer error.
        if len(chain_policies) + 1 > _inheritance_mod.MAX_CHAIN_DEPTH:
            raise _inheritance_mod.PolicyInheritanceError(
                f"Policy chain depth exceeds maximum of "
                f"{_inheritance_mod.MAX_CHAIN_DEPTH} "
                f"(chain: {' -> '.join(visited)} -> {next_ref})"
            )

        parent_result = _pkg().discover_policy(
            project_root,
            policy_override=next_ref,
            no_cache=False,
        )

        if parent_result.policy is None:
            # Parent fetch failed -- merge what we have so far and warn.
            attempted = len(chain_policies) + 1
            resolved = len(chain_policies)
            partial_warning = (next_ref, resolved, attempted)
            break

        chain_policies.append(parent_result.policy)
        chain_sources.append(parent_result.source)
        visited.append(next_ref)
        current = parent_result.policy

    # No actual ancestors fetched -- nothing to merge or re-cache.
    if len(chain_policies) == 1:
        if partial_warning is not None:
            ref, resolved, attempted = partial_warning
            _rich_warning(
                f"Policy chain incomplete: {ref} unreachable, "
                f"using {resolved} of {attempted} policies",
                symbol="warning",
            )
        return

    # Merge in [root, ..., leaf] order.  We collected leaf-first, so reverse.
    ordered = list(reversed(chain_policies))
    ordered_sources = list(reversed(chain_sources))

    try:
        merged = _inheritance_mod.resolve_policy_chain(ordered)
    except _inheritance_mod.PolicyInheritanceError:
        # Re-raise depth errors from the canonical validator so callers
        # see a single consistent error type.
        raise

    chain_refs: list[str] = [_strip_source_prefix(src) for src in ordered_sources if src]

    cache_key = _strip_source_prefix(leaf_source) if leaf_source else ""
    if cache_key:
        _pkg()._write_cache(cache_key, merged, project_root, chain_refs=chain_refs)

    fetch_result.policy = merged

    if partial_warning is not None:
        ref, resolved, attempted = partial_warning
        _rich_warning(
            f"Policy chain incomplete: {ref} unreachable, using {resolved} of {attempted} policies",
            symbol="warning",
        )


def discover_policy(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Discover and load the applicable policy for a project.

    Resolution order:
    1. If policy_override is a local file path -> load from file
    2. If policy_override is an https:// URL -> fetch from URL
       (http:// is rejected for security)
    3. If policy_override is "org" -> auto-discover from project's git remote
    4. If policy_override is "owner/repo" (or "host/owner/repo")
       -> fetch from that repo via GitHub Contents API
    5. If policy_override is None -> auto-discover from project's git remote

    The user-facing forms are documented in
    ``apm_cli.policy._help_text.POLICY_SOURCE_FORMS_HELP``; that constant
    is the single source of truth shared by ``apm audit --policy`` and
    ``apm policy status --policy-source``.

    The optional ``expected_hash`` (``"<algo>:<hex>"``) pins the leaf
    policy bytes; mismatches return ``outcome="hash_mismatch"`` and
    must always be treated fail-closed by callers.
    """
    if policy_override:
        path = Path(policy_override)
        if path.exists() and path.is_file():
            return _pkg()._load_from_file(path, expected_hash=expected_hash)
        if policy_override.startswith("http://"):
            return PolicyFetchResult(
                error="Refusing plaintext http:// policy URL -- use https://",
                source=f"url:{policy_override}",
            )
        if policy_override.startswith("https://"):
            return _pkg()._fetch_from_url(
                policy_override,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
            )
        if policy_override != "org":
            # Try as owner/repo reference
            return _pkg()._fetch_from_repo(
                policy_override,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
            )

    # Auto-discover from git remote
    return _pkg()._auto_discover(project_root, no_cache=no_cache, expected_hash=expected_hash)


def _load_from_file(path: Path, *, expected_hash: str | None = None) -> PolicyFetchResult:
    """Load policy from a local file."""
    try:
        # Read raw bytes ourselves so we can verify the pin against the
        # exact bytes that get parsed (matches the on-the-wire semantics
        # used by the URL/repo fetchers).
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return PolicyFetchResult(
            error=f"Failed to read {path}: {e}",
            outcome="cache_miss_fetch_fail",
        )

    source_label = f"file:{path}"
    mismatch = _pkg()._verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, _warnings = load_policy(content)
        outcome = "empty" if _pkg()._is_policy_empty(policy) else "found"
        actual_hash = (
            _pkg()._compute_hash_normalized(content, expected_hash)
            if expected_hash is not None
            else None
        )
        return PolicyFetchResult(
            policy=policy,
            source=source_label,
            outcome=outcome,
            raw_bytes_hash=actual_hash,
            expected_hash=expected_hash,
        )
    except PolicyValidationError as e:
        return PolicyFetchResult(error=f"Invalid policy file {path}: {e}", outcome="malformed")
