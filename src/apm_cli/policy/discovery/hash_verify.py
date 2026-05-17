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

from ..project_config import (
    _DEFAULT_HASH_ALGORITHM,
    _HASH_HEX_LEN,
    _HEX_RE,
    ALLOWED_HASH_ALGORITHMS,
    ProjectPolicyConfigError,
    compute_policy_hash,
)
from .class_ import PolicyFetchResult

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


def _split_hash_pin(expected_hash: str) -> tuple[str, str]:
    """Split an ``"<algo>:<hex>"`` pin into (algorithm, lowercase_hex).

    Bare hex (no prefix) is interpreted as sha256 for backwards
    compatibility -- callers that care about the algorithm should pass a
    fully-qualified pin. Raises :class:`ProjectPolicyConfigError` on a
    structurally invalid pin (unsupported algorithm, wrong length, non
    hex). The discovery helpers translate that into a fail-closed
    ``hash_mismatch`` outcome rather than crashing.
    """
    raw = expected_hash.strip()
    if ":" in raw:
        algo, _, hex_part = raw.partition(":")
        algo = algo.strip().lower()
    else:
        algo = _DEFAULT_HASH_ALGORITHM
        hex_part = raw
    hex_part = hex_part.strip().lower()
    if algo not in ALLOWED_HASH_ALGORITHMS:
        raise ProjectPolicyConfigError(f"Unsupported policy.hash algorithm '{algo}'")
    expected_len = _HASH_HEX_LEN[algo]
    if len(hex_part) != expected_len or not _HEX_RE.match(hex_part):
        raise ProjectPolicyConfigError(f"policy.hash is not a valid {algo} digest")
    return algo, hex_part


def _compute_hash_normalized(content: str, expected_hash: str | None) -> str:
    """Compute the digest of *content* under the algorithm declared by
    *expected_hash*, returning the canonical ``"<algo>:<hex>"`` form.

    When *expected_hash* is ``None`` the default algorithm (sha256) is
    used so the cache always carries a digest for later pin verification.
    """
    algo = _DEFAULT_HASH_ALGORITHM
    if expected_hash:
        try:
            algo, _ = _split_hash_pin(expected_hash)
        except ProjectPolicyConfigError:
            algo = _DEFAULT_HASH_ALGORITHM
    digest = compute_policy_hash(content, algo)
    return f"{algo}:{digest}"


def _verify_hash_pin(
    content: object,
    expected_hash: str | None,
    source_label: str,
) -> PolicyFetchResult | None:
    """Verify fetched policy bytes against the project's pin (#827).

    Returns ``None`` when there is no pin, or the digest matches. On
    mismatch -- or on a structurally invalid pin, which is treated as a
    mismatch to stay fail-closed -- returns a :class:`PolicyFetchResult`
    with ``outcome="hash_mismatch"`` that callers must propagate. The
    hash is computed on the raw UTF-8 bytes that get parsed (matching
    ``yaml.safe_load`` semantics) so a malicious mirror cannot bypass the
    check by re-serializing semantically-equivalent YAML.
    """
    if expected_hash is None:
        return None

    raw_bytes: bytes
    if isinstance(content, bytes):
        raw_bytes = content
    elif isinstance(content, str):
        raw_bytes = content.encode("utf-8")
    else:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(
                f"Policy hash mismatch from {source_label}: "
                "no content available to verify against pin"
            ),
            expected_hash=expected_hash,
        )

    try:
        algo, expected_hex = _split_hash_pin(expected_hash)
    except ProjectPolicyConfigError as exc:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(f"Policy hash mismatch from {source_label}: invalid pin ({exc})"),
            expected_hash=expected_hash,
        )

    digest = hashlib.new(algo)
    digest.update(raw_bytes)
    actual_hex = digest.hexdigest().lower()
    if actual_hex == expected_hex:
        return None

    expected_norm = f"{algo}:{expected_hex}"
    actual_norm = f"{algo}:{actual_hex}"
    return PolicyFetchResult(
        outcome="hash_mismatch",
        source=source_label,
        error=(
            f"Policy hash mismatch from {source_label}: expected {expected_norm}, got {actual_norm}"
        ),
        expected_hash=expected_norm,
        raw_bytes_hash=actual_norm,
    )
