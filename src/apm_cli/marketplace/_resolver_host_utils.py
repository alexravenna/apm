"""Host-matching and canonical-normalisation helpers for the marketplace resolver.

These are all *private* to the marketplace package.  They are imported by
``resolver.py`` which re-exports any symbol tests reference directly so that
``from apm_cli.marketplace.resolver import _X`` keeps working.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..models.dependency.reference import DependencyReference
from ..utils.github_host import is_azure_devops_hostname, is_github_hostname
from ._resolver_models import CrossRepoMisconfigRisk
from .models import MarketplacePlugin, MarketplaceSource

# ---------------------------------------------------------------------------
# Slug normalisation
# ---------------------------------------------------------------------------


def _normalize_owner_repo_slug(repo: str) -> str:
    """Lowercase ``owner/repo`` slug with optional ``.git`` suffix stripped."""
    r = repo.strip().rstrip("/").lower()
    if r.endswith(".git"):
        r = r[:-4]
    return r


def _marketplace_project_slug(owner: str, repo: str) -> str:
    return _normalize_owner_repo_slug(f"{owner}/{repo}")


def _normalize_repo_field_for_match(repo_field: str, marketplace_host: str) -> str:
    """Normalize a repo field to a logical project path for matching.

    Accept bare ``owner/repo`` paths, host-qualified shorthand like
    ``git.epam.com/owner/repo``, and URL / SSH forms. If the field explicitly names
    a different host than the marketplace host, return an empty string so it does
    not match by suffix alone.
    """
    raw = repo_field.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]

    host_l = marketplace_host.strip().lower()

    if raw.startswith(("http://", "https://", "ssh://")):
        parsed = urlparse(raw)
        parsed_host = (parsed.hostname or "").strip().lower()
        if parsed_host and parsed_host != host_l:
            return ""
        return parsed.path.lstrip("/").lower()

    if raw.startswith("git@") and ":" in raw:
        host_part, path_part = raw[4:].split(":", 1)
        if host_part.strip().lower() != host_l:
            return ""
        return path_part.lstrip("/").lower()

    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 3 and parts[0].strip().lower() == host_l:
        parts = parts[1:]
    return "/".join(parts).lower()


def _repo_field_matches_marketplace(
    repo_field: str, owner: str, repo: str, marketplace_host: str
) -> bool:
    """True if dict ``repo`` identifies the same project as the marketplace source."""
    if not repo_field or "/" not in repo_field:
        return False
    normalized_repo = _normalize_repo_field_for_match(repo_field, marketplace_host)
    if not normalized_repo:
        return False
    return normalized_repo == _marketplace_project_slug(owner, repo)


# ---------------------------------------------------------------------------
# Plugin source-type inference
# ---------------------------------------------------------------------------


def _coerce_dict_plugin_type(s: dict) -> str:
    """Return normalized source ``type`` for a plugin entry dict (``type`` / ``source`` / ``kind``).

    ``type`` is case-insensitive. When it is missing, infers ``github`` or
    ``git-subdir`` from ``repo`` plus path fields so in-marketplace matching and
    ``path``/``subdir`` extraction match manifests that only set ``kind`` or omit
    ``type`` (still require a valid ``repo`` for dict sources).
    """
    for key in ("type", "source", "kind"):
        v = s.get(key, "")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    repo = s.get("repo", "")
    if not isinstance(repo, str) or "/" not in repo.strip():
        return ""
    subdir = s.get("subdir", "")
    if isinstance(subdir, str) and subdir.strip():
        return "git-subdir"
    path = s.get("path", "")
    if isinstance(path, str) and path.strip():
        return "github"
    return "github"


def _is_in_marketplace_source(plugin: MarketplacePlugin, source: MarketplaceSource) -> bool:
    """Per spec §Interface Contract — in-marketplace detection."""
    s = plugin.source
    if s is None:
        return False
    if isinstance(s, str):
        return True
    if not isinstance(s, dict):
        return False
    source_type = _coerce_dict_plugin_type(s)
    if source_type in ("github", "git-subdir", "gitlab"):
        return _repo_field_matches_marketplace(
            s.get("repo", ""), source.owner, source.repo, source.host
        )
    return False


# ---------------------------------------------------------------------------
# Host-capability detection
# ---------------------------------------------------------------------------


def _marketplace_host_needs_explicit_git_path(host: str) -> bool:
    """True when in-repo marketplace plugins must use ``git`` + ``path`` (clone root + subdir).

    ``github.com`` and ``*.ghe.com`` virtual shorthand is reliable. Azure DevOps uses
    a different URL shape and is excluded. Self-managed GitLab FQDNs are often
    classified as ``generic`` by :meth:`AuthResolver.classify_host` when not listed in
    ``GITLAB_HOST`` / ``APM_GITLAB_HOSTS`` — they still need explicit clone URLs so
    paths like ``registry/pkg`` are not treated as extra project namespace segments.
    """
    if not host or not str(host).strip():
        return False
    h = str(host).strip().split("/", 1)[0]
    if is_azure_devops_hostname(h):
        return False
    return not is_github_hostname(h)


def _needs_canonical_host_prefix(canonical: str, host: str) -> bool:
    """True when a GitHub-family enterprise host must be prefixed to ``canonical``.

    GitHub-family hosts (``github.com`` + ``*.ghe.com``) keep virtual shorthand --
    ``resolve_plugin_source`` emits a bare ``owner/repo[/path]`` canonical because
    there is no nested-group ambiguity to disambiguate. ``DependencyReference.parse``
    defaults missing hosts to ``github.com``, which is correct for ``github.com`` but
    silently mis-routes auth for every ``*.ghe.com`` marketplace.

    Returns True only for enterprise GitHub hosts (``*.ghe.com``) so the caller can
    backfill the host while preserving shorthand semantics. Idempotent: when the
    canonical already starts with ``host`` (case-insensitive) -- as happens when the
    manifest's dict source carries a host-qualified ``repo`` -- this returns False
    so the prefix is not duplicated.

    GHES (GitHub Enterprise Server, configured via ``GITHUB_HOST``) is not handled
    here. Those hosts return True from ``_marketplace_host_needs_explicit_git_path``
    (neither GitHub-family nor ADO) so ``resolve_marketplace_plugin`` builds a
    structured ``dep_ref`` upstream and this helper is never reached. The
    ``is_github_hostname`` check below is defense-in-depth that would also reject
    them if a future change ever bypassed the upstream guard.

    Also returns False when ``canonical`` is in URL form (``https://...``) or SSH
    SCP shorthand (``git@host:owner/repo``). Manifests that put a full URL in the
    ``repo`` field reach this point via ``_resolve_github_source`` (which only
    requires a ``/``); detecting those by ``":"`` in the first slash-split segment
    avoids producing malformed ``host/https://...`` canonicals. Those forms already
    carry a host and ``DependencyReference.parse`` resolves them natively.
    """
    h = (host or "").strip()
    if not h or not is_github_hostname(h) or h.lower() == "github.com":
        return False
    first_segment = canonical.split("/", 1)[0]
    if ":" in first_segment:
        return False
    return first_segment.lower() != h.lower()


# ---------------------------------------------------------------------------
# Cross-repo misconfiguration sentinel
# ---------------------------------------------------------------------------


def _compute_cross_repo_misconfig_risk(
    plugin: MarketplacePlugin,
    source: MarketplaceSource,
    canonical: str,
    dep_ref: DependencyReference | None,
) -> CrossRepoMisconfigRisk | None:
    """Identify the #1305 misconfiguration: cross-repo dict ``type: github``
    source with bare ``repo`` on an enterprise GitHub-family marketplace.

    Returns a :class:`CrossRepoMisconfigRisk` when **all** of:

    - ``dep_ref`` is ``None`` (GitHub-family virtual-shorthand path; GitLab and
      self-managed FQDNs build a structured ref upstream and sidestep the bug)
    - ``plugin.source`` is a dict whose normalized type is ``github`` (other
      dict types -- ``gitlab``, ``git-subdir`` -- hit the same auth-routing
      bug but the "host-qualify with marketplace host" remediation only
      matches operator intent for the GitHub family)
    - the source is **not** an in-marketplace reference (PR #1292 already
      backfills the host for those)
    - ``_needs_canonical_host_prefix`` agrees the canonical is bare and the
      host is GitHub-family enterprise (``*.ghe.com``; idempotent against
      already host-qualified, URL, and SSH forms)
    - the ``repo`` field is a non-empty ``owner/repo`` shorthand

    Otherwise returns ``None``. Pure -- no logging, no side effects.
    """
    if dep_ref is not None or not isinstance(plugin.source, dict):
        return None
    if _coerce_dict_plugin_type(plugin.source) != "github":
        return None
    if _is_in_marketplace_source(plugin, source):
        return None
    if not _needs_canonical_host_prefix(canonical, source.host):
        return None
    repo_field = plugin.source.get("repo", "")
    bare = repo_field.strip().lstrip("/") if isinstance(repo_field, str) else ""
    if not bare or "/" not in bare:
        return None
    return CrossRepoMisconfigRisk(
        marketplace_host=source.host,
        bare_repo_field=bare,
        suggested_qualified_repo=f"{source.host}/{bare}",
    )
