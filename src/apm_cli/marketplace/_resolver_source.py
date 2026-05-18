"""Source-resolution helpers for the marketplace resolver.

Converts raw ``MarketplacePlugin.source`` fields to canonical
``owner/repo[/path][#ref]`` strings (or structured ``DependencyReference``
objects for GitLab-class hosts).

All helpers in this module are *private* to the marketplace package.
They are imported by ``resolver.py`` which re-exports any symbol that tests
reference directly so ``from apm_cli.marketplace.resolver import _X`` works.
"""

from __future__ import annotations

from urllib.parse import quote

from ..models.dependency.reference import DependencyReference
from ..utils.path_security import PathTraversalError, validate_path_segments
from ._resolver_host_utils import _coerce_dict_plugin_type
from .models import MarketplacePlugin, MarketplaceSource

# ---------------------------------------------------------------------------
# GitLab / explicit-git helpers
# ---------------------------------------------------------------------------


def _marketplace_https_git_url(source: MarketplaceSource) -> str:
    """HTTPS clone URL for the registered marketplace project."""
    segments = [p for p in f"{source.owner}/{source.repo}".split("/") if p]
    encoded = "/".join(quote(seg, safe="") for seg in segments)
    return f"https://{source.host}/{encoded}.git"


def _from_string_src(src: str, plugin_root: str) -> tuple[str | None, str | None]:
    """Extract ``(in_repo_path, ref)`` for a string-form plugin source."""
    rel = src.strip("/")
    if rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")

    if plugin_root and rel and rel != "." and "/" not in rel:
        root = plugin_root.strip("/")
        if root.startswith("./"):
            root = root[2:]
        root = root.strip("/")
        if root:
            rel = f"{root}/{rel}"

    if not rel or rel == ".":
        return None, None
    validate_path_segments(rel, context="relative source path")
    return rel, None


def _from_dict_src(
    src: dict,
    source_type: str,
    ref: str | None,
) -> tuple[str | None, str | None]:
    """Extract ``(in_repo_path, ref)`` for a dict-form plugin source."""
    if source_type == "github":
        path = src.get("path", "")
        path = path.strip("/") if isinstance(path, str) else ""
        if not path:
            return None, ref
        validate_path_segments(path, context="github source path")
        return path, ref

    if source_type in ("git-subdir", "gitlab"):
        sub = (src.get("subdir", "") or src.get("path", "")) or ""
        sub = sub.strip("/") if isinstance(sub, str) else ""
        if not sub:
            return None, ref
        validate_path_segments(sub, context="git-subdir source path")
        return sub, ref

    return None, None


def _extract_in_repo_path_and_ref(
    plugin: MarketplacePlugin, plugin_root: str = ""
) -> tuple[str | None, str | None]:
    """Return ``(in_repo_path, ref)`` for GitLab explicit git+path resolution.

    ``in_repo_path`` is ``None`` when the plugin is the repository root (no
    subdirectory package). ``ref`` is only set for dict sources that declare it.
    """
    src = plugin.source
    if src is None:
        return None, None

    if isinstance(src, str):
        return _from_string_src(src, plugin_root)

    if not isinstance(src, dict):
        return None, None

    source_type = _coerce_dict_plugin_type(src)
    ref_val = src.get("ref", "")
    ref: str | None = ref_val.strip() if isinstance(ref_val, str) and ref_val.strip() else None
    return _from_dict_src(src, source_type, ref)


def _gitlab_in_marketplace_dependency_reference(
    source: MarketplaceSource,
    in_repo_path: str,
    ref: str | None,
) -> DependencyReference:
    """Build ``DependencyReference`` equivalent to object-form ``git`` + ``path`` (spec)."""
    entry: dict = {"git": _marketplace_https_git_url(source), "path": in_repo_path}
    if ref:
        entry["ref"] = ref
    return DependencyReference.parse_from_dict(entry)


# ---------------------------------------------------------------------------
# Typed source-type resolvers
# ---------------------------------------------------------------------------


def _resolve_github_source(source: dict) -> str:
    """Resolve a ``github`` source type to ``owner/repo[/path][#ref]``.

    Accepts ``path`` field (Copilot CLI format) as a virtual subdirectory.
    """
    repo = source.get("repo", "") or source.get("repository", "")
    ref = source.get("ref", "")
    path = source.get("path", "").strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid github source: 'repo' (or 'repository') field must be 'owner/repo', got '{repo}'"
        )
    if path:
        try:
            validate_path_segments(path, context="github source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        base = f"{repo}/{path}"
    else:
        base = repo
    if ref:
        return f"{base}#{ref}"
    return base


def _resolve_url_source(source: dict) -> str:
    """Resolve a ``url`` source type.

    Delegates to ``DependencyReference.parse()`` to extract the
    ``owner/repo`` coordinate from any valid Git URL (GitHub, GHES, GitLab,
    Bitbucket, ADO, SSH).  The URL's host is *not* preserved -- downstream
    resolution (``RefResolver``) uses the configured ``GITHUB_HOST`` for
    ``git ls-remote``.  True cross-host resolution is tracked in #1010.
    """
    url = source.get("url", "")
    if not url:
        raise ValueError("URL source requires a non-empty 'url' field")
    try:
        dep = DependencyReference.parse(url)
    except ValueError as exc:
        raise ValueError(f"Cannot resolve URL source '{url}': {exc}") from exc
    if dep.is_local:
        raise ValueError(f"URL source '{url}' resolves to a local path, not a Git coordinate.")
    if dep.reference:
        return f"{dep.repo_url}#{dep.reference}"
    return dep.repo_url


def _resolve_git_subdir_source(source: dict) -> str:
    """Resolve a ``git-subdir`` source type to ``owner/repo[/subdir][#ref]``."""
    repo = source.get("repo", "") or source.get("url", "")
    # Reject full URLs -- the url fallback accepts owner/repo strings only
    if "://" in repo:
        raise ValueError(
            f"Invalid git-subdir source: expected 'owner/repo' but got a URL '{repo}'. "
            f"Use source type 'url' for full URL references."
        )
    ref = source.get("ref", "")
    subdir = (source.get("subdir", "") or source.get("path", "")).strip("/")
    if not repo or "/" not in repo:
        raise ValueError(
            f"Invalid git-subdir source: 'repo' (or 'url') must be 'owner/repo', got '{repo}'"
        )
    if subdir:
        try:
            validate_path_segments(subdir, context="git-subdir source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        base = f"{repo}/{subdir}"
    else:
        base = repo
    if ref:
        return f"{base}#{ref}"
    return base


def _resolve_relative_source(
    source: str,
    marketplace_owner: str,
    marketplace_repo: str,
    plugin_root: str = "",
) -> str:
    """Resolve a relative path source to ``owner/repo[/subdir]``.

    Relative sources point to subdirectories within the marketplace repo itself.
    When *plugin_root* is set (from ``metadata.pluginRoot`` in the manifest),
    bare names (no ``/``) are resolved under that directory.
    """
    # Normalize the relative path (strip leading ./ and trailing /)
    rel = source.strip("/")
    if rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")

    # If plugin_root is set and source is a bare name, prepend it
    if plugin_root and rel and rel != "." and "/" not in rel:
        root = plugin_root.strip("/")
        if root.startswith("./"):
            root = root[2:]
        root = root.strip("/")
        if root:
            rel = f"{root}/{rel}"

    if rel and rel != ".":
        try:
            validate_path_segments(rel, context="relative source path")
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc
        return f"{marketplace_owner}/{marketplace_repo}/{rel}"
    return f"{marketplace_owner}/{marketplace_repo}"


# ---------------------------------------------------------------------------
# Public surface: resolve_plugin_source
# ---------------------------------------------------------------------------


def resolve_plugin_source(
    plugin: MarketplacePlugin,
    marketplace_owner: str = "",
    marketplace_repo: str = "",
    plugin_root: str = "",
) -> str:
    """Resolve a plugin's source to a canonical ``owner/repo[#ref]`` string.

    Handles 4 source types: relative, github, url, git-subdir.
    NPM sources are rejected with a clear message.

    Args:
        plugin: The marketplace plugin to resolve.
        marketplace_owner: Owner of the marketplace repo (for relative sources).
        marketplace_repo: Repo name of the marketplace (for relative sources).
        plugin_root: Base path for bare-name sources (from metadata.pluginRoot).

    Returns:
        Canonical ``owner/repo[#ref]`` string.

    Raises:
        ValueError: If the source type is unsupported or the source is invalid.
    """
    source = plugin.source
    if source is None:
        raise ValueError(f"Plugin '{plugin.name}' has no source defined")

    # String source = relative path
    if isinstance(source, str):
        return _resolve_relative_source(
            source, marketplace_owner, marketplace_repo, plugin_root=plugin_root
        )

    if not isinstance(source, dict):
        raise ValueError(
            f"Plugin '{plugin.name}' has unrecognized source format: {type(source).__name__}"
        )

    source_type = _coerce_dict_plugin_type(source)
    if not source_type:
        raise ValueError(
            f"Plugin '{plugin.name}' has dict source with no 'type' and no inferrable 'repo' field"
        )

    if source_type == "github":
        return _resolve_github_source(source)
    elif source_type == "url":
        return _resolve_url_source(source)
    elif source_type == "git-subdir":
        return _resolve_git_subdir_source(source)
    elif source_type == "gitlab":
        # GitLab-native marketplace entries mirror git-subdir (repo + path/subdir).
        return _resolve_git_subdir_source(source)
    elif source_type == "npm":
        raise ValueError(
            f"Plugin '{plugin.name}' uses npm source type which is not supported by APM. "
            f"APM requires Git-based sources. "
            f"Consider asking the marketplace maintainer to add a 'github' source."
        )
    else:
        raise ValueError(f"Plugin '{plugin.name}' has unsupported source type: '{source_type}'")
