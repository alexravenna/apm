"""Base classes and shared helpers for install dependency sources.

Defines the ``DependencySource`` strategy ABC, the ``Materialization``
result dataclass, and the ``_format_package_type_label`` helper used by
the concrete source classes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.apm_package import PackageInfo


def _format_package_type_label(pkg_type) -> str | None:
    """Human-readable label for a detected ``PackageType``.

    Centralised so every install path emits the same wording and so
    new ``PackageType`` values can be added without grepping for ad-hoc
    dicts.  Missing ``HOOK_PACKAGE`` from this table is what made
    microsoft/apm#780 silent -- keep all classifiable enum members
    covered.
    """
    from apm_cli.models.apm_package import PackageType

    return {
        PackageType.CLAUDE_SKILL: "Skill (SKILL.md detected)",
        PackageType.MARKETPLACE_PLUGIN: "Marketplace Plugin (plugin.json or agents/skills/commands)",
        PackageType.HYBRID: "Hybrid (apm.yml + SKILL.md)",
        PackageType.APM_PACKAGE: "APM Package (apm.yml)",
        PackageType.HOOK_PACKAGE: "Hook Package (hooks/*.json only)",
        PackageType.SKILL_BUNDLE: "Skill Bundle (skills/<name>/SKILL.md)",
    }.get(pkg_type)


@dataclass
class Materialization:
    """Outcome of ``DependencySource.acquire()``.

    Carries everything the integration template needs to run the security
    gate + primitive integration on a freshly-acquired package.
    """

    package_info: PackageInfo | None
    install_path: Path
    dep_key: str
    deltas: dict[str, int] = field(default_factory=lambda: {"installed": 1})


class DependencySource(ABC):
    """Strategy: acquire one dependency and prepare it for integration.

    Subclasses encapsulate source-specific concerns (filesystem copy,
    cache reuse, fresh download with progress + hash verification).
    The post-acquire template flow is the same for every source.
    """

    INTEGRATE_ERROR_PREFIX: str = "Failed to integrate primitives"
    """Per-source error wording used by the integration template when
    ``integrate_package_primitives`` raises.  Subclasses override to
    preserve the legacy diagnostic text shown to users."""

    def __init__(
        self,
        ctx: InstallContext,
        dep_ref: Any,
        install_path: Path,
        dep_key: str,
    ):
        self.ctx = ctx
        self.dep_ref = dep_ref
        self.install_path = install_path
        self.dep_key = dep_key

    @abstractmethod
    def acquire(self) -> Materialization | None:
        """Materialise the dependency on disk and build PackageInfo.

        Returns ``None`` to skip integration entirely (e.g. local dep at
        user scope, copy/download failure).  Otherwise returns a
        ``Materialization`` consumed by the integration template.
        """
