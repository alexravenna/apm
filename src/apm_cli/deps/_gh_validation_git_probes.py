"""Git-protocol validation helpers for virtual-package existence probes.

Extracted from ``github_downloader_validation`` to keep that module under
the 500-line cap.  All public symbols are re-exported from the parent
module so callers import from ``apm_cli.deps.github_downloader_validation``
unchanged.

Contains the auth-chain construction and the two git-based fallback probes
(``ls-remote`` and shallow-fetch + ``ls-tree``) plus the SSH-attempt gate.
The Contents-API directory probe and the top-level orchestrator live in
``github_downloader_validation``.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

import git
from git.exc import GitCommandError

from ..utils.github_host import build_authorization_header_git_env

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference
    from .github_downloader import GitHubPackageDownloader


_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")


class AttemptSpec(NamedTuple):
    """A single (label, url, env) attempt in the auth-chain."""

    label: str
    url: str
    env: dict


def _is_sha_pin(ref: str) -> bool:
    """Return True when ``ref`` looks like an abbreviated or full git SHA."""
    return bool(_SHA_RE.fullmatch(ref))


def _build_validation_attempts(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    log: Callable[[str], None],
) -> list[AttemptSpec]:
    """Return the AttemptSpec chain for a probe against ``dep_ref``.

    Mirrors the auth chain in ``_clone_with_fallback`` and centralises the
    header-injection switch so both ``ls-remote`` and the shallow-fetch
    path probe reuse it.

    SECURITY (panel round-3 finding): for ALL HTTPS attempts (ADO and
    non-ADO) we inject credentials via ``http.extraheader`` rather than
    embedding them in the URL.  This keeps tokens out of the OS process
    table, git's own logs, and any temp ``.git/config`` written by the
    shallow-fetch probe.

    Auth scheme handling (panel round-3 ADO Basic finding):
      * ADO + ``auth_scheme == "basic"`` (PAT): ``Authorization: Basic
        base64(":" + PAT)`` per ADO's HTTP Basic convention.  A raw
        ``Bearer <PAT>`` is rejected with 401.
      * ADO + ``auth_scheme == "bearer"`` (AAD JWT): ``Authorization:
        Bearer <token>``.
      * GitLab: ``Authorization: Basic base64("oauth2:" + PAT)`` to match
        the GitLab HTTPS clone credential shape without putting the PAT in
        the URL.
      * Non-ADO with ``auth_scheme == "bearer"``: ``Authorization: Bearer
        <token>`` (matches GitHub recommendation for OAuth/App tokens).
      * Non-ADO with ``auth_scheme == "basic"`` (legacy classic PAT):
        ``Authorization: Bearer <token>`` -- GitHub accepts both forms;
        Bearer keeps the token out of any URL component.
    """
    if dep_ref.is_artifactory():
        return []

    dep_token: str | None = downloader._resolve_dep_token(dep_ref)
    dep_auth_ctx = downloader._resolve_dep_auth_ctx(dep_ref)
    dep_auth_scheme: str = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"
    is_insecure: bool = bool(getattr(dep_ref, "is_insecure", False))
    is_ado: bool = dep_ref.is_azure_devops()
    host_info = (
        downloader.auth_resolver.classify_host(dep_ref.host, port=dep_ref.port)
        if getattr(dep_ref, "host", None)
        else None
    )
    is_gitlab = host_info is not None and host_info.kind == "gitlab"

    attempts: list[AttemptSpec] = []

    # Attempt 1: explicit token, header-injected. Skipped when no token.
    if dep_token:
        if is_ado and dep_auth_scheme == "basic":
            # ADO PAT requires HTTP Basic with base64(":PAT"). A raw
            # Bearer header would 401 every ADO PAT user.
            encoded = base64.b64encode(f":{dep_token}".encode()).decode("ascii")
            auth_env = build_authorization_header_git_env("Basic", encoded)
            label = "ADO authenticated HTTPS (basic header)"
        elif is_ado:  # bearer (AAD JWT)
            auth_env = build_authorization_header_git_env("Bearer", dep_token)
            label = "ADO authenticated HTTPS (bearer header)"
        elif is_gitlab:
            encoded = base64.b64encode(f"oauth2:{dep_token}".encode()).decode("ascii")
            auth_env = build_authorization_header_git_env("Basic", encoded)
            label = "GitLab authenticated HTTPS (basic header)"
        else:
            # Non-ADO: header injection rather than URL embedding so the
            # token never appears in argv or temp .git/config.
            auth_env = build_authorization_header_git_env("Bearer", dep_token)
            label = "authenticated HTTPS (header)"

        token_env = {**downloader.git_env, **auth_env}
        token_url = downloader._build_repo_url(
            dep_ref.repo_url,
            use_ssh=False,
            dep_ref=dep_ref,
            token="",  # tokenless URL: credentials live in the env header
            auth_scheme=dep_auth_scheme if is_ado else "basic",
        )
        attempts.append(AttemptSpec(label, token_url, token_env))

    # Attempt 2: plain HTTPS w/ credential helper (no token, no header).
    plain_env = downloader._build_noninteractive_git_env(
        preserve_config_isolation=is_insecure,
        suppress_credential_helpers=is_insecure,
    )
    plain_url = downloader._build_repo_url(
        dep_ref.repo_url,
        use_ssh=False,
        dep_ref=dep_ref,
        token="",
    )
    attempts.append(AttemptSpec("plain HTTPS w/ credential helper", plain_url, plain_env))

    # Attempt 3 (SSH): only when allowed. StrictHostKeyChecking is
    # intentionally inherited from the user's ssh config; do NOT add
    # `-o StrictHostKeyChecking=no` thinking it's safer -- it isn't.
    if not is_insecure and _ssh_attempt_allowed(downloader):
        try:
            ssh_url = downloader._build_repo_url(
                dep_ref.repo_url,
                use_ssh=True,
                dep_ref=dep_ref,
            )
            ssh_env = dict(plain_env)
            ssh_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
            attempts.append(AttemptSpec("SSH", ssh_url, ssh_env))
        except Exception as exc:
            log(f"  [!] SSH URL build skipped: {exc}")

    return attempts


def _ref_exists_via_ls_remote(
    downloader: GitHubPackageDownloader,
    dep_ref: DependencyReference,
    ref: str,
    log: Callable[[str], None],
) -> tuple[bool, AttemptSpec | None]:
    """Check if ``ref`` exists in the remote repo via ``git ls-remote``.

    Lenient fallback for when the Contents API rejects a path with 404
    even though ``git clone`` would succeed -- e.g. SSO-half-authorized
    PATs, fine-grained PAT scope mismatches between API and git
    protocols, or repo policies that gate the Contents API more
    strictly than git.

    For SHA-pinned refs (hex-only, 7-40 chars) the ls-remote call omits
    ``--heads --tags`` because those filters silently drop commit SHAs
    -- the full ref list is scanned for a SHA-prefix match instead.

    Returns:
        ``(True, winning_attempt)`` on the first attempt that resolves
        the ref; ``(False, None)`` if every attempt fails. Callers MUST
        reuse ``winning_attempt`` for any follow-up probe at the same
        ref so the auth-chain promise holds end-to-end (panel round-3:
        if ls-remote succeeded via SSH but the follow-up probe used the
        rejected PAT, the fallback would silently false-reject).
    """
    attempts = _build_validation_attempts(downloader, dep_ref, log)
    if not attempts:
        return False, None

    is_sha = _is_sha_pin(ref)
    ref_lc = ref.lower()
    g = git.cmd.Git()
    for attempt in attempts:
        label, url, env = attempt
        try:
            if is_sha:
                # SHA pins: scan the full advertised-refs list.  The
                # ``--heads --tags`` filters scan only ``refs/heads/*``
                # and ``refs/tags/*`` and silently drop commit SHAs.
                output = g.ls_remote(url, env=env)
                if output and any(
                    line.split("\t", 1)[0].lower().startswith(ref_lc)
                    for line in output.splitlines()
                    if line
                ):
                    log(f"  [+] ls-remote ok via {label}")
                    return True, attempt
                log(f"  [!] ls-remote returned no SHA match via {label}")
            else:
                output = g.ls_remote("--heads", "--tags", url, ref, env=env)
                if output and output.strip():
                    log(f"  [+] ls-remote ok via {label}")
                    return True, attempt
                log(f"  [!] ls-remote returned no matching refs via {label}")
        except (GitCommandError, OSError) as exc:
            log(f"  [x] ls-remote failed via {label}: {downloader._sanitize_git_error(str(exc))}")

    return False, None


def _ssh_attempt_allowed(downloader: GitHubPackageDownloader) -> bool:
    """Whether the SSH ls-remote attempt should run.

    Mirrors ``_clone_with_fallback``'s gating: SSH is in scope when the
    user explicitly preferred it (``--ssh``) or when cross-protocol
    fallback is allowed.  Default HTTPS-preferring users get no SSH
    attempt -- keeps validation output clean and never invokes ssh on
    machines that don't have it configured.
    """
    try:
        from .transport_selection import ProtocolPreference
    except ImportError:
        return False
    return downloader._protocol_pref == ProtocolPreference.SSH or downloader._allow_fallback
