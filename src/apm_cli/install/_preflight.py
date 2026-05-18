"""Pre-flight authentication check for ``apm install --update`` (#1015).

Private helper extracted from :mod:`apm_cli.install.pipeline` to keep
``pipeline.py`` under 500 lines.  Import ``_preflight_auth_check`` from
``apm_cli.install.pipeline`` (it is re-exported there) rather than
importing from this module directly.
"""

from __future__ import annotations

import builtins
import contextlib

from .errors import AuthenticationError


def _preflight_auth_check(ctx, auth_resolver, verbose: bool) -> None:
    """Verify auth for every distinct (host, org) before write phases.

    Called only when ``update_refs`` is set, so we know the pipeline is
    about to overwrite ``apm.yml``, ``apm.lock.yaml``, and
    ``apm_modules/``.  A single ``git ls-remote`` per cluster catches
    stale tokens before any file is touched.

    For ADO clusters, a stale ``ADO_APM_PAT`` automatically falls back
    to an ``az cli`` AAD bearer via :meth:`AuthResolver.execute_with_bearer_fallback`
    -- matching the protocol used by the actual clone path. Without this,
    ``apm install -g`` (which skipped preflight) would succeed but
    ``apm install -g --update`` would fail on the same machine with the
    same creds. See #1212.

    Raises :class:`AuthenticationError` (with ``build_error_context``
    payload) on the first auth failure that survives the fallback.
    """
    import os
    import subprocess as _sp

    from ..utils.github_host import (
        is_ado_auth_failure_signal,
        is_azure_devops_hostname,
        is_github_hostname,
    )

    logger = getattr(ctx, "logger", None)

    def _trace(line: str) -> None:
        """Emit a verbose tracing line; best-effort, never raises."""
        if not verbose or logger is None:
            return
        with contextlib.suppress(Exception):
            logger.verbose_detail(line)

    seen: builtins.set = builtins.set()
    for dep in ctx.deps_to_install:
        host = dep.host
        if not host or is_github_hostname(host):
            continue  # github.com uses API probe with unauth fallback
        org = dep.repo_url.split("/")[0] if dep.repo_url and "/" in dep.repo_url else None
        key = (host, org)
        if key in seen:
            continue
        seen.add(key)

        dep_ctx = auth_resolver.resolve_for_dep(dep)
        _auth_scheme = getattr(dep_ctx, "auth_scheme", "basic") or "basic"

        from ..deps.github_downloader import GitHubPackageDownloader

        _dl = GitHubPackageDownloader(auth_resolver=auth_resolver)
        _dl.github_host = host
        probe_url = _dl._build_repo_url(
            dep.repo_url,
            use_ssh=False,
            dep_ref=dep,
            token=dep_ctx.token,
            auth_scheme=_auth_scheme,
        )
        _ctx_env = getattr(dep_ctx, "git_env", {}) or {}
        probe_env = {**os.environ, **_dl.git_env, **_ctx_env}
        is_generic = not is_github_hostname(host) and not is_azure_devops_hostname(host)
        if is_generic:
            for _key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_ASKPASS"):
                probe_env.pop(_key, None)

        host_display = host if not org else f"{host}/{org}"

        def _run_ls_remote(url, env):
            # auth-delegated: invoked via _primary_op/_bearer_op below, both
            # routed through auth_resolver.execute_with_bearer_fallback.
            try:
                return _sp.run(
                    ["git", "ls-remote", "--heads", "--exit-code", url],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    env=env,
                )
            except _sp.TimeoutExpired:
                return None  # network timeout sentinel; treated as non-auth

        def _primary_op(url=probe_url, env=probe_env):
            return _run_ls_remote(url, env)

        def _bearer_op(
            bearer, dep=dep, dep_ctx=dep_ctx, host=host, host_display=host_display, _dl=_dl
        ):
            # SECURITY: build a CLEAN env via _build_git_env(scheme="bearer")
            # rather than {**probe_env, **build_ado_bearer_git_env(bearer)}.
            # probe_env carries GIT_TOKEN=<stale-PAT> from dep_ctx.git_env;
            # leaving it set during the bearer attempt would leak the
            # rejected PAT into the child-process env table even though the
            # GIT_CONFIG_VALUE_0 header carries the bearer. _build_git_env
            # explicitly skips GIT_TOKEN for scheme="bearer".
            bearer_env = auth_resolver._build_git_env(bearer, scheme="bearer", host_kind="ado")
            bearer_url = _dl._build_repo_url(
                dep.repo_url,
                use_ssh=False,
                dep_ref=dep,
                token=None,
                auth_scheme="bearer",
            )
            _trace(f"Preflight: {host_display} -- retrying with az cli bearer")
            return _run_ls_remote(bearer_url, bearer_env)

        def _is_auth_failure(outcome):
            if outcome is None:
                return False  # timeout: not an auth failure
            if outcome.returncode == 0:
                return False
            return is_ado_auth_failure_signal(outcome.stderr or "")

        ado_eligible = (
            dep.is_azure_devops()
            and _auth_scheme == "basic"
            and getattr(dep_ctx, "source", None) == "ADO_APM_PAT"
        )

        if ado_eligible:
            fallback_result = auth_resolver.execute_with_bearer_fallback(
                dep,
                _primary_op,
                _bearer_op,
                _is_auth_failure,
            )
            result = fallback_result.outcome
            # bearer_also_failed is True only when the bearer leg actually
            # ran AND its outcome still matched the auth-failure signature.
            # Early returns from execute_with_bearer_fallback (az
            # unavailable, JWT acquisition failed) leave bearer_attempted
            # False so the diagnostic does not falsely claim an attempt.
            bearer_also_failed = (
                fallback_result.bearer_attempted
                and result is not None
                and result.returncode != 0
                and is_ado_auth_failure_signal(result.stderr or "")
            )
        else:
            result = _primary_op()
            bearer_also_failed = False

        if result is None:
            continue  # timeout fallthrough -- handled by the real phase

        if result.returncode != 0:
            if not is_ado_auth_failure_signal(result.stderr or ""):
                continue  # non-auth git failure (network, ref-not-found) -- defer
            _trace(f"Preflight: {host_display} -- auth rejected")
            _diag = auth_resolver.build_error_context(
                host,
                "install --update",
                org=org,
                dep_url=dep.repo_url,
                bearer_also_failed=bearer_also_failed,
            )
            raise AuthenticationError(
                f"Authentication failed for {host}",
                diagnostic_context=(
                    _diag
                    + "\n\n    No files were modified."
                    + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                ),
            )
        else:
            _trace(f"Preflight: {host_display} -- accepted")
