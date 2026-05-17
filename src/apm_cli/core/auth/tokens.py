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

from apm_cli.core.token_manager import GitHubTokenManager

from .class_ import HostInfo, _org_to_env_suffix

T = TypeVar("T")


def _resolve_token(self, host_info: HostInfo, org: str | None) -> tuple[str | None, str, str]:
    """Walk the token resolution chain.  Returns (token, source, scheme).

    Resolution order (GitHub-class: ``github``, ``ghe_cloud``, ``ghes``):
    1. Per-org ``GITHUB_APM_PAT_{ORG}`` when *org* is set
    2. ``GITHUB_APM_PAT`` -> ``GITHUB_TOKEN`` -> ``GH_TOKEN``
    3. ``gh auth token --hostname <host>`` (gh CLI active account)
    4. Host-specific git credential helper

    Resolution order (``gitlab``): ``GITLAB_APM_PAT`` → ``GITLAB_TOKEN`` →
    credential helper. GitHub env vars are not consulted.

    Resolution order (``generic``): credential helper only (no GitHub or
    GitLab platform env vars).

    Resolution order (ADO): ``ADO_APM_PAT`` → AAD bearer → ``none``.

    All token-bearing requests use HTTPS.
    """
    if host_info.kind == "ado":
        # ADO resolution chain: PAT env -> AAD bearer -> none
        pat = os.environ.get("ADO_APM_PAT")
        if pat:
            return pat, "ADO_APM_PAT", "basic"
        # Try AAD bearer via az cli (lazy import to avoid module-load cost on non-ADO paths)
        from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

        provider = get_bearer_provider()
        if provider.is_available():
            try:
                bearer = provider.get_bearer_token()
                return bearer, GitHubTokenManager.ADO_BEARER_SOURCE, "bearer"
            except AzureCliBearerError:
                # az is on PATH but token acquisition failed (e.g., not logged in).
                # Fall through to token=None; build_error_context will render Case 3.
                pass
        return None, "none", "basic"

    # ADO uses ADO_APM_PAT (single var) + AAD bearer fallback;
    # per-org vars and credential fill are out of scope.

    # 1. Per-org GitHub PAT (GitHub-class hosts only — not GitLab / generic / ADO)
    if org and host_info.kind in ("github", "ghe_cloud", "ghes"):
        env_name = f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
        token = os.environ.get(env_name)
        if token:
            return token, env_name, "basic"

    # 2. Global env vars by host class
    purpose = self._purpose_for_host(host_info)
    token = self._token_manager.get_token_for_purpose(purpose)
    if token:
        source = self._identify_env_source(purpose)
        return token, source, "basic"

    # 3. gh CLI active account (eligibility gated inside the call;
    #    unsupported hosts return None instantly without a subprocess)
    gh_token = self._token_manager.resolve_credential_from_gh_cli(host_info.host)
    if gh_token:
        return gh_token, "gh-auth-token", "basic"

    # 4. Git credential helper (not for ADO)
    if host_info.kind not in ("ado",):
        # Note: path= is intentionally omitted here. _resolve_token is the
        # primary credential-resolution leg invoked once per host; it has
        # no per-call repository context. The fallback leg in
        # _try_credential_fallback re-invokes resolve_credential_from_git
        # WITH path= when the primary credential is rejected, so GCM
        # multi-account users still get per-URL disambiguation -- they
        # just pay one extra round-trip on the first miss. Adding path=
        # here would require threading repo context through every
        # resolve() call site, which is disproportionate to the benefit.
        credential = self._token_manager.resolve_credential_from_git(
            host_info.host, port=host_info.port
        )
        if credential:
            return credential, "git-credential-fill", "basic"

    return None, "none", "basic"


def _purpose_for_host(host_info: HostInfo) -> str:
    if host_info.kind == "ado":
        return "ado_modules"
    if host_info.kind == "gitlab":
        return "gitlab_modules"
    if host_info.kind == "generic":
        return "generic_modules"
    return "modules"


def _identify_env_source(self, purpose: str) -> str:
    """Return the name of the first env var that matched for *purpose*."""
    for var in self._token_manager.TOKEN_PRECEDENCE.get(purpose, []):
        if os.environ.get(var):
            return var
    return "env"


def _build_git_env(
    token: str | None = None,
    *,
    scheme: str = "basic",
    host_kind: str = "github",
) -> dict:
    """Pre-built env dict for subprocess git calls.

    For ADO bearer tokens (scheme='bearer'), injects an Authorization header
    via GIT_CONFIG_COUNT/KEY/VALUE env vars (see github_host.build_ado_bearer_git_env).
    For all other cases, behavior is unchanged.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    if scheme == "bearer" and token and host_kind == "ado":
        # B2 #852: skip GIT_TOKEN for bearer scheme -- the JWT is injected via
        # GIT_CONFIG_VALUE_0 only; GIT_TOKEN here would leak it into every
        # child-process env (visible in /proc/<pid>/environ, ps eww).
        #
        # #1214 follow-up: a stale GIT_TOKEN already in the parent env
        # (set by a prior shell, CI step, or another tool) would survive
        # the os.environ.copy() above and defeat the isolation guarantee.
        # Drop it explicitly so the bearer env is clean by construction.
        env.pop("GIT_TOKEN", None)
        from apm_cli.utils.github_host import build_ado_bearer_git_env

        env.update(build_ado_bearer_git_env(token))
    elif token:
        env["GIT_TOKEN"] = token
    return env


def _diagnostics_or_none(self):
    """Return the wired logger's DiagnosticCollector, or None."""
    if self._logger is None:
        return None
    try:
        return self._logger.diagnostics
    except AttributeError:
        return None
