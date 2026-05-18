"""GitHub-specific file-download helpers for APM packages.

Implements the CDN fast-path (raw.githubusercontent.com), the Contents-API
download path, rate-limit/auth error handling, and the public
``download_github_file`` entry point.
All names are private to the ``download_strategies`` package; the public
API surface lives in :mod:`git_strategy` which re-exports everything.
"""

from dataclasses import dataclass

import requests

from ...models.apm_package import DependencyReference
from ...utils.github_host import build_raw_content_url, default_host, is_github_hostname


@dataclass
class _RawUrlCtx:
    """Context bundle for :func:`_try_generic_host_raw_url`."""

    host: str
    owner: str
    repo: str
    ref: str
    file_path: str
    file_ctx: object
    dep_ref: object
    verbose_callback: object


@dataclass
class _ContentsApi404Ctx:
    """Context bundle for :func:`_handle_contents_api_404`."""

    host: str
    owner: str
    repo: str
    dep_ref: object
    file_path: str
    ref: str
    headers: dict
    api_url_candidates: list
    is_github_host: bool
    verbose_callback: object


def try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
    """Attempt to fetch a file via raw.githubusercontent.com (CDN).

    Returns the raw bytes on success, or ``None`` if the file was not found
    (HTTP 404) or the request failed for any reason.  This is intentionally
    best-effort: callers fall back to the Contents API when ``None`` is
    returned.
    """
    raw_url = build_raw_content_url(owner, repo, ref, file_path)
    try:
        response = requests.get(raw_url, timeout=30)
        if response.status_code == 200:
            return response.content
    except requests.exceptions.RequestException:
        pass
    return None


def _try_raw_cdn_download(
    self,
    owner: str,
    repo: str,
    ref: str,
    file_path: str,
    verbose_callback,
    host: str,
    dep_ref,
) -> bytes | None:
    """Try to download via raw.githubusercontent.com CDN (no auth, no rate limit).

    Attempts the given ref first; if that returns 404 and the ref is a
    typical default branch, also tries the other default.  Returns the
    downloaded bytes on success, or None if all attempts fail.
    """
    content = self.try_raw_download(owner, repo, ref, file_path)
    if content is not None:
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return content
    # raw download returned 404 -- could be wrong default branch.
    # Try the other default branch before falling through to the API.
    if ref in ("main", "master"):
        fallback_ref = "master" if ref == "main" else "main"
        content = self.try_raw_download(owner, repo, fallback_ref, file_path)
        if content is not None:
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return content
    return None


@dataclass
class _HttpDownloadContext:
    dep_ref: "DependencyReference"
    file_path: str
    ref: str
    token: "str | None"
    is_github_host: bool
    api_url: str
    verbose_callback: "object"


def _handle_http_401_or_403(
    self,
    e: "requests.exceptions.HTTPError",
    ctx: _HttpDownloadContext,
) -> bytes:
    """Handle a 401/403 from the GitHub Contents API.

    Extracted from :func:`download_github_file` to reduce its statement
    count within the configured Ruff thresholds.  Raises ``RuntimeError``
    on auth failure; returns ``bytes`` when the unauthenticated public-repo
    retry succeeds.
    """
    dep_ref = ctx.dep_ref
    file_path = ctx.file_path
    ref = ctx.ref
    token = ctx.token
    is_github_host = ctx.is_github_host
    api_url = ctx.api_url
    verbose_callback = ctx.verbose_callback
    host = dep_ref.host or default_host()
    owner = dep_ref.repo_url.split("/", 1)[0]

    # Distinguish rate limiting from auth failure.
    is_rate_limit = False
    if is_github_host:
        try:
            rl_remaining = e.response.headers.get("X-RateLimit-Remaining")
            if rl_remaining is not None and int(rl_remaining) == 0:
                is_rate_limit = True
        except (TypeError, ValueError):
            pass

    if is_rate_limit:
        error_msg = f"GitHub API rate limit exceeded for {dep_ref.repo_url}. "
        if not token:
            error_msg += (
                "Unauthenticated requests are limited to "
                "60/hour (shared per IP). "
                + self._host.auth_resolver.build_error_context(
                    host,
                    "API request (rate limited)",
                    org=owner,
                    port=(dep_ref.port if dep_ref else None),
                    dep_url=(dep_ref.repo_url if dep_ref else None),
                )
            )
        else:
            error_msg += (
                "Authenticated rate limit exhausted. "
                "Wait a few minutes or check your token's "
                "rate-limit quota."
            )
        raise RuntimeError(error_msg) from e

    # Retry without auth -- the repo might be public.
    # GHES/GHE-DR don't support unauthenticated org-scoped retries.
    if token and is_github_host and not host.lower().endswith(".ghe.com"):
        try:
            unauth_headers: dict[str, str] = {"Accept": "application/vnd.github.v3.raw"}
            response = self._host._resilient_get(api_url, headers=unauth_headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return self._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError:
            pass  # Fall through to the original error

    error_msg = f"Authentication failed for {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
    if not token:
        error_msg += self._host.auth_resolver.build_error_context(
            host,
            "download",
            org=owner,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif is_github_host and not host.lower().endswith(".ghe.com"):
        error_msg += (
            "Both authenticated and unauthenticated access "
            "were attempted. The repository may be private, "
            "or your token may lack SSO/SAML authorization "
            "for this organization."
        )
    elif is_github_host:
        error_msg += "Please check your GitHub token permissions."
    else:
        # Generic host: don't claim SSO/SAML or "GitHub token".
        error_msg += (
            f"Host {host} rejected the request. "
            "Verify the repository exists and that the token has "
            "access. Tokens are sourced from your git credential "
            "helper, a per-org GITHUB_APM_PAT_<ORG> env var, or "
            f"GITHUB_HOST={host} when this host is your GitHub "
            "Enterprise Server."
        )
    raise RuntimeError(error_msg)  # noqa: B904


def _try_generic_host_raw_url(self, ctx: _RawUrlCtx) -> bytes | None:
    """Try the raw URL path for a non-GitHub (Gitea/Gogs/generic) host.

    Attempts ``https://{host}/{owner}/{repo}/raw/{ref}/{file_path}`` with
    any host-scoped credentials and returns the raw bytes on HTTP 200.
    Returns ``None`` on 404 or any network error so the caller can fall
    through to the Contents API negotiation loop.

    Only called when ``is_github_host`` is ``False``; never touches the
    GitHub CDN or the GitHub Contents API.
    """
    host = ctx.host
    owner = ctx.owner
    repo = ctx.repo
    ref = ctx.ref
    file_path = ctx.file_path
    file_ctx = ctx.file_ctx
    dep_ref = ctx.dep_ref
    verbose_callback = ctx.verbose_callback
    raw_url = f"https://{host}/{owner}/{repo}/raw/{ref}/{file_path}"
    raw_headers = self._build_generic_host_auth_headers(host, file_ctx, accept=None)
    if verbose_callback:
        verbose_callback(f"Trying raw URL on generic host {host}: {raw_url}")
    try:
        response = self._host._resilient_get(raw_url, headers=raw_headers, timeout=30)
        if response.status_code == 200:
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return response.content
    except (requests.RequestException, OSError) as raw_err:
        if verbose_callback:
            verbose_callback(
                f"Raw URL on {host} failed for {file_path}@{ref}: "
                f"{type(raw_err).__name__}; falling back to Contents API."
            )
    return None


def _handle_contents_api_404(self, ctx: _ContentsApi404Ctx) -> bytes:
    """Handle a 404 from the primary Contents-API URL candidate.

    For generic hosts, works through remaining API-version candidates
    (v1 → v3) before attempting a main/master branch swap.  Raises a
    descriptive ``RuntimeError`` when all candidates and both default
    branches are exhausted.

    Extracted from :func:`download_github_file` to reduce its branch count
    below the configured PLR0912/C901 thresholds.
    """
    host = ctx.host
    owner = ctx.owner
    repo = ctx.repo
    dep_ref = ctx.dep_ref
    file_path = ctx.file_path
    ref = ctx.ref
    headers = ctx.headers
    api_url_candidates = ctx.api_url_candidates
    is_github_host = ctx.is_github_host
    verbose_callback = ctx.verbose_callback
    # For generic hosts, try remaining API version candidates before ref fallback.
    for candidate_url in api_url_candidates[1:]:
        try:
            if verbose_callback:
                verbose_callback(f"Contents API 404; trying next candidate: {candidate_url}")
            candidate_resp = self._host._resilient_get(candidate_url, headers=headers, timeout=30)
            candidate_resp.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return self._extract_contents_api_payload(candidate_resp, is_github_host)
        except requests.exceptions.HTTPError as ce:
            if ce.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    f"Failed to download {file_path}: HTTP {ce.response.status_code}"
                )

    # Non-default refs have no branch-swap fallback.
    if ref not in ("main", "master"):
        raise RuntimeError(  # noqa: B904
            self._build_unsupported_or_missing_error(
                host,
                dep_ref.repo_url,
                file_path,
                ref,
                api_url_candidates,
                is_github_host=is_github_host,
            )
        )

    # Try the other default branch (main ↔ master).
    fallback_ref = "master" if ref == "main" else "main"
    fallback_url_candidates = self._build_contents_api_urls(
        host, owner, repo, file_path, fallback_ref
    )

    for fallback_url in fallback_url_candidates:
        try:
            response = self._host._resilient_get(fallback_url, headers=headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return self._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError as fe:
            if fe.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    f"Failed to download {file_path}: HTTP {fe.response.status_code}"
                )

    raise RuntimeError(  # noqa: B904
        self._build_unsupported_or_missing_error(
            host,
            dep_ref.repo_url,
            file_path,
            ref,
            api_url_candidates,
            is_github_host=is_github_host,
            fallback_ref=fallback_ref,
        )
    )


def download_github_file(
    self,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
    verbose_callback=None,
) -> bytes:
    """Download a file from GitHub repository.

    For github.com without a token, tries raw.githubusercontent.com first
    (CDN, no rate limit) before falling back to the Contents API.
    Authenticated requests and non-github.com hosts always use the
    Contents API directly.

    Args:
        dep_ref: Parsed dependency reference
        file_path: Path to file within the repository
        ref: Git reference (branch, tag, or commit SHA)
        verbose_callback: Optional callable for verbose logging

    Returns:
        bytes: File content
    """
    host = dep_ref.host or default_host()

    # Parse owner/repo from repo_url.  ``owner`` doubles as the org for
    # auth resolution -- no separate extraction needed.
    owner, repo = dep_ref.repo_url.split("/", 1)
    file_ctx = self._host.auth_resolver.resolve(host, owner, port=dep_ref.port)
    token = file_ctx.token

    # --- CDN fast-path for github.com without a token ---
    # raw.githubusercontent.com is served from GitHub's CDN and is not
    # subject to the REST API rate limit (60 req/h unauthenticated).
    # Only available for github.com -- GHES/GHE-DR have no equivalent.
    if host.lower() == "github.com" and not token:
        cdn_result = _try_raw_cdn_download(
            self, owner, repo, ref, file_path, verbose_callback, host, dep_ref
        )
        if cdn_result is not None:
            return cdn_result
        # All raw attempts failed -- fall through to API path which
        # handles private repos, rate-limit messaging, and SAML errors.

    # --- Generic host: raw URL first, then API version negotiation ---
    # For non-GitHub non-GHE hosts (Gitea, Gogs, self-hosted git), try the
    # raw URL path first, then negotiate API versions v1 -> v3.
    is_github_host = is_github_hostname(host) or self._is_configured_ghes(host)
    if not is_github_host:
        raw_result = _try_generic_host_raw_url(
            self,
            _RawUrlCtx(
                host=host,
                owner=owner,
                repo=repo,
                ref=ref,
                file_path=file_path,
                file_ctx=file_ctx,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
            ),
        )
        if raw_result is not None:
            return raw_result

    # --- Contents API path (authenticated, enterprise, or raw fallback) ---
    # Build API URL candidates - format differs by host type.
    api_url_candidates = self._build_contents_api_urls(
        host, owner, repo, file_path, ref, is_github_host=is_github_host
    )
    api_url = api_url_candidates[0]

    # Set up authentication headers.
    # GitHub family: use GitHub raw-media accept header. Generic hosts
    # ignore it and may return JSON envelopes -- handle that on read.
    accept = "application/vnd.github.v3.raw" if is_github_host else "application/json"
    if is_github_host:
        headers: dict[str, str] = {"Accept": accept}
        if token:
            headers["Authorization"] = f"token {token}"
    else:
        headers = self._build_generic_host_auth_headers(host, file_ctx, accept=accept)

    # Issue the Contents API request.
    try:
        if verbose_callback and not is_github_host:
            verbose_callback(f"Trying Contents API on {host}: {api_url}")
        response = self._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return self._extract_contents_api_payload(response, is_github_host)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return _handle_contents_api_404(
                self,
                _ContentsApi404Ctx(
                    host=host,
                    owner=owner,
                    repo=repo,
                    dep_ref=dep_ref,
                    file_path=file_path,
                    ref=ref,
                    headers=headers,
                    api_url_candidates=api_url_candidates,
                    is_github_host=is_github_host,
                    verbose_callback=verbose_callback,
                ),
            )
        if e.response.status_code in (401, 403):
            return _handle_http_401_or_403(
                self,
                e,
                _HttpDownloadContext(
                    dep_ref=dep_ref,
                    file_path=file_path,
                    ref=ref,
                    token=token,
                    is_github_host=is_github_host,
                    api_url=api_url,
                    verbose_callback=verbose_callback,
                ),
            )
        raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e
