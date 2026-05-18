"""Data-classes returned by the marketplace resolver.

Kept in a tiny module so they can be imported without pulling in the full
resolver logic (which has heavier dependencies on client / registry).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..models.dependency.reference import DependencyReference
from .models import MarketplacePlugin


@dataclass(frozen=True)
class CrossRepoMisconfigRisk:
    """Signal that a cross-repo dict ``type: github`` source on an enterprise
    GitHub-family marketplace resolved to a bare canonical (#1305).

    Attached to :class:`MarketplacePluginResolution` when the marketplace is on
    ``*.ghe.com`` and the plugin's dict source declares a bare ``owner/repo``
    that does not match the marketplace project. The resolver deliberately
    leaves these canonicals bare (PR #1292 scoped its host backfill to
    in-marketplace sources), so ``DependencyReference.parse`` defaults the host
    to ``github.com``. Two intents share this syntax -- a legitimate cross-host
    ``github.com`` open-source dep, or a misconfigured same-host entry that
    should have been ``corp.ghe.com/owner/repo`` -- and the resolver cannot
    distinguish them. The install command consults this sentinel when the
    package fails validation so an actionable hint surfaces only at the
    failure boundary, never on the legitimate path.
    """

    marketplace_host: str
    bare_repo_field: str
    suggested_qualified_repo: str


@dataclass
class MarketplacePluginResolution:
    """Outcome of :func:`~apm_cli.marketplace.resolver.resolve_marketplace_plugin`.

    Iteration yields ``(canonical, plugin)`` so callers can write
    ``canonical, plugin = resolve_marketplace_plugin(...)`` unchanged.
    When :attr:`dependency_reference` is set (GitLab-class in-marketplace
    subdirectory plugins), install logic should prefer it over
    :meth:`~apm_cli.models.dependency.reference.DependencyReference.parse`
    on :attr:`canonical` to avoid mis-parsing nested paths as GitLab project segments.
    :attr:`cross_repo_misconfig_risk` is non-``None`` only for the #1305
    cross-repo bare-on-enterprise pattern; consumers emit it as a hint when the
    package subsequently fails validation.
    """

    canonical: str
    plugin: MarketplacePlugin
    dependency_reference: DependencyReference | None = None
    cross_repo_misconfig_risk: CrossRepoMisconfigRisk | None = None

    def __iter__(self) -> Iterator[str | MarketplacePlugin]:
        yield self.canonical
        yield self.plugin
