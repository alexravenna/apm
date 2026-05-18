"""Centralized authentication resolution for APM CLI.

Every APM operation that touches a remote host MUST use AuthResolver.
Resolution is per-(host, org) pair, thread-safe, and cached per-process.

All token-bearing requests use HTTPS — that is the transport security
boundary. Token environment variables are chosen by host class (GitHub-class,
GitLab, generic, or ADO); when a resolved token fails against the target host,
``try_with_fallback`` retries with git credential helpers where applicable.

Usage::

    resolver = AuthResolver()
    ctx = resolver.resolve("github.com", org="microsoft")
    # ctx.token, ctx.source, ctx.token_type, ctx.host_info, ctx.git_env

For dependencies::

    ctx = resolver.resolve_for_dep(dep_ref)

For operations with automatic auth/unauth fallback::

    result = resolver.try_with_fallback(
        "github.com", lambda token, env: download(token, env),
        org="microsoft",
    )
"""

from __future__ import annotations

import os
from typing import TypeVar

from apm_cli.utils.github_host import (
    is_azure_devops_hostname,
    is_gitlab_hostname,
    is_valid_fqdn,
)

from .class_ import HostInfo

T = TypeVar("T")


def classify_host(host: str, port: int | None = None) -> HostInfo:
    """Return a ``HostInfo`` describing *host*.

    ``port`` is carried through onto the returned ``HostInfo`` so that
    downstream code (cache keys, credential-helper input, error text)
    can discriminate between the same hostname on different ports.
    Host-kind classification itself is transport-agnostic -- the port
    never influences whether a host is GitHub/GHES/ADO/generic.
    """
    h = host.lower()

    if h == "github.com":
        return HostInfo(
            host=host,
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
            port=port,
        )

    if h.endswith(".ghe.com"):
        return HostInfo(
            host=host,
            kind="ghe_cloud",
            has_public_repos=False,
            api_base=f"https://{host}/api/v3",
            port=port,
        )

    if is_azure_devops_hostname(host):
        return HostInfo(
            host=host,
            kind="ado",
            has_public_repos=True,
            api_base="https://dev.azure.com",
            port=port,
        )

    # GHES: GITHUB_HOST is set to a non-github.com, non-ghe.com FQDN
    ghes_host = os.environ.get("GITHUB_HOST", "").lower()
    if (
        ghes_host
        and ghes_host == h
        and ghes_host not in {"github.com", "gitlab.com"}
        and not ghes_host.endswith(".ghe.com")
    ):
        if is_valid_fqdn(ghes_host):
            return HostInfo(
                host=host,
                kind="ghes",
                has_public_repos=True,
                api_base=f"https://{host}/api/v3",
                port=port,
            )

    # GitLab (SaaS + env-configured self-managed) — after GHES per spec (no silent GHES → GitLab)
    if is_gitlab_hostname(host):
        if h == "gitlab.com":
            api_base = "https://gitlab.com/api/v4"
        else:
            api_base = f"https://{host}/api/v4"
        return HostInfo(
            host=host,
            kind="gitlab",
            has_public_repos=True,
            api_base=api_base,
            port=port,
        )

    # Generic FQDN (Bitbucket, self-hosted non-GitLab, etc.)
    return HostInfo(
        host=host,
        kind="generic",
        has_public_repos=True,
        api_base=f"https://{host}/api/v3",
        port=port,
    )


# Ordered prefix -> token-kind mapping used by detect_token_type.
_TOKEN_PREFIXES: dict[str, str] = {
    "github_pat_": "fine-grained",
    "ghp_": "classic",
    "ghu_": "oauth",
    "gho_": "oauth",
    "ghs_": "github-app",
    "ghr_": "github-app",
}


def detect_token_type(token: str) -> str:
    """Classify a token string by its prefix.

    Note: EMU (Enterprise Managed Users) tokens use standard PAT
    prefixes (``ghp_`` or ``github_pat_``).  There is no prefix that
    identifies a token as EMU-scoped — that's a property of the
    account, not the token format.

    Prefix reference (docs.github.com):
    - ``github_pat_`` -> fine-grained PAT
    - ``ghp_``        -> classic PAT
    - ``ghu_``        -> OAuth user-to-server (e.g. ``gh auth login``)
    - ``gho_``        -> OAuth app token
    - ``ghs_``        -> GitHub App installation (server-to-server)
    - ``ghr_``        -> GitHub App refresh token
    """
    for prefix, kind in _TOKEN_PREFIXES.items():
        if token.startswith(prefix):
            return kind
    return "unknown"


def gitlab_rest_headers(
    token: str | None,
    *,
    oauth_bearer: bool = False,
) -> dict[str, str]:
    """Build HTTP headers for GitLab REST API v4 calls.

    Personal access tokens use ``PRIVATE-TOKEN``. OAuth2 access tokens
    typically use ``Authorization: Bearer <token>``; set *oauth_bearer*
    to use that style.

    Does not log or print *token*. Callers must not log the returned dict.
    """
    if not token:
        return {}
    if oauth_bearer:
        return {"Authorization": f"Bearer {token}"}
    return {"PRIVATE-TOKEN": token}
