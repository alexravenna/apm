"""Package-type detection for APM packages.

Provides :func:`gather_detection_evidence` and :func:`detect_package_type`,
the single source of truth for the package-classification cascade.

Public names are re-exported via ``apm_cli.models.validation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ._types import PackageType

# Canonical order of the directories that mark a Claude Code marketplace
# plugin.  Tests assert this ordering on ``DetectionEvidence.plugin_dirs_present``
# so adding a new directory here is a public-API change.
_PLUGIN_DIRS: tuple[str, ...] = ("agents", "skills", "commands")


def _has_hook_json(package_path: Path) -> bool:
    """Check if the package has hook JSON files in hooks/ or .apm/hooks/."""
    for hooks_dir in [package_path / "hooks", package_path / APM_DIR / "hooks"]:
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            return True
    return False


@dataclass(frozen=True)
class DetectionEvidence:
    """Snapshot of the file-system signals that drove classification.

    Returned from :func:`gather_detection_evidence` and consumed by
    install-time observability (verbose detection traces, near-miss
    warnings, deploy-summary labelling).  Kept independent of
    :func:`detect_package_type` so that the classification function can
    keep its existing ``(PackageType, Optional[Path])`` return signature
    while observability code can pull richer detail on demand.
    """

    has_apm_yml: bool
    has_skill_md: bool
    has_hook_json: bool
    plugin_json_path: Path | None
    plugin_dirs_present: tuple[str, ...]
    has_claude_plugin_dir: bool = False
    nested_skill_dirs: tuple[str, ...] = ()
    has_plugin_manifest: bool = False

    @property
    def has_plugin_evidence(self) -> bool:
        """True if a real plugin manifest is present.

        Only ``plugin.json`` or ``.claude-plugin/`` directory count as
        plugin evidence.  Bare ``skills/``, ``agents/``, ``commands/``
        directories do NOT -- those are handled by the SKILL_BUNDLE
        classification path instead.
        """
        return self.has_plugin_manifest


def gather_detection_evidence(package_path: Path) -> DetectionEvidence:
    """Collect all package-type signals from a directory in one pass.

    Pure: no side-effects, no file mutations.  Cheap (a handful of stat
    calls).  See :class:`DetectionEvidence` for the shape of the return
    value.
    """
    from ...utils.helpers import find_plugin_json

    plugin_dirs_present = tuple(name for name in _PLUGIN_DIRS if (package_path / name).is_dir())
    plugin_json_path = find_plugin_json(package_path)
    has_claude_plugin_dir = (package_path / ".claude-plugin").is_dir()

    # Plugin manifest = plugin.json OR .claude-plugin/ directory.
    has_plugin_manifest = plugin_json_path is not None or has_claude_plugin_dir

    # Nested skill dirs: directories under skills/ that contain a SKILL.md.
    nested_skill_dirs: tuple[str, ...] = ()
    skills_dir = package_path / "skills"
    if skills_dir.is_dir():
        nested_skill_dirs = tuple(
            d.name
            for d in sorted(skills_dir.iterdir())
            if d.is_dir() and (d / SKILL_MD_FILENAME).exists()
        )

    return DetectionEvidence(
        has_apm_yml=(package_path / APM_YML_FILENAME).exists(),
        has_skill_md=(package_path / SKILL_MD_FILENAME).exists(),
        has_hook_json=_has_hook_json(package_path),
        plugin_json_path=plugin_json_path,
        plugin_dirs_present=plugin_dirs_present,
        has_claude_plugin_dir=has_claude_plugin_dir,
        nested_skill_dirs=nested_skill_dirs,
        has_plugin_manifest=has_plugin_manifest,
    )


def _classify_apm_yml_package(
    evidence: DetectionEvidence, package_path: Path
) -> tuple[PackageType, Path | None]:
    """Return APM_PACKAGE or INVALID for packages where ``apm.yml`` is present."""
    apm_dir = package_path / APM_DIR
    if apm_dir.exists() or _apm_yml_declares_dependencies(package_path / APM_YML_FILENAME):
        return PackageType.APM_PACKAGE, None
    return PackageType.INVALID, None


def detect_package_type(
    package_path: Path,
) -> tuple[PackageType, Path | None]:
    """Classify a package directory into a ``PackageType``.

    Single source of truth for the detection cascade.  Pure: no
    side-effects, no file mutations.

    Cascade order (first match wins):

    1. ``MARKETPLACE_PLUGIN`` -- plugin manifest present: ``plugin.json``
       OR ``.claude-plugin/`` directory.  This is the strictest signal
       (explicit plugin packaging intent).
    2. ``HYBRID`` -- root ``SKILL.md`` AND ``apm.yml`` present.
    3. ``CLAUDE_SKILL`` -- root ``SKILL.md`` only (no ``apm.yml``).
    4. ``SKILL_BUNDLE`` -- nested ``skills/<x>/SKILL.md`` detected;
       ``apm.yml`` optional; no ``.apm/`` required.
    5. ``APM_PACKAGE`` -- ``apm.yml`` present. ``.apm/`` is optional: a
       dep-only ``apm.yml`` (no ``.apm/`` and no nested skills) is a valid
       curated aggregator that contributes no own primitives (#1094).
    6. ``HOOK_PACKAGE`` -- ``hooks/*.json`` only, no other signals.
    7. ``INVALID`` -- nothing recognisable.

    Returns:
        A ``(package_type, plugin_json_path)`` tuple.  *plugin_json_path*
        is non-None only when ``MARKETPLACE_PLUGIN`` was matched via an
        actual ``plugin.json`` file (not via directory evidence alone).
    """
    evidence = gather_detection_evidence(package_path)

    # 1. Plugin manifest present -> MARKETPLACE_PLUGIN
    if evidence.has_plugin_manifest:
        return PackageType.MARKETPLACE_PLUGIN, evidence.plugin_json_path

    # 2. Root SKILL.md + apm.yml -> HYBRID
    if evidence.has_apm_yml and evidence.has_skill_md:
        return PackageType.HYBRID, None

    # 3. Root SKILL.md only -> CLAUDE_SKILL
    if evidence.has_skill_md:
        return PackageType.CLAUDE_SKILL, None

    # 4. Nested skills/<x>/SKILL.md -> SKILL_BUNDLE (apm.yml optional)
    if evidence.nested_skill_dirs:
        return PackageType.SKILL_BUNDLE, None

    # 5. apm.yml present -> APM classification.
    #    With .apm/ OR declared dependencies, a valid APM_PACKAGE.
    #    Without either, INVALID (the user committed to "this is an APM
    #    package" by adding apm.yml; we trust that signal and surface the
    #    standard "missing .apm/" diagnostic instead of silently falling
    #    through to a hooks/skill-bundle classification). Dep-only is
    #    valid as a curated aggregator (#1094).
    if evidence.has_apm_yml:
        return _classify_apm_yml_package(evidence, package_path)

    # 6. hooks/*.json -> HOOK_PACKAGE; 7. Nothing recognisable -> INVALID
    return (
        (PackageType.HOOK_PACKAGE, None) if evidence.has_hook_json else (PackageType.INVALID, None)
    )


def _apm_yml_declares_dependencies(apm_yml_path: Path) -> bool:
    """Return True iff ``apm.yml`` declares at least one dependency.

    Used by ``_validate_apm_package_with_yml`` to accept a dep-only
    ``apm.yml`` (no ``.apm/`` directory) as a valid curated aggregator
    (#1094). Any non-empty ``apm`` or ``mcp`` list under ``dependencies``
    OR ``devDependencies`` qualifies. Tolerant of malformed YAML /
    missing keys: returns False on any parse problem so callers fall
    back to the legacy "missing .apm/" diagnostic instead of silently
    accepting a malformed manifest.
    """
    try:
        from ...utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    def _has_listed_deps(block: object) -> bool:
        if not isinstance(block, dict):
            return False
        # Schema requires `apm` and `mcp` to be lists of strings or dicts
        # (see APMPackage._parse_dependency_dict). Non-list values, or
        # lists with no parseable entries, are malformed; treat them as
        # "no declared dependencies" so the caller falls through to the
        # legacy "missing .apm/" diagnostic instead of silently accepting
        # a malformed manifest.
        for key in ("apm", "mcp"):
            value = block.get(key)
            if isinstance(value, list) and any(isinstance(entry, (str, dict)) for entry in value):
                return True
        return False

    return _has_listed_deps(data.get("dependencies")) or _has_listed_deps(
        data.get("devDependencies")
    )
