"""MarketplaceBuilder -- load, resolve, compose, and write marketplace.json.

This module implements the full build pipeline:

1. **Load** -- parse ``marketplace.yml`` via ``yml_schema.load_marketplace_yml``.
2. **Resolve** -- for every package entry, call ``git ls-remote`` (via
   ``RefResolver``) and determine the concrete tag + SHA.
3. **Compose** -- produce an Anthropic-compliant ``marketplace.json`` dict
   with all APM-only fields stripped.
4. **Write** -- atomically write the JSON to disk (or skip on dry-run)
   and produce a ``BuildReport`` with diff statistics.

Hard rule: the output ``marketplace.json`` conforms byte-for-byte to
Anthropic's schema.  No APM-specific keys, no extensions, no renamed
fields.  ``packages`` in yml becomes ``plugins`` in json.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.auth import HostInfo

from ...utils.github_host import default_host
from ...utils.path_security import ensure_path_within
from ..diagnostics import BuildDiagnostic
from ..errors import (
    BuildError,
)
from ..output_mappers import (
    MARKETPLACE_OUTPUT_MAPPERS,
    MapperResult,
)
from ..output_mappers import (
    _is_display_version as _mapper_is_display_version,
)
from ..output_mappers import (
    _subtract_plugin_root as _mapper_subtract_plugin_root,
)
from ..output_profiles import (
    MarketplaceOutputProfile,
)
from ..ref_resolver import RefResolver
from ..yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml

logger = logging.getLogger(__name__)

__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolveResult",
    "ResolvedPackage",
]

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPackage:
    """A package entry after ref resolution."""

    name: str
    source_repo: str  # "owner/repo" only
    subdir: str | None  # APM-only (used to compose the output ``source`` object)
    ref: str  # resolved tag name, e.g. "v1.2.0"
    sha: str  # 40-char git SHA
    requested_version: str | None  # original APM-only range (for diagnostics)
    tags: tuple[str, ...]
    is_prerelease: bool  # True if the resolved ref was a prerelease semver


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving package refs in a marketplace build."""

    entries: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs

    @property
    def ok(self) -> bool:
        """True when every package resolved without error."""
        return len(self.errors) == 0


@dataclass(frozen=True)
class MarketplaceOutputReport:
    """Summary for one generated marketplace output profile."""

    profile: str
    resolved: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs
    warnings: tuple[str, ...]  # non-fatal diagnostic messages
    diagnostics: tuple[BuildDiagnostic, ...] = ()  # structured diagnostics
    unchanged_count: int = 0
    added_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    output_path: Path = field(default_factory=lambda: Path("."))
    dry_run: bool = False


@dataclass(frozen=True)
class BuildReport:
    """Summary of a marketplace build run across one or more output profiles."""

    outputs: tuple[MarketplaceOutputReport, ...]

    @property
    def primary_output(self) -> MarketplaceOutputReport:
        """Return the first output report for legacy single-output callers."""
        if not self.outputs:
            return MarketplaceOutputReport(
                profile="",
                resolved=(),
                errors=(),
                warnings=(),
            )
        return self.outputs[0]

    @property
    def resolved(self) -> tuple[ResolvedPackage, ...]:
        return self.primary_output.resolved

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        return self.primary_output.errors

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warn for output in self.outputs for warn in output.warnings)

    @property
    def diagnostics(self) -> tuple[BuildDiagnostic, ...]:
        return tuple(diag for output in self.outputs for diag in output.diagnostics)

    @property
    def unchanged_count(self) -> int:
        return self.primary_output.unchanged_count

    @property
    def added_count(self) -> int:
        return self.primary_output.added_count

    @property
    def updated_count(self) -> int:
        return self.primary_output.updated_count

    @property
    def removed_count(self) -> int:
        return self.primary_output.removed_count

    @property
    def output_path(self) -> Path:
        return self.primary_output.output_path

    @property
    def dry_run(self) -> bool:
        return any(output.dry_run for output in self.outputs)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize build report as the §4 JSON contract.

        Shape: {ok, dry_run, warnings[], errors[],
                marketplace: {outputs: [{format, path, added, updated,
                unchanged, skipped}]}, bundle: null}
        """
        all_warnings = list(self.warnings)
        all_errors: list[dict[str, str]] = []
        output_entries: list[dict[str, Any]] = []

        for out in self.outputs:
            output_entries.append(
                {
                    "format": out.profile,
                    "path": str(out.output_path),
                    "added": out.added_count,
                    "updated": out.updated_count,
                    "unchanged": out.unchanged_count,
                    "skipped": out.removed_count,
                }
            )
            for pkg_name, err_msg in out.errors:
                all_errors.append({"code": "build_error", "message": f"{pkg_name}: {err_msg}"})

        ok = len(all_errors) == 0
        return {
            "ok": ok,
            "dry_run": self.dry_run,
            "warnings": all_warnings,
            "errors": all_errors,
            "marketplace": {
                "outputs": output_entries,
            },
            "bundle": None,
        }

    @classmethod
    def failure_to_json_dict(
        cls,
        *,
        errors: list[dict[str, str]],
        warnings: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Produce the §4 JSON shape for a pre-build failure.

        Used when the build cannot even start (e.g., config parse error,
        unknown format filter).
        """
        return {
            "ok": False,
            "dry_run": dry_run,
            "warnings": warnings or [],
            "errors": errors,
            "marketplace": {
                "outputs": [],
            },
            "bundle": None,
        }


@dataclass
class BuildOptions:
    """Configuration knobs for MarketplaceBuilder."""

    concurrency: int = 8
    timeout_seconds: float = 10.0
    include_prerelease: bool = False
    allow_head: bool = False
    continue_on_error: bool = False
    offline: bool = False
    marketplace_output: Path | None = None
    # Backwards-compatible spelling for callers that predate ``apm pack``.
    output_override: Path | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

# 40-char hex SHA pattern
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _is_display_version(version: str | None) -> bool:
    """Return True if *version* looks like a fixed display version, not a range."""
    return _mapper_is_display_version(version)


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    """Remove pluginRoot prefix from a local source path for emit."""
    return _mapper_subtract_plugin_root(source, plugin_root)


class MarketplaceBuilder:
    """Load marketplace.yml, resolve refs, compose and write marketplace.json.

    Parameters
    ----------
    marketplace_yml_path:
        Path to the ``marketplace.yml`` file.
    options:
        Build options.  Defaults to ``BuildOptions()`` if not provided.
    auth_resolver:
        Optional ``AuthResolver`` for authenticating requests to private
        GitHub repositories.  When ``None`` (default) a fresh resolver is
        created lazily the first time a token is needed.
    """

    def __init__(
        self,
        marketplace_yml_path: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> None:
        self._yml_path = marketplace_yml_path
        self._project_root = marketplace_yml_path.parent
        self._options = options or BuildOptions()
        self._yml: MarketplaceYml | None = None
        self._resolver: RefResolver | None = None
        self._auth_resolver = auth_resolver
        # Resolved once per build, used by worker threads (read-only).
        self._github_token: str | None = None
        self._host: str = default_host() or "github.com"
        self._host_info: HostInfo | None = None
        self._auth_resolved: bool = False

    @classmethod
    def from_config(
        cls,
        config: MarketplaceYml,
        project_root: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> MarketplaceBuilder:
        """Construct a builder from an already-loaded MarketplaceConfig.

        Use this when the caller has already chosen between apm.yml and
        the legacy ``marketplace.yml`` (typically via
        ``migration.load_marketplace_config``).  ``project_root`` is the
        directory output paths are resolved against.
        """
        # Use a synthetic path so legacy code paths that consult
        # ``self._yml_path.parent`` still resolve to the project root.
        synthetic_path = project_root / (
            config.source_path.name if config.source_path is not None else "apm.yml"
        )
        instance = cls(synthetic_path, options=options, auth_resolver=auth_resolver)
        instance._project_root = project_root
        instance._yml = config
        return instance

    # -- lazy loaders -------------------------------------------------------

    def _load_yml(self) -> MarketplaceYml:
        if self._yml is None:
            # Shape-aware load: when the configured path is an apm.yml
            # file, use the apm.yml loader; otherwise default to the
            # legacy marketplace.yml loader.  Callers that have already
            # loaded a config should use ``from_config`` to bypass this.
            from ..yml_schema import load_marketplace_from_apm_yml

            if self._yml_path.name == "apm.yml":
                self._yml = load_marketplace_from_apm_yml(self._yml_path)
            else:
                self._yml = load_marketplace_yml(self._yml_path)
        return self._yml

    def _get_resolver(self) -> RefResolver:
        if self._resolver is None:
            self._ensure_auth()
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=self._host,
                token=self._github_token,
            )
        return self._resolver

    def _ensure_auth(self) -> None:
        """Lazily resolve host classification and GitHub token.

        Short-circuits when already resolved (even if no token was found)
        or when running in offline mode.  Offline mode is still marked as
        resolved so repeated calls remain idempotent.  Called by
        ``_get_resolver()`` so both ``resolve()`` and ``build()`` benefit
        from authenticated ``git ls-remote`` when available.
        """
        if self._auth_resolved:
            return
        if self._options.offline:
            self._auth_resolved = True
            return
        self._github_token = self._resolve_github_token()
        self._auth_resolved = True

    # -- output path --------------------------------------------------------

    def _output_path(self) -> Path:
        if self._options.marketplace_output is not None:
            return self._options.marketplace_output
        if self._options.output_override is not None:
            return self._options.output_override
        yml = self._load_yml()
        output_path = self._project_root / yml.claude.output
        # Containment guard -- reject output paths that escape the project root.
        ensure_path_within(output_path, self._project_root)
        return output_path

    def _mapper_for_profile(self, profile: MarketplaceOutputProfile):
        mapper = MARKETPLACE_OUTPUT_MAPPERS.get(profile.mapper)
        if mapper is None:
            raise BuildError(f"Unknown marketplace output mapper: {profile.mapper}")
        return mapper

    def remote_metadata_for_profile(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
    ) -> dict[str, dict[str, Any]] | None:
        """Return remote metadata needed to compose this output, if any."""
        mapper = self._mapper_for_profile(profile)
        if not mapper.uses_remote_metadata:
            return None
        return self._prefetch_metadata(resolved)

    def _map_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        """Map resolved packages into one marketplace output format."""
        mapper = self._mapper_for_profile(profile)
        return mapper.compose(
            config=self._load_yml(),
            resolved=resolved,
            remote_metadata=remote_metadata,
        )

    # -- single-entry resolution --------------------------------------------

    def _resolve_entry(self, entry: PackageEntry) -> ResolvedPackage:
        return _resolve_helpers._resolve_entry(self, entry)

    def _resolve_explicit_ref(
        self, entry: PackageEntry, resolver: RefResolver, owner_repo: str
    ) -> ResolvedPackage:
        return _resolve_helpers._resolve_explicit_ref(self, entry, resolver, owner_repo)

    def _resolve_version_range(
        self, entry: PackageEntry, resolver: RefResolver, owner_repo: str, yml: MarketplaceYml
    ) -> ResolvedPackage:
        return _resolve_helpers._resolve_version_range(self, entry, resolver, owner_repo, yml)

    # -- concurrent resolution ----------------------------------------------

    def resolve(self) -> ResolveResult:
        return _resolve_helpers.resolve(self)

    # -- remote description fetcher -----------------------------------------

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        return _metadata._fetch_remote_metadata(self, pkg)

    def _resolve_github_token(self) -> str | None:
        return _metadata._resolve_github_token(self)

    def _prefetch_metadata(self, resolved: list[ResolvedPackage]) -> dict[str, dict[str, str]]:
        return _metadata._prefetch_metadata(self, resolved)

    # -- composition --------------------------------------------------------

    def compose_marketplace_json(self, resolved: list[ResolvedPackage]) -> dict[str, Any]:
        return _compose.compose_marketplace_json(self, resolved)

    def compose_codex_marketplace_json(
        self, resolved: list[ResolvedPackage]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        return _compose.compose_codex_marketplace_json(self, resolved)

    def write_codex_marketplace_json(
        self, resolved: tuple[ResolvedPackage, ...]
    ) -> tuple[Path, tuple[str, ...]]:
        return _compose.write_codex_marketplace_json(self, resolved)

    def compose_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], tuple[str, ...], tuple[BuildDiagnostic, ...]]:
        return _compose.compose_output(self, profile, resolved, remote_metadata)

    def write_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        output_path: Path,
        *,
        include_diff: bool = False,
        remote_metadata: dict[str, dict[str, Any]] | None = None,
        errors: tuple[tuple[str, str], ...] = (),
    ) -> BuildReport:
        return _compose.write_output(
            self,
            profile,
            resolved,
            output_path,
            include_diff=include_diff,
            remote_metadata=remote_metadata,
            errors=errors,
        )

    # -- diff ---------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _compute_diff(
        old_json: dict[str, Any] | None, new_json: dict[str, Any]
    ) -> tuple[int, int, int, int]:
        return _compose._compute_diff(old_json, new_json)

    # -- atomic write -------------------------------------------------------

    @staticmethod
    @staticmethod
    def _serialize_json(data: dict[str, Any]) -> str:
        return _compose._serialize_json(data)

    @staticmethod
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        return _compose._atomic_write(path, content)

    def _load_existing_json(self, path: Path) -> dict[str, Any] | None:
        return _compose._load_existing_json(self, path)

    # -- full pipeline ------------------------------------------------------

    def build(self) -> BuildReport:
        return _compose.build(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ref_prefix(refname: str) -> str:
    """Strip ``refs/tags/`` or ``refs/heads/`` prefix."""
    if refname.startswith("refs/tags/"):
        return refname[len("refs/tags/") :]
    if refname.startswith("refs/heads/"):
        return refname[len("refs/heads/") :]
    return refname


from . import compose as _compose
from . import metadata as _metadata
from . import resolve_helpers as _resolve_helpers
