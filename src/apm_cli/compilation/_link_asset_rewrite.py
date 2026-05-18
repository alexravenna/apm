"""In-package asset link rewriting helpers (feature #1147).

Extracted from :class:`~apm_cli.compilation.link_resolver.UnifiedLinkResolver`
to keep the parent module under the 500-line budget. These functions are
internal helpers and may change without notice; import them via
``UnifiedLinkResolver`` or from ``_link_asset_rewrite`` only within the
``apm_cli.compilation`` package.

Design notes
------------
* All three functions are pure or near-pure (no class state needed), which
  is why they are module-level rather than class methods.
* ``resolve_in_package_asset_link`` receives the full
  :class:`~apm_cli.compilation.link_resolver.LinkResolutionContext` to avoid
  a long argument list; the import is guarded by ``TYPE_CHECKING`` to prevent
  a circular dependency (``link_resolver`` imports this module).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

if TYPE_CHECKING:  # pragma: no cover
    from apm_cli.compilation.link_resolver import LinkResolutionContext


def is_rewritable_relative_link(link_path: str) -> bool:
    """Decide whether a link is a candidate for in-package asset rewrite.

    Filters out everything that obviously is not a relative filesystem
    path inside the package: empty links, fragment-only links, links
    with any URL scheme, root-absolute paths, and protocol-relative
    URLs. The remaining links are *relative paths* that may resolve
    to a sibling file inside the source package.

    Args:
        link_path: Raw link target as it appears in the markdown.

    Returns:
        True if the link should be considered for asset rewriting.
    """
    stripped = (link_path or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("#", "//", "/")):
        # Fragment-only, protocol-relative, or root-absolute paths.
        return False
    # Any URL scheme (http:, mailto:, file:, javascript:, ...): skip.
    try:
        parsed = urlparse(stripped)
    except Exception:
        return False
    return not parsed.scheme


def split_link_target(link_path: str) -> tuple[str, str]:
    """Split a markdown link target into ``(path, suffix)``.

    Preserves a trailing ``#fragment`` or ``?query`` so the resolver
    can rewrite only the path component and re-append the suffix
    verbatim. Markdown link titles (``"title"`` after a space) are
    intentionally NOT stripped here -- the existing ``LINK_PATTERN``
    treats the whole inside of the parentheses as a single group, so
    a title would be embedded in ``link_path``. Such links are passed
    through unchanged by ``is_rewritable_relative_link`` indirectly
    (they typically contain a space and resolve to nothing).

    Returns:
        ``(path_part, suffix)`` where ``suffix`` includes its leading
        delimiter (``#`` or ``?``) or is the empty string. When both
        delimiters are present (e.g. ``doc.md?x=1#sec``), the split
        occurs at whichever appears first so the full remainder is
        preserved verbatim.
    """
    candidates = [link_path.find(sep) for sep in ("#", "?")]
    positions = [idx for idx in candidates if idx != -1]
    if not positions:
        return link_path, ""
    idx = min(positions)
    return link_path[:idx], link_path[idx:]


def _resolve_candidate(source_dir: Path, path_part: str, package_root: Path) -> Path | None:
    """Resolve a path part to a concrete file within package_root.

    Returns the resolved ``Path`` on success, or ``None`` if the candidate
    does not exist, is not a file, or escapes ``package_root``.
    """
    try:
        candidate = (source_dir / path_part).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    try:
        ensure_path_within(candidate, package_root)
    except PathTraversalError:
        return None
    return candidate


def resolve_in_package_asset_link(link_path: str, ctx: LinkResolutionContext) -> str | None:
    """Rewrite an in-package relative link to its post-install location.

    Resolves ``link_path`` relative to ``ctx.source_file.parent``,
    validates the resolved path lies inside ``ctx.package_root`` via
    :func:`ensure_path_within` (which also normalises symlinks and
    Windows extended prefixes), and returns the relative path from
    ``ctx.target_location`` to the resolved file. Preserves any
    ``#fragment`` or ``?query`` suffix.

    Returns ``None`` if any of the following hold; the caller
    preserves the original link unchanged:

    * ``ctx.package_root`` is not a directory (defensive).
    * The candidate file does not exist or is not a regular file.
    * The candidate escapes ``ctx.package_root`` (symlink traversal,
      ``..`` chains, etc.).
    * Path computation raises (broken filesystem, encoding, ...).
    """
    if ctx.package_root is None or not ctx.package_root.is_dir():
        return None

    path_part, suffix = split_link_target(link_path)
    if not path_part:
        return None

    try:
        source_dir = ctx.source_file.parent if ctx.source_file.is_file() else ctx.source_location
    except OSError:
        return None

    candidate = _resolve_candidate(source_dir, path_part, ctx.package_root)
    if candidate is None:
        return None

    # Replay-frame translation (#1182): during audit-replay of a
    # self-package, ``ctx.base_dir`` is the scratch tmpdir but
    # ``ctx.package_root`` (and therefore ``candidate``) still points
    # at the real project tree. Computing ``relpath`` directly would
    # produce a tmpdir-traversal link (e.g. ``../../../../Users/...``)
    # that diverges from what real install writes to disk, causing
    # spurious drift. Detect the cross-frame case (candidate outside
    # base_dir) and re-anchor the target onto package_root so the
    # rewrite mirrors the install-time output.
    relpath_anchor = ctx.target_location
    try:
        candidate_in_base = candidate.is_relative_to(ctx.base_dir)
    except (OSError, ValueError):
        candidate_in_base = True
    if not candidate_in_base:
        try:
            target_rel = ctx.target_location.relative_to(ctx.base_dir)
            relpath_anchor = ctx.package_root / target_rel
        except (OSError, ValueError):
            relpath_anchor = ctx.target_location

    try:
        relative_path = os.path.relpath(candidate, relpath_anchor)
    except (OSError, ValueError):
        return None

    rewritten = relative_path.replace(os.sep, "/")
    return f"{rewritten}{suffix}"
