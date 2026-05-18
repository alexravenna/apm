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

from collections.abc import Callable
from typing import TypeVar

from .class_ import BearerFallbackOutcome

T = TypeVar("T")


def _try_credential_fallback_impl(
    exc: Exception,
    self_: object,
    auth_ctx: object,
    host_info: object,
    operation: object,
    path: str | None,
    _log: object,
) -> T:
    """Inner logic for the ``_try_credential_fallback`` closure.

    Extracted from :func:`try_with_fallback` to reduce its McCabe complexity
    within the configured Ruff thresholds.
    """
    if auth_ctx.source in ("gh-auth-token", "git-credential-fill", "none"):
        raise exc
    # ADO uses ADO_APM_PAT + AAD bearer fallback; credential fill is out of scope.
    if host_info.kind == "ado":
        raise exc
    _log(
        f"Token from {auth_ctx.source} failed for {host_info.display_name}; "
        "trying secondary credential sources"
    )
    _log(f"trying gh auth token for {host_info.display_name}")
    gh_token = self_._token_manager.resolve_credential_from_gh_cli(host_info.host)
    if gh_token:
        _log(f"gh auth token resolved a credential for {host_info.display_name}")
        return operation(
            gh_token,
            self_._build_git_env(gh_token, scheme="basic", host_kind=host_info.kind),
        )
    path_suffix = f" (path={path})" if path else ""
    _log(f"trying git credential fill for {host_info.display_name}{path_suffix}")
    cred = self_._token_manager.resolve_credential_from_git(
        host_info.host, port=host_info.port, path=path
    )
    if cred:
        _log(f"git credential fill resolved a credential for {host_info.display_name}")
        return operation(
            cred,
            self_._build_git_env(cred, scheme="basic", host_kind=host_info.kind),
        )
    raise exc


def _try_ado_bearer_fallback_impl(
    exc: Exception,
    self_: object,
    operation: object,
    ado_bearer_fallback_available: bool,
    auth_ctx: object,
) -> T:
    """Inner logic for the ``_try_ado_bearer_fallback`` closure.

    Extracted from :func:`try_with_fallback` to reduce its McCabe complexity
    within the configured Ruff thresholds.
    """
    if not ado_bearer_fallback_available:
        raise exc
    from apm_cli.utils.github_host import is_ado_auth_failure_signal

    if not is_ado_auth_failure_signal(str(exc)):
        raise exc
    from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider

    provider = get_bearer_provider()
    if not provider.is_available():
        raise exc
    try:
        bearer = provider.get_bearer_token()
        bearer_env = self_._build_git_env(bearer, scheme="bearer", host_kind="ado")
        result = operation(bearer, bearer_env)
        # Success on fallback -- emit deferred diagnostic warning
        self_.emit_stale_pat_diagnostic(auth_ctx.host_info.display_name)
        return result
    except AzureCliBearerError:
        pass  # Bearer acquisition itself failed; fall through to original error
    except Exception:
        # Bearer also failed (Case 4). Re-raise the ORIGINAL PAT exception.
        pass
    raise exc


def try_with_fallback(
    self,
    host: str,
    operation: Callable[..., T],
    *,
    org: str | None = None,
    port: int | None = None,
    path: str | None = None,
    unauth_first: bool = False,
    verbose_callback: Callable[[str], None] | None = None,
) -> T:
    """Execute *operation* with automatic auth/unauth fallback.

    Parameters
    ----------
    host:
        Target git host.
    operation:
        ``operation(token, git_env) -> T`` -- the work to do.
    org:
        Optional organisation for per-org token lookup.
    path:
        Optional repository path (``org/repo``) included in the
        ``git credential fill`` request so helpers configured with
        ``credential.useHttpPath = true`` can disambiguate per-URL
        (notably Git Credential Manager for multi-account users).
    unauth_first:
        If *True*, try unauthenticated first (saves rate limits, EMU-safe).
    verbose_callback:
        Called with a human-readable step description at each attempt.

    When the resolved token comes from a global env var and fails
    (e.g. a github.com PAT tried on ``*.ghe.com``), the method
    retries with ``gh auth token`` and then ``git credential fill``
    before giving up.
    """
    auth_ctx = self.resolve(host, org, port=port)
    host_info = auth_ctx.host_info
    git_env = auth_ctx.git_env

    def _log(msg: str) -> None:
        if verbose_callback:
            verbose_callback(msg)

    def _try_credential_fallback(exc: Exception) -> T:
        """Retry the operation when the originally-resolved token fails.

        Walks the secondary chain in order: gh CLI (GitHub-like hosts;
        internal guard short-circuits unsupported hosts), then
        ``git credential fill`` (with ``path`` when known so
        helpers can disambiguate per-URL). Sources already obtained
        from a secondary chain (``gh-auth-token``,
        ``git-credential-fill``, ``none``) skip retry to avoid
        double-invocation.
        """
        return _try_credential_fallback_impl(exc, self, auth_ctx, host_info, operation, path, _log)

    # ADO bearer fallback machinery (PAT was tried first; bearer is the safety net)
    ado_bearer_fallback_available = (
        auth_ctx.host_info.kind == "ado" and auth_ctx.source == "ADO_APM_PAT"
    )

    def _try_ado_bearer_fallback(exc: Exception) -> T:
        """Retry ADO operation with AAD bearer when PAT fails with 401."""
        return _try_ado_bearer_fallback_impl(
            exc, self, operation, ado_bearer_fallback_available, auth_ctx
        )

    # Hosts that never have public repos -> auth-only
    if host_info.kind == "ghe_cloud":
        _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
        try:
            return operation(auth_ctx.token, git_env)
        except Exception as exc:
            return _try_credential_fallback(exc)

    # ADO: auth-first with bearer fallback when PAT fails
    if host_info.kind == "ado":
        _log(f"Auth-only attempt for {host_info.kind} host {host_info.display_name}")
        try:
            return operation(auth_ctx.token, git_env)
        except Exception as exc:
            return _try_ado_bearer_fallback(exc)

    if unauth_first:
        # Validation path: save rate limits, EMU-safe
        try:
            _log(f"Trying unauthenticated access to {host_info.display_name}")
            return operation(None, git_env)
        except Exception:
            if auth_ctx.token:
                _log(f"Unauthenticated failed, retrying with token (source: {auth_ctx.source})")
                try:
                    return operation(auth_ctx.token, git_env)
                except Exception as exc:
                    return _try_credential_fallback(exc)
            raise
    # Download path: auth-first for higher rate limits
    elif auth_ctx.token:
        try:
            _log(
                f"Trying authenticated access to {host_info.display_name} "
                f"(source: {auth_ctx.source})"
            )
            return operation(auth_ctx.token, git_env)
        except Exception as exc:
            if host_info.has_public_repos:
                _log("Authenticated failed, retrying without token")
                try:
                    return operation(None, git_env)
                except Exception:
                    return _try_credential_fallback(exc)
            return _try_credential_fallback(exc)
    else:
        _log(f"No token available, trying unauthenticated access to {host_info.display_name}")
        return operation(None, git_env)


def execute_with_bearer_fallback(
    self,
    dep_ref,
    primary_op,
    bearer_op,
    is_auth_failure,
) -> BearerFallbackOutcome:
    """Run ``primary_op``; on a confirmed auth failure for ADO, retry
    via AAD bearer using ``bearer_op(bearer_token)``.

    F1 #852: collapses the duplicated PAT->bearer fallback that used to
    live in both :meth:`try_with_fallback` (clone path) and
    ``install/validation.py::_validate_package_exists`` (ls-remote path).

    Args:
        dep_ref: DependencyReference -- only used to detect ADO and to
            supply the host display string for the deferred [!] warning.
        primary_op: Callable returning the primary outcome (typically a
            ``subprocess.CompletedProcess`` or any object). Whatever it
            returns is returned as-is on the no-fallback paths.
        bearer_op: Callable[[str], object] taking the freshly-acquired
            bearer JWT and returning the same outcome shape as
            ``primary_op``. Only invoked on a confirmed auth failure.
        is_auth_failure: Callable[[outcome], bool]. Receives whatever
            ``primary_op`` returned and decides whether the failure
            signature matches an ADO auth rejection (HTTP 401, "Authentication
            failed", etc.). Caller knows the outcome shape; resolver does not.

    Returns:
        :class:`BearerFallbackOutcome` carrying the final ``outcome``
        plus a ``bearer_attempted`` flag. The flag is True iff
        ``bearer_op`` was actually invoked (ADO + auth-failure signature
        + az provider available + JWT acquired) and lets callers
        distinguish "PAT rejected, bearer also rejected" from "PAT
        rejected, bearer never tried" for accurate diagnostics. Never
        raises (exceptions from ``bearer_op`` are swallowed).
    """
    primary = primary_op()
    if dep_ref is None or not getattr(dep_ref, "is_azure_devops", lambda: False)():
        return BearerFallbackOutcome(primary, False)
    if not is_auth_failure(primary):
        return BearerFallbackOutcome(primary, False)
    try:
        from apm_cli.core.azure_cli import AzureCliBearerError, get_bearer_provider
    except ImportError:
        return BearerFallbackOutcome(primary, False)
    provider = get_bearer_provider()
    if not provider.is_available():
        return BearerFallbackOutcome(primary, False)
    try:
        bearer = provider.get_bearer_token()
    except AzureCliBearerError:
        return BearerFallbackOutcome(primary, False)
    try:
        fallback = bearer_op(bearer)
    except Exception:
        return BearerFallbackOutcome(primary, True)
    if fallback is None or is_auth_failure(fallback):
        return BearerFallbackOutcome(primary, True)
    host_display = getattr(dep_ref, "host", None) or "dev.azure.com"
    self.emit_stale_pat_diagnostic(host_display)
    return BearerFallbackOutcome(fallback, True)
