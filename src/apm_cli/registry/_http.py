"""Private HTTP transport helpers for the MCP registry client.

Contains the persistent HTTP-cache layer extracted from
:meth:`~apm_cli.registry.client.SimpleRegistryClient._cached_get_json`.

Not part of the public API.
"""

from __future__ import annotations

import json as _json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ._helpers import _safe_headers

if TYPE_CHECKING:
    import requests

_log = logging.getLogger(__name__)


def _get_json_with_cache(
    session: requests.Session,
    http_cache: Any | None,
    timeout: tuple,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """GET ``url`` honoring the persistent HTTP cache.

    On a fresh cache hit returns the parsed JSON immediately.  On an
    expired entry, sends ``If-None-Match`` for revalidation; on 304 the
    cached body is reused and its TTL refreshed.  Returns
    ``(json_payload, response_headers)``; when there is no payload
    (204 No Content), ``json_payload`` is ``None``.

    Falls back to a plain ``session.get`` when the cache is disabled
    or unavailable.

    Args:
        session: Active :class:`requests.Session` to use for network calls.
        http_cache: Persistent cache instance, or ``None`` to skip caching.
        timeout: ``(connect, read)`` timeout tuple forwarded to ``session.get``.
        url: Full URL to fetch.
        params: Optional query-parameter dict.
    """
    # Cache key includes query params so paginated/search URLs are
    # cached independently.
    cache_key = url
    if params:
        cache_key = f"{url}?{urlencode(sorted(params.items()))}"

    # Auth bypass: when the request would carry an Authorization
    # header (either on the session or per-request), skip the
    # cache entirely. Caching authenticated responses risks
    # cross-identity body leakage when a different caller hits
    # the same URL with different credentials -- and scoping the
    # cache by hashed token would just recreate the underlying
    # auth-store responsibility. Bypass is the simple safe
    # default; the MCP registry path is anonymous in practice.
    session_auth = bool(session.headers.get("Authorization"))
    if session_auth or http_cache is None:
        kwargs0: dict[str, Any] = {"timeout": timeout}
        if params:
            kwargs0["params"] = params
        response = session.get(url, **kwargs0)
        response.raise_for_status()
        return response.json(), _safe_headers(response)

    # Fresh cache hit
    cached = http_cache.get(cache_key)
    if cached is not None:
        try:
            return _json.loads(cached.body.decode("utf-8")), {}
        except (ValueError, UnicodeDecodeError):
            pass  # fall through to network

    # Expired or missing: send conditional headers if we have an ETag
    request_headers = http_cache.conditional_headers(cache_key)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if params:
        kwargs["params"] = params
    if request_headers:
        kwargs["headers"] = request_headers
    response = session.get(url, **kwargs)

    if response.status_code == 304:
        http_cache.refresh_expiry(cache_key, _safe_headers(response))
        cached = http_cache.get(cache_key)
        if cached is not None:
            try:
                return _json.loads(cached.body.decode("utf-8")), _safe_headers(response)
            except (ValueError, UnicodeDecodeError):
                pass  # fall through to a fresh fetch
        # Stored entry vanished between revalidate and read: refetch
        kwargs2: dict[str, Any] = {"timeout": timeout}
        if params:
            kwargs2["params"] = params
        response = session.get(url, **kwargs2)

    response.raise_for_status()
    try:
        body = response.content
        http_cache.store(
            cache_key,
            body,
            status_code=response.status_code,
            headers=_safe_headers(response),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("HTTP cache store failed for %s: %s", cache_key, exc)
    return response.json(), _safe_headers(response)
