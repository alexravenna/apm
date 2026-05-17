"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import random
import time

import requests

from ...models.apm_package import DependencyReference
from ...utils.github_host import (
    build_https_clone_url,
    build_ssh_url,
    default_host,
)
from ..host_backends import backend_for
from .class_ import _debug


def resilient_get(
    self,
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
    max_retries: int = 3,
) -> requests.Response:
    """HTTP GET with retry on 429/503 and rate-limit header awareness.

    Args:
        url: Request URL
        headers: HTTP headers
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts for transient failures

    Returns:
        requests.Response (caller should call .raise_for_status() as needed)

    Raises:
        requests.exceptions.RequestException: After all retries exhausted
    """
    last_exc = None
    last_response = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)

            # Handle rate limiting -- GitHub returns 429 for secondary limits
            # and 403 with X-RateLimit-Remaining: 0 for primary limits.
            is_rate_limited = response.status_code in (429, 503)
            if not is_rate_limited and response.status_code == 403:
                try:
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and int(remaining) == 0:
                        is_rate_limited = True
                except (TypeError, ValueError):
                    pass

            if is_rate_limited:
                last_response = response
                retry_after = response.headers.get("Retry-After")
                reset_at = response.headers.get("X-RateLimit-Reset")
                if retry_after:
                    try:
                        wait = min(float(retry_after), 60)
                    except (TypeError, ValueError):
                        # Retry-After may be an HTTP-date; fall back to exponential backoff
                        wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                elif reset_at:
                    try:
                        wait = max(0, min(int(reset_at) - time.time(), 60))
                    except (TypeError, ValueError):
                        wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                else:
                    wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                _debug(
                    f"Rate limited ({response.status_code}), retry in "
                    f"{wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
                continue

            # Log rate limit proximity
            remaining = response.headers.get("X-RateLimit-Remaining")
            try:
                if remaining and int(remaining) < 10:
                    _debug(f"GitHub API rate limit low: {remaining} requests remaining")
            except (TypeError, ValueError):
                pass

            return response
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                _debug(
                    f"Connection error, retry in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
        except requests.exceptions.Timeout as e:
            last_exc = e
            if attempt < max_retries - 1:
                _debug(f"Timeout, retrying (attempt {attempt + 1}/{max_retries})")

    # If rate limiting exhausted all retries, return the last response so
    # callers can inspect headers (e.g. X-RateLimit-Remaining) and raise
    # an appropriate user-facing error.
    if last_response is not None:
        return last_response

    if last_exc:
        raise last_exc
    raise requests.exceptions.RequestException(f"All {max_retries} attempts failed for {url}")


def build_repo_url(
    self,
    repo_ref: str,
    use_ssh: bool = False,
    dep_ref: DependencyReference = None,
    token: str | None = None,
    auth_scheme: str = "basic",
) -> str:
    """Build the appropriate repository URL for cloning.

    Supports both GitHub and Azure DevOps URL formats:
    - GitHub: https://github.com/owner/repo.git
    - ADO: https://dev.azure.com/org/project/_git/repo

    Args:
        repo_ref: Repository reference in format "owner/repo" or
            "org/project/repo" for ADO
        use_ssh: Whether to use SSH URL for git operations
        dep_ref: Optional DependencyReference for ADO-specific URL building
        token: Optional per-dependency token override
        auth_scheme: Auth scheme ("basic" or "bearer"). Bearer tokens are
            injected via env vars, NOT embedded in the URL.

    Returns:
        str: Repository URL suitable for git clone operations
    """
    # Resolve host (used for token-routing and as a fallback when
    # ``dep_ref`` is missing for legacy callers).
    if dep_ref and dep_ref.host:
        host = dep_ref.host
    else:
        host = getattr(self._host, "github_host", None) or default_host()

    # Pick the vendor-specific backend via ``classify_host`` -- this
    # replaces the in-line ``if is_ado / elif is_github / else`` ladder
    # with a single dispatch.
    backend = backend_for(
        dep_ref,
        self._host.auth_resolver,
        fallback_host=host,
    )

    is_ado = backend.kind == "ado"
    is_insecure = bool(getattr(dep_ref, "is_insecure", False)) if dep_ref is not None else False

    # Resolve the effective token. ``token == ""`` is the explicit
    # "suppress per-instance default" signal used by the
    # TransportSelector for plain-HTTPS / SSH attempts.
    if token == "":
        effective_token: str | None = ""
    elif token is not None:
        effective_token = token
    elif is_ado:
        effective_token = self._host.ado_token
    elif backend.is_github_family:
        effective_token = self._host.github_token
    elif backend.kind == "gitlab" and dep_ref is not None:
        # GitLab tokens come from GITLAB_APM_PAT / GITLAB_TOKEN /
        # credential helpers via the per-dep AuthResolver lookup.
        effective_token = self._host.auth_resolver.resolve_for_dep(dep_ref).token
    else:
        # Generic hosts: backend never embeds tokens; pick None so the
        # branch below produces the expected "no credential in URL" form.
        effective_token = None

    _debug(
        f"build_repo_url: host={host}, kind={backend.kind}, "
        f"dep_ref={'present' if dep_ref else 'None'}, "
        f"ado_org={dep_ref.ado_organization if dep_ref else None}"
    )

    # ADO without a parsed ``ado_organization`` cannot use the ADO
    # builders (they need org/project/repo). Fall through to the
    # generic GitHub-style URL the way the previous ladder did.
    if is_ado and not (dep_ref and dep_ref.ado_organization):
        backend = backend_for(
            None,
            self._host.auth_resolver,
            fallback_host=host,
        )

    if dep_ref is None:
        # Legacy no-dep_ref callers: preserve historical behaviour.
        # Build URL directly from ``repo_ref`` + ``host`` since the
        # backends require a dep_ref to read host/port/etc.
        port = None
        if use_ssh:
            return build_ssh_url(host, repo_ref, port=port)
        if is_insecure:
            return f"http://{host}/{repo_ref}.git"
        if backend.is_github_family and effective_token:
            return build_https_clone_url(host, repo_ref, token=effective_token, port=port)
        return build_https_clone_url(host, repo_ref, token=None, port=port)

    if use_ssh:
        return backend.build_clone_ssh_url(dep_ref)
    if is_insecure:
        return backend.build_clone_http_url(dep_ref)
    return backend.build_clone_https_url(dep_ref, token=effective_token, auth_scheme=auth_scheme)
