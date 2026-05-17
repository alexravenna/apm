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
import sys
from typing import TypeVar

from .class_ import _org_to_env_suffix

T = TypeVar("T")


def build_error_context(
    self,
    host: str,
    operation: str,
    org: str | None = None,
    *,
    port: int | None = None,
    dep_url: str | None = None,
    bearer_also_failed: bool = False,
) -> str:
    """Build an actionable error message for auth failures.

    ``bearer_also_failed=True`` prepends a single line to the Case 4
    block (PAT set, az available, both attempts failed) clarifying
    that ADO_APM_PAT was tried first and rejected before the bearer
    attempt -- so the user understands why both halves of the
    protocol failed without having to read the full diagnostic
    context. Callers MUST only set this when the bearer attempt
    actually ran (see :class:`BearerFallbackOutcome.bearer_attempted`).
    """
    auth_ctx = self.resolve(host, org, port=port)
    host_info = auth_ctx.host_info
    display = host_info.display_name

    # --- ADO-specific error cases ---
    if host_info.kind == "ado":
        from apm_cli.core.azure_cli import get_bearer_provider

        provider = get_bearer_provider()
        az_available = provider.is_available()
        pat_set = bool(os.environ.get("ADO_APM_PAT"))

        org_part = org or ""
        if not org_part:
            source_url = dep_url or ""
            if source_url:
                parts = source_url.replace("https://", "").split("/")
                if len(parts) >= 2 and (
                    parts[0] in ("dev.azure.com",) or parts[0].endswith(".visualstudio.com")
                ):
                    org_part = parts[1] if len(parts) > 1 else ""

        token_url = (
            f"https://dev.azure.com/{org_part}/_usersSettings/tokens"
            if org_part
            else "https://dev.azure.com/<org>/_usersSettings/tokens"
        )

        if pat_set:
            if az_available:
                # Case 4: PAT and bearer were both available; both attempts
                # failed. We may not have observed an explicit 401 (could be
                # a 404, a network error, etc.) so the wording stays
                # tentative -- see #856 review C6.
                prefix = (
                    "    ADO_APM_PAT was rejected; az cli bearer was also rejected.\n\n"
                    if bearer_also_failed
                    else ""
                )
                return (
                    f"\n{prefix}"
                    f"    ADO_APM_PAT is set, and Azure CLI credentials may also be available,\n"
                    f"    but the Azure DevOps request still failed.\n\n"
                    f"    If this is an authentication failure, the PAT may be expired, revoked,\n"
                    f"    or scoped to a different org, and Azure CLI credentials may need to\n"
                    f"    be refreshed.\n\n"
                    f"    To fix:\n"
                    f"      1. Unset the PAT to test Azure CLI auth only:  unset ADO_APM_PAT\n"
                    f"      2. Re-authenticate Azure CLI if needed:        az login\n"
                    f"      3. Retry:                                       apm install\n\n"
                    f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
                )
            # PAT set but rejected, no az -> bare PAT failure
            return (
                f"\n    ADO_APM_PAT is set, but the Azure DevOps request failed.\n"
                f"    If this is an authentication failure, the token may be expired,\n"
                f"    revoked, or scoped to a different org.\n\n"
                f"    Generate a new PAT at {token_url}\n"
                f"    with Code (Read) scope.\n\n"
                f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # No PAT set
        if not az_available:
            # Case 1: no az, no PAT
            return (
                f"\n    Azure DevOps requires authentication. You have two options:\n\n"
                f"    1. Install Azure CLI and sign in (recommended for Entra ID users):\n"
                f"         brew install azure-cli            # macOS\n"
                f"         winget install Microsoft.AzureCLI # Windows\n"
                f"         apt-get install azure-cli         # Debian/Ubuntu\n"
                f"         dnf install azure-cli             # Fedora/RHEL\n"
                f"         (full guide: https://aka.ms/InstallAzureCli)\n"
                f"         az login\n"
                f"         apm install                   # retry -- no env var needed\n\n"
                f"    2. Use a Personal Access Token:\n"
                f"         export ADO_APM_PAT=your_token\n"
                f"         (Create one at {token_url} with Code (Read) scope.)\n\n"
                f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # az is available; check if logged in by trying to get tenant
        tenant = provider.get_current_tenant_id()
        if tenant is None:
            # Case 3: az present, not logged in
            return (
                "\n    Azure DevOps requires authentication. You have two options:\n\n"
                "    1. Sign in with Azure CLI (recommended for Entra ID users):\n"
                "         az login\n"
                "         apm install                   # retry -- no env var needed\n\n"
                "    2. Use a Personal Access Token:\n"
                "         export ADO_APM_PAT=your_token\n\n"
                "    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
            )

        # Case 2: az returned token (tenant known) but ADO rejected it.
        # Note: bearer_also_failed=True is structurally unreachable here --
        # callers only set it when source == "ADO_APM_PAT" (i.e. pat_set
        # is True), and Case 2 lives in the `not pat_set` branch. We do
        # not render a "PAT was also rejected" prefix in this case
        # because no PAT was tried.
        return (
            f"\n    Your az cli session (tenant: {tenant}) returned a bearer token,\n"
            f"    but Azure DevOps rejected it (HTTP 401).\n\n"
            f"    Check that you are signed into the correct tenant:\n"
            f"      az account show\n"
            f"      az login --tenant <correct-tenant-id>\n\n"
            f"    Docs: https://microsoft.github.io/apm/getting-started/authentication/#azure-devops"
        )

    # --- Non-ADO error paths ---
    lines: list[str] = [f"Authentication failed for {operation} on {display}."]

    if auth_ctx.token:
        lines.append(
            f"Token was provided (source: {auth_ctx.source}, type: {auth_ctx.token_type})."
        )
        if host_info.kind == "ghe_cloud":
            lines.append(
                "GHE Cloud Data Residency hosts (*.ghe.com) require "
                "enterprise-scoped tokens. Ensure your PAT is authorized "
                "for this enterprise."
            )
        elif host_info.kind == "gitlab":
            lines.append(
                "Ensure your GitLab personal or project access token meets the "
                "API read requirements for your instance policy."
            )
        elif host.lower() == "github.com":
            lines.append(
                "If your organization uses SAML SSO or is an EMU org, "
                "ensure your PAT is authorized at "
                "https://github.com/settings/tokens"
            )
        elif host_info.kind == "generic":
            lines.append("Verify credentials for this host in your git credential helper.")
        else:
            lines.append(
                "If your organization uses SAML SSO, you may need to "
                "authorize your token at https://github.com/settings/tokens"
            )
    else:
        lines.append("No token available.")
        if host_info.kind == "gitlab":
            lines.append(
                "Set GITLAB_APM_PAT or GITLAB_TOKEN, or configure git credential fill "
                f"for {display}."
            )
        elif host_info.kind == "generic":
            lines.append(
                "APM does not apply GitHub PAT environment variables to generic git "
                f"hosts; configure git credential fill for {display} or use a "
                "public repository if available."
            )
        else:
            lines.append("Set GITHUB_APM_PAT or GITHUB_TOKEN, or run 'gh auth login'.")

    if org and host_info.kind not in ("ado", "gitlab", "generic"):
        lines.append(
            f"If packages span multiple organizations, set per-org tokens: "
            f"GITHUB_APM_PAT_{_org_to_env_suffix(org)}"
        )

    # When a custom port is in play, helpers that key by hostname alone
    # (some `gh` integrations, older keychain backends) can silently
    # return the wrong credential. Point the user at the concrete fix.
    if host_info.port is not None:
        lines.append(
            f"[i] Host '{display}' -- verify your credential helper stores per-port entries "
            f"(some helpers key by host only)."
        )

    lines.append("Run with --verbose for detailed auth diagnostics.")
    return "\n".join(lines)


def emit_stale_pat_diagnostic(self, host_display: str) -> None:
    """Emit a [!] warning when PAT was rejected but bearer succeeded.

    F3 #852: when an InstallLogger is wired via :meth:`set_logger`, the
    warning is collected by its DiagnosticCollector so it appears in the
    install summary. Without a logger (e.g. unit tests) we fall back to
    the inline ``_rich_warning`` emission for backwards compatibility.

    #1212 follow-up: dedup per host_display so the user sees ONE warning
    per ADO host even when preflight, list_remote_refs, and the clone
    path each trigger the bearer-fallback path against the same host.

    Naming: previously ``_emit_stale_pat_diagnostic`` (private). Public
    now (#856 follow-up C9) so external modules (validation.py,
    github_downloader.py) do not reach into the underscore API.

    #1214 follow-up: guard the check-then-add under self._lock so two
    threads (parallel install) racing on the same ADO host cannot both
    pass the membership check before either calls add(); without the
    lock the dedup set defeats its own purpose.
    """
    with self._lock:
        if host_display in self._stale_pat_warned_hosts:
            return
        self._stale_pat_warned_hosts.add(host_display)
    msg = f"ADO_APM_PAT was rejected for {host_display}; fell back to az cli bearer."
    detail = "Consider unsetting the stale variable."
    diagnostics = self._diagnostics_or_none()
    if diagnostics is not None:
        diagnostics.warn(msg, detail=detail)
        return
    try:
        from apm_cli.utils.console import _rich_warning

        _rich_warning(msg, symbol="warning")
        _rich_warning(f"    {detail}", symbol="warning")
    except ImportError:
        pass  # console module not importable in some test contexts


def notify_auth_source(self, host_display: str, ctx) -> None:
    """Emit the verbose auth-source line for ``host_display`` exactly once.

    F2 #852: routes through CommandLogger when wired (so the line obeys
    the same verbose channel as every other diagnostic), and falls back
    to a direct stderr write when no logger is set so the existing
    bearer e2e tests keep working.
    """
    host_key = (host_display or "").lower()
    if not host_key or host_key in self._verbose_auth_logged_hosts:
        return
    self._verbose_auth_logged_hosts.add(host_key)
    if ctx is None or getattr(ctx, "source", "none") == "none":
        return
    if getattr(ctx, "auth_scheme", None) == "bearer":
        line = f"  [i] {host_key} -- using bearer from az cli (source: {ctx.source})"
    else:
        line = f"  [i] {host_key} -- token from {ctx.source}"
    if self._logger is not None and getattr(self._logger, "verbose", False):
        try:
            from apm_cli.utils.console import _rich_echo

            _rich_echo(line, color="dim")
            return
        except ImportError:
            pass
    # No logger wired -- the install path always wires one in the
    # bearer branch, so this fallback only fires in unit-test contexts
    # that opt-in via APM_VERBOSE=1.
    sys.stderr.write(line + "\n")
