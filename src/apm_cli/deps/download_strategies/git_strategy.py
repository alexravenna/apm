"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import base64
import json
import os
from urllib.parse import quote

import requests

from ...core.auth import AuthResolver, HostInfo
from ...models.apm_package import DependencyReference
from ...utils.github_host import (
    build_ado_api_url,
    build_raw_content_url,
    default_host,
    is_github_hostname,
)
from .class_ import DownloadDelegate


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


def download_ado_file(
    self,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
) -> bytes:
    """Download a file from Azure DevOps repository.

    Args:
        dep_ref: Parsed dependency reference with ADO-specific fields
        file_path: Path to file within the repository
        ref: Git reference (branch, tag, or commit SHA)

    Returns:
        bytes: File content
    """
    import base64

    # Validate required ADO fields before proceeding
    if not all([dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo]):
        raise ValueError(
            "Invalid Azure DevOps dependency reference: missing "
            "organization, project, or repo. "
            f"Got: org={dep_ref.ado_organization}, "
            f"project={dep_ref.ado_project}, repo={dep_ref.ado_repo}"
        )

    host = dep_ref.host or "dev.azure.com"
    api_url = build_ado_api_url(
        dep_ref.ado_organization,
        dep_ref.ado_project,
        dep_ref.ado_repo,
        file_path,
        ref,
        host,
    )

    # Set up authentication headers - ADO uses Basic auth with PAT
    headers: dict[str, str] = {}
    if self._host.ado_token:
        # ADO uses Basic auth: username can be empty, password is the PAT
        auth = base64.b64encode(f":{self._host.ado_token}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"

    try:
        response = self._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.content
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            # Try fallback branches
            if ref not in ["main", "master"]:
                raise RuntimeError(
                    f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
                ) from e

            fallback_ref = "master" if ref == "main" else "main"
            fallback_url = build_ado_api_url(
                dep_ref.ado_organization,
                dep_ref.ado_project,
                dep_ref.ado_repo,
                file_path,
                fallback_ref,
                host,
            )

            try:
                response = self._host._resilient_get(fallback_url, headers=headers, timeout=30)
                response.raise_for_status()
                return response.content
            except requests.exceptions.HTTPError as fallback_err:
                raise RuntimeError(
                    f"File not found: {file_path} in {dep_ref.repo_url} "
                    f"(tried refs: {ref}, {fallback_ref})"
                ) from fallback_err
        elif e.response.status_code in (401, 403):
            error_msg = f"Authentication failed for Azure DevOps {dep_ref.repo_url}. "
            if not self._host.ado_token:
                error_msg += self._host.auth_resolver.build_error_context(
                    host,
                    "download",
                    org=dep_ref.ado_organization if dep_ref else None,
                    port=dep_ref.port if dep_ref else None,
                    dep_url=dep_ref.repo_url if dep_ref else None,
                )
            else:
                error_msg += "Please check your Azure DevOps PAT permissions."
            raise RuntimeError(error_msg) from e
        else:
            raise RuntimeError(
                f"Failed to download {file_path}: HTTP {e.response.status_code}"
            ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e


def download_gitlab_file(
    self,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
    verbose_callback=None,
) -> bytes:
    """Download a file via GitLab REST v4 ``repository/files/.../raw``."""
    host = dep_ref.host or default_host()
    host_info = self._host.auth_resolver.classify_host(host)
    project_path = dep_ref.repo_url
    if not project_path:
        raise RuntimeError("Missing repository path for GitLab file download")

    org = project_path.split("/")[0]
    file_ctx = self._host.auth_resolver.resolve(host, org, port=dep_ref.port)
    token = file_ctx.token
    headers = AuthResolver.gitlab_rest_headers(token)

    api_base = host_info.api_base.rstrip("/")
    enc_proj = quote(project_path, safe="")
    enc_file = quote(file_path, safe="")

    def _raw_url(r: str) -> str:
        return (
            f"{api_base}/projects/{enc_proj}/repository/files/{enc_file}/raw"
            f"?ref={quote(r, safe='')}"
        )

    api_url = _raw_url(ref)

    try:
        response = self._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return response.content
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            if ref not in ("main", "master"):
                raise RuntimeError(
                    f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
                ) from e
            fallback_ref = "master" if ref == "main" else "main"
            fallback_url = _raw_url(fallback_ref)
            try:
                response = self._host._resilient_get(fallback_url, headers=headers, timeout=30)
                response.raise_for_status()
                if verbose_callback:
                    verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                return response.content
            except requests.exceptions.HTTPError as fallback_err:
                raise RuntimeError(
                    f"File not found: {file_path} in {dep_ref.repo_url} "
                    f"(tried refs: {ref}, {fallback_ref})"
                ) from fallback_err
        if e.response is not None and e.response.status_code in (401, 403):
            error_msg = (
                f"Authentication failed for GitLab {dep_ref.repo_url} "
                f"(file: {file_path}, ref: {ref}). "
            )
            if not token:
                error_msg += self._host.auth_resolver.build_error_context(
                    host, "download", org=org, port=dep_ref.port
                )
            else:
                error_msg += "Please verify your token can read this project (required API scope)."
            raise RuntimeError(error_msg) from e
        if e.response is not None:
            raise RuntimeError(
                f"Failed to download {file_path}: HTTP {e.response.status_code}"
            ) from e
        raise
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e


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

    # Parse owner/repo from repo_url
    owner, repo = dep_ref.repo_url.split("/", 1)

    # Resolve token via AuthResolver for CDN fast-path decision
    org = None
    if dep_ref and dep_ref.repo_url:
        parts = dep_ref.repo_url.split("/")
        if parts:
            org = parts[0]
    file_ctx = self._host.auth_resolver.resolve(host, org, port=dep_ref.port)
    token = file_ctx.token

    # --- CDN fast-path for github.com without a token ---
    # raw.githubusercontent.com is served from GitHub's CDN and is not
    # subject to the REST API rate limit (60 req/h unauthenticated).
    # Only available for github.com -- GHES/GHE-DR have no equivalent.
    if host.lower() == "github.com" and not token:
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
        # All raw attempts failed -- fall through to API path which
        # handles private repos, rate-limit messaging, and SAML errors.

    # --- Generic host: raw URL first, then API version negotiation ---
    # For non-GitHub non-GHE hosts (Gitea, Gogs, self-hosted git), try the
    # raw URL path first, then negotiate API versions v1 -> v3.
    is_github_host = is_github_hostname(host) or self._is_configured_ghes(host)
    if not is_github_host:
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

    # --- Contents API path (authenticated, enterprise, or raw fallback) ---
    # Build API URL candidates - format differs by host type
    api_url_candidates = self._build_contents_api_urls(
        host, owner, repo, file_path, ref, is_github_host=is_github_host
    )
    api_url = api_url_candidates[0]

    # Set up authentication headers
    # GitHub family: use GitHub raw-media accept header. Generic hosts
    # ignore it and may return JSON envelopes -- handle that on read.
    accept = "application/vnd.github.v3.raw" if is_github_host else "application/json"
    if is_github_host:
        headers: dict[str, str] = {"Accept": accept}
        if token:
            headers["Authorization"] = f"token {token}"
    else:
        headers = self._build_generic_host_auth_headers(host, file_ctx, accept=accept)

    # Try to download with the specified ref
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
            # For generic hosts, try remaining API version candidates before ref fallback
            for candidate_url in api_url_candidates[1:]:
                try:
                    if verbose_callback:
                        verbose_callback(
                            f"Contents API 404; trying next candidate: {candidate_url}"
                        )
                    candidate_resp = self._host._resilient_get(
                        candidate_url, headers=headers, timeout=30
                    )
                    candidate_resp.raise_for_status()
                    if verbose_callback:
                        verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                    return self._extract_contents_api_payload(candidate_resp, is_github_host)
                except requests.exceptions.HTTPError as ce:
                    if ce.response.status_code != 404:
                        raise RuntimeError(  # noqa: B904
                            f"Failed to download {file_path}: HTTP {ce.response.status_code}"
                        )

            # Try fallback branches if the specified ref fails
            if ref not in ["main", "master"]:
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

            # Try the other default branch
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
        elif e.response.status_code in (401, 403):
            # Distinguish rate limiting from auth failure.
            # X-RateLimit-* headers are GitHub-specific; treat as
            # rate-limit only when the host is in the GitHub family.
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
                    response = self._host._resilient_get(
                        api_url, headers=unauth_headers, timeout=30
                    )
                    response.raise_for_status()
                    if verbose_callback:
                        verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                    return self._extract_contents_api_payload(response, is_github_host)
                except requests.exceptions.HTTPError:
                    pass  # Fall through to the original error

            error_msg = (
                f"Authentication failed for {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
            )
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
        else:
            raise RuntimeError(
                f"Failed to download {file_path}: HTTP {e.response.status_code}"
            ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}")  # noqa: B904


def _is_configured_ghes(host: str) -> bool:
    """Return True when *host* matches the user's declared GHES via GITHUB_HOST.

    ``GITHUB_HOST=<custom-domain>`` is the documented opt-in for treating
    a non-``*.ghe.com`` FQDN as GitHub-family. Centralised so the routing
    check, header builder, and Contents-API URL builder cannot drift.
    """
    configured = os.environ.get("GITHUB_HOST", "").strip().lower()
    if not configured:
        return False
    return (host or "").lower() == configured


def _build_contents_api_urls(
    host: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
    *,
    is_github_host: bool | None = None,
) -> list[str]:
    """Return the ordered list of Contents-API URL candidates for *host*.

    Thin wrapper around the per-host backends -- the actual URL shape
    lives on the backend. Kept as a static method on
    :class:`DownloadDelegate` for back-compat with existing callers
    and tests that monkey-patch it.
    """
    from ..host_backends import GenericGitBackend, GHECloudBackend, GHESBackend, GitHubBackend

    if is_github_host is None:
        is_github_host = is_github_hostname(host) or DownloadDelegate._is_configured_ghes(host)

    host_lower = (host or "").lower()
    if not is_github_host:
        backend = GenericGitBackend(
            host_info=HostInfo(
                host=host,
                kind="generic",
                has_public_repos=False,
                api_base=f"https://{host}",
            )
        )
    elif host_lower == "github.com":
        backend = GitHubBackend(
            host_info=HostInfo(
                host=host,
                kind="github",
                has_public_repos=True,
                api_base="https://api.github.com",
            )
        )
    elif host_lower.endswith(".ghe.com"):
        backend = GHECloudBackend(
            host_info=HostInfo(
                host=host,
                kind="ghe_cloud",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
            )
        )
    else:
        # Configured GHES (GITHUB_HOST=<custom-host>): api_base is
        # ``https://{host}/api/v3``, not ``https://api.{host}``.
        backend = GHESBackend(
            host_info=HostInfo(
                host=host,
                kind="ghes",
                has_public_repos=False,
                api_base=f"https://{host}/api/v3",
            )
        )
    return backend.build_contents_api_urls(owner, repo, file_path, ref)


def _build_generic_host_auth_headers(
    host: str, auth_ctx, *, accept: str | None = None
) -> dict[str, str]:
    """Build HTTP headers for a generic-host (non-GitHub) request.

    SECURITY GUARD: Only attach Authorization when the token is
    unambiguously intended for this host. A token resolved from a
    global env var (GITHUB_APM_PAT, GITHUB_TOKEN, GH_TOKEN) MUST NOT
    be sent to an arbitrary non-GitHub host -- doing so leaks the
    user's GitHub PAT to whatever FQDN is in the dependency line.
    The clone path at ``get_clone_url`` already enforces the same
    guard via ``is_github_hostname``; this mirrors it for HTTP file
    downloads.

    Forwarding is allowed when:
    - source == ``git-credential-fill``: git's credential helper
      looks tokens up by host, so they are host-scoped by
      construction.
    - source == ``GITHUB_APM_PAT_<ORG>``: per-org env var is
      explicit user opt-in for that org's host.
    - the user opted into this host as their GitHub Enterprise
      Server via ``GITHUB_HOST=<host>``: the token is intended for
      this host, even if the FQDN is not under ``*.ghe.com``.
    """
    headers: dict[str, str] = {}
    if accept:
        headers["Accept"] = accept
    if auth_ctx is None or not getattr(auth_ctx, "token", None):
        return headers
    source = getattr(auth_ctx, "source", None) or ""
    host_scoped = source == "git-credential-fill"
    org_scoped = source.startswith("GITHUB_APM_PAT_")
    configured_ghes = DownloadDelegate._is_configured_ghes(host)
    if host_scoped or org_scoped or configured_ghes:
        headers["Authorization"] = f"token {auth_ctx.token}"
    return headers


def _extract_contents_api_payload(response, is_github_host: bool) -> bytes:
    """Decode a Contents-API response into raw file bytes.

    - GitHub family: ``Accept: application/vnd.github.v3.raw`` returns
      the file bytes directly; pass through ``response.content``.
    - Generic hosts (Gitea, Gogs): the raw-media accept header is
      ignored and the server returns a JSON envelope of the form::

          {"content": "<base64>", "encoding": "base64", ...}

      Decode ``content`` as base64 and return the resulting bytes.
      Some Gitea installations also emit ``encoding: ""`` with raw
      content -- pass that through unchanged. If the response is not
      a JSON envelope at all (custom proxy, raw bytes), fall back to
      ``response.content``.
    """
    if is_github_host:
        return response.content

    body = response.content
    try:
        ctype = str((response.headers or {}).get("Content-Type") or "").lower()
    except (AttributeError, TypeError):
        ctype = ""
    if "json" not in ctype and not (
        isinstance(body, (bytes, bytearray)) and body.lstrip().startswith(b"{")
    ):
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return body
    if not isinstance(payload, dict) or "content" not in payload:
        return body
    encoding = (payload.get("encoding") or "").lower()
    content_field = payload.get("content") or ""
    if encoding == "base64":
        try:
            return base64.b64decode(content_field, validate=False)
        except (ValueError, TypeError):
            return body
    # Non-base64 envelope (rare): return literal content if it's a string,
    # otherwise fall back to the raw body.
    if isinstance(content_field, str):
        return content_field.encode("utf-8")
    return body


def _build_unsupported_or_missing_error(
    host: str,
    repo_url: str,
    file_path: str,
    ref: str,
    api_url_candidates: list[str],
    *,
    is_github_host: bool,
    fallback_ref: str | None = None,
) -> str:
    """Build a discoverable error when no Contents-API candidate hits 200."""
    ref_part = f"(tried refs: {ref}, {fallback_ref})" if fallback_ref else f"at ref '{ref}'"
    if is_github_host:
        return f"File not found: {file_path} in {repo_url} {ref_part}"
    # Non-GitHub host: name what was tried so users can diagnose
    # GitLab / unsupported-host cases without re-reading source.
    tried = ", ".join(["raw"] + [u.split("/api/")[1].split("/")[0] for u in api_url_candidates])
    canonical_url = f"https://{host}/{repo_url}/raw/{ref}/{file_path}"
    return (
        f"File not found on generic host {host}: {canonical_url} {ref_part}. "
        f"Tried URL families: {tried}. "
        "If this is GitLab, virtual subdirectory packages are not "
        "supported (use the dict-form full repo URL instead)."
    )
