"""Resolve ``NAME@MARKETPLACE`` specifiers to canonical ``owner/repo#ref`` strings.

The ``@`` disambiguation rule:
- If input matches ``^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+$`` (no ``/``, no ``:``,
  no ``#`` before the ``@``), it is a marketplace ref.
- Everything else goes to the existing ``DependencyReference.parse()`` path.
- These inputs previously raised ``ValueError`` ("Use 'user/repo' format"),
  so this is a backward-compatible grammar extension.

For marketplaces on hosts where FQDN shorthand cannot split nested paths safely
(``gitlab.com``, self-managed GitLab **even when not** listed in ``GITLAB_HOST``,
and other non-GitHub / non-ADO FQDNs such as ``git.example.com``), in-marketplace
plugin sources under a subdirectory of the marketplace repository are resolved to a
:class:`~apm_cli.models.dependency.reference.DependencyReference` built like explicit
``git:`` + ``path:``; clone target
is only the registered marketplace project; the plugin directory is ``virtual_path``.
``github.com`` and ``*.ghe.com`` keep shorthand (no structured ref); ``*.ghe.com``
canonicals additionally carry a host prefix so downstream auth resolves at the
enterprise host instead of falling back to ``github.com`` (#1285).
:func:`resolve_marketplace_plugin` returns
:class:`MarketplacePluginResolution`, which iterates as ``(canonical, plugin)`` so
existing ``canonical, plugin = resolve_marketplace_plugin(...)`` call sites keep
working; consumers that need the structured ref use ``result.dependency_reference``.

Implementation note
-------------------
The heavy helpers live in three private sibling modules to keep each file <= 500 lines:

* ``_resolver_models``     -- :class:`CrossRepoMisconfigRisk` and
  :class:`MarketplacePluginResolution` data-classes.
* ``_resolver_host_utils`` -- host-matching, canonical-normalisation, and the
  cross-repo misconfiguration sentinel.
* ``_resolver_source``     -- typed source-to-canonical converters and
  :func:`resolve_plugin_source`.

All public names and the private symbols used by existing tests are re-exported
from *this* module so every ``from apm_cli.marketplace.resolver import X`` import
continues to work without changes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from ._resolver_host_utils import (
    _compute_cross_repo_misconfig_risk,
    _is_in_marketplace_source,
    _marketplace_host_needs_explicit_git_path,
    _needs_canonical_host_prefix,
)
from ._resolver_models import CrossRepoMisconfigRisk, MarketplacePluginResolution
from ._resolver_source import (
    _extract_in_repo_path_and_ref,
    _gitlab_in_marketplace_dependency_reference,
    _resolve_git_subdir_source,
    _resolve_github_source,
    _resolve_relative_source,
    _resolve_url_source,
    resolve_plugin_source,
)
from .client import fetch_or_cache
from .errors import PluginNotFoundError
from .registry import get_marketplace_by_name

logger = logging.getLogger(__name__)

_MARKETPLACE_RE = re.compile(r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._-]+)(?:#(.+))?$")

# Characters that signal a semver range rather than a raw git ref
_SEMVER_RANGE_CHARS = re.compile(r"[~^<>=!]")

# ---------------------------------------------------------------------------
# Re-export private symbols so ``from apm_cli.marketplace.resolver import _X``
# keeps working for existing tests and any caller that reached below the
# public surface.
# ---------------------------------------------------------------------------
__all__ = [
    "CrossRepoMisconfigRisk",
    "MarketplacePluginResolution",
    "_compute_cross_repo_misconfig_risk",
    "_extract_in_repo_path_and_ref",
    "_gitlab_in_marketplace_dependency_reference",
    "_resolve_git_subdir_source",
    "_resolve_github_source",
    "_resolve_relative_source",
    "_resolve_url_source",
    "parse_marketplace_ref",
    "resolve_marketplace_plugin",
    "resolve_plugin_source",
]


def parse_marketplace_ref(
    specifier: str,
) -> tuple[str, str, str | None] | None:
    """Parse a ``NAME@MARKETPLACE[#ref]`` specifier.

    The optional ``#ref`` suffix carries a raw git ref (tag, branch, or
    SHA). Semver range characters (``^``, ``~``, ``>=``, ``<``, ``!=``)
    are rejected with a ``ValueError`` because marketplace refs are raw
    git refs, not version constraints.

    Returns:
        ``(plugin_name, marketplace_name, ref_or_none)`` if the
        specifier matches, or ``None`` if it does not look like a
        marketplace ref.

    Raises:
        ValueError: If the ``#`` suffix contains semver range characters.
    """
    s = specifier.strip()
    # Quick rejection: slashes and colons *before* the fragment belong to
    # other formats.  Split on ``#`` first so that refs with slashes
    # (e.g. ``feature/branch``) do not cause a false rejection.
    head = s.split("#", 1)[0]
    if "/" in head or ":" in head:
        return None
    match = _MARKETPLACE_RE.match(s)
    if match:
        ref = match.group(3)
        if ref and _SEMVER_RANGE_CHARS.search(ref):
            raise ValueError(
                "Semver ranges are not supported in marketplace refs. "
                "Use a raw git tag, branch, or SHA instead "
                "(e.g. 'plugin@mkt#v2.0.0'). "
                "See: https://microsoft.github.io/apm/guides/marketplaces/"
            )
        return (match.group(1), match.group(2), ref)
    return None


def resolve_marketplace_plugin(
    plugin_name: str,
    marketplace_name: str,
    *,
    version_spec: str | None = None,
    auth_resolver: object | None = None,
    warning_handler: Callable[[str], None] | None = None,
) -> MarketplacePluginResolution:
    """Resolve a marketplace plugin reference to a canonical string and plugin row.

    For non-GitHub, non-ADO marketplace hosts and in-marketplace subdirectory plugins,
    also returns :attr:`MarketplacePluginResolution.dependency_reference` so callers
    clone the marketplace project only and use ``virtual_path`` for the plugin directory.

    When *version_spec* is given it is treated as a raw git ref override
    that replaces the plugin's ``source.ref``.  When ``None`` the ref
    from the marketplace entry is used as-is.

    Args:
        plugin_name: Plugin name within the marketplace.
        marketplace_name: Registered marketplace name.
        version_spec: Optional raw git ref override (e.g. ``"v2.0.0"``
            or ``"main"``).  ``None`` uses the marketplace entry's
            ``source.ref``.
        auth_resolver: Optional ``AuthResolver`` instance.
        warning_handler: Optional callback for security warnings.  When
            provided, warnings (immutability violations, shadow detections)
            are forwarded here instead of being emitted through Python
            stdlib logging.  Callers typically pass
            ``CommandLogger.warning`` so warnings render through the CLI
            output system.

    Returns:
        :class:`MarketplacePluginResolution` (iterates as ``(canonical, plugin)``).

    Raises:
        MarketplaceNotFoundError: If the marketplace is not registered.
        PluginNotFoundError: If the plugin is not in the marketplace.
        MarketplaceFetchError: If the marketplace cannot be fetched.
        ValueError: If the plugin source cannot be resolved.
    """

    def _emit_warning(msg: str) -> None:
        """Route warning through handler when available, else stdlib."""
        if warning_handler is not None:
            warning_handler(msg)
        else:
            logger.warning("%s", msg)

    source = get_marketplace_by_name(marketplace_name)
    manifest = fetch_or_cache(source, auth_resolver=auth_resolver)

    plugin = manifest.find_plugin(plugin_name)
    if plugin is None:
        raise PluginNotFoundError(plugin_name, marketplace_name)

    canonical = resolve_plugin_source(
        plugin,
        marketplace_owner=source.owner,
        marketplace_repo=source.repo,
        plugin_root=manifest.plugin_root,
    )

    dep_ref = None
    if _marketplace_host_needs_explicit_git_path(source.host) and _is_in_marketplace_source(
        plugin, source
    ):
        in_repo_path, path_ref = _extract_in_repo_path_and_ref(
            plugin, plugin_root=manifest.plugin_root
        )
        if in_repo_path:
            dep_ref = _gitlab_in_marketplace_dependency_reference(
                source, in_repo_path, version_spec or path_ref
            )
            canonical = dep_ref.to_canonical()

    # ---- Backfill host on canonical for GitHub-family enterprise hosts ----
    # ``*.ghe.com`` marketplaces keep virtual shorthand (no structured ``dep_ref``)
    # because there is no nested-group ambiguity to disambiguate, but the bare
    # canonical drops the host that ``DependencyReference.parse`` needs to route auth
    # at the enterprise host instead of falling back to ``github.com``. Backfill the
    # host so the canonical self-routes, scoped to in-marketplace sources where the
    # host is unambiguously the registered marketplace host (#1285).
    if (
        dep_ref is None
        and _is_in_marketplace_source(plugin, source)
        and _needs_canonical_host_prefix(canonical, source.host)
    ):
        canonical = f"{source.host}/{canonical}"
        logger.debug(
            "Backfilled marketplace host '%s' onto canonical for %s@%s (auth routing #1285)",
            source.host,
            plugin_name,
            marketplace_name,
        )

    # ---- Cross-repo misconfig sentinel (#1305) ----
    # PR #1292's host backfill only covers in-marketplace sources. A cross-repo
    # dict ``type: github`` source with a bare ``repo`` on an enterprise
    # marketplace cannot be safely backfilled here -- the bare syntax also
    # legitimately means "a github.com open-source dep from this enterprise
    # marketplace" -- so the canonical stays bare and downstream auth routes at
    # github.com. Attach a sentinel so the install command can emit an
    # actionable hint ONLY when the package subsequently fails validation; the
    # legitimate cross-host path validates fine and never sees the hint.
    cross_repo_misconfig_risk = _compute_cross_repo_misconfig_risk(
        plugin, source, canonical, dep_ref
    )

    # ---- Raw ref override ----
    # When version_spec is provided it is treated as a raw git ref that
    # overrides whatever ref came from the marketplace source field.
    if version_spec and dep_ref is None:
        base = canonical.split("#", 1)[0]
        canonical = f"{base}#{version_spec}"
        logger.debug(
            "Using raw git ref '%s' for %s@%s",
            version_spec,
            plugin_name,
            marketplace_name,
        )

    # ---- Ref immutability check (advisory) ----
    # Record the plugin -> ref mapping (scoped by version) and warn if
    # it changed since the last install (potential ref-swap attack).
    # Using the plugin's declared version field ensures legitimate
    # version bumps never trigger false-positive warnings.
    current_ref = canonical.split("#", 1)[1] if "#" in canonical else None
    plugin_version = plugin.version or ""
    if current_ref:
        from .version_pins import check_ref_pin, record_ref_pin

        previous_ref = check_ref_pin(
            marketplace_name,
            plugin_name,
            current_ref,
            version=plugin_version,
        )
        if previous_ref is not None:
            _emit_warning(
                f"Plugin {plugin_name}@{marketplace_name} ref changed: was '{previous_ref}', now '{current_ref}'. "
                "This may indicate a ref swap attack."
            )
        record_ref_pin(
            marketplace_name,
            plugin_name,
            current_ref,
            version=plugin_version,
        )

    logger.debug(
        "Resolved %s@%s -> %s",
        plugin_name,
        marketplace_name,
        canonical,
    )

    # -- Shadow detection (advisory) --
    # Warn when the same plugin name exists in other registered
    # marketplaces.  This helps users notice potential name-squatting
    # where an attacker publishes a same-named plugin in a secondary
    # marketplace.
    try:
        from .shadow_detector import detect_shadows

        shadows = detect_shadows(plugin_name, marketplace_name, auth_resolver=auth_resolver)
        for shadow in shadows:
            _emit_warning(
                f"Plugin '{plugin_name}' also found in marketplace '{shadow.marketplace_name}'. "
                "Verify you are installing from the intended source."
            )
    except Exception:
        # Shadow detection must never break installation
        logger.debug("Shadow detection failed", exc_info=True)

    return MarketplacePluginResolution(
        canonical=canonical,
        plugin=plugin,
        dependency_reference=dep_ref,
        cross_repo_misconfig_risk=cross_repo_misconfig_risk,
    )
