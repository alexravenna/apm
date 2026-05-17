"""Dataclasses, loader, and validation for marketplace authoring config.

The marketplace publisher configuration may live in two places:

* (Preferred, current) inside ``apm.yml`` under a top-level
  ``marketplace:`` block.  Loaded via
  :func:`load_marketplace_from_apm_yml`.
* (Legacy, deprecated) inside a standalone ``marketplace.yml`` file.
  Loaded via :func:`load_marketplace_from_legacy_yml`.

Both paths produce the same immutable :class:`MarketplaceConfig`
dataclass that the builder consumes.

Key design rules
----------------
* **Anthropic pass-through preservation.**  The ``metadata`` block is
  stored as a plain ``dict`` with original key casing (e.g.
  ``pluginRoot`` stays ``pluginRoot``).  Unknown keys inside ``metadata``
  are preserved -- only the builder decides what is forwarded.
* **APM-only vs Anthropic separation.**  Build-time fields (``build``,
  ``version``, ``ref``, ``subdir``, ``tag_pattern``,
  ``include_prerelease``) live as explicit dataclass attributes so the
  builder can strip them cleanly.
* **Strict key sets.**  Unknown keys inside the marketplace block raise
  ``MarketplaceYmlError`` so typos are never silently ignored.  The
  apm.yml top-level is intentionally NOT strict here -- only the
  ``marketplace:`` subtree is validated by this module.
* **Local-path packages.**  ``source`` accepts ``./...`` paths in
  addition to ``owner/repo`` shape.  Local packages skip ref resolution.
"""

from __future__ import annotations

import re
from typing import Any

from ...utils.path_security import PathTraversalError, validate_path_segments
from ..errors import MarketplaceYmlError
from ..output_profiles import MARKETPLACE_OUTPUTS, known_output_names
from .class_ import (
    MarketplaceBuild,
    MarketplaceClaudeConfig,
    MarketplaceCodexConfig,
    MarketplaceConfig,
    MarketplaceOutputSpec,
    MarketplaceOwner,
    PackageEntry,
)

_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SOURCE_RE = re.compile(r"^(?:[^/]+/[^/]+|\./.*)$")
LOCAL_SOURCE_RE = re.compile(r"^\./")
_TAG_PLACEHOLDERS = ("{version}", "{name}")
_BUILD_KEYS = frozenset(
    {
        "tagPattern",
    }
)
_PACKAGE_ENTRY_KEYS = frozenset(
    {
        "name",
        "source",
        "subdir",
        "version",
        "ref",
        "tag_pattern",
        "include_prerelease",
        "description",
        "homepage",
        "tags",
        "author",
        "license",
        "repository",
        "keywords",
        "category",
    }
)
_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})
_APM_MARKETPLACE_KEYS = frozenset(
    {
        "name",  # optional override of top-level apm.yml name
        "description",  # optional override of top-level apm.yml description
        "version",  # optional override of top-level apm.yml version
        "owner",
        "output",
        "outputs",
        "claude",
        "metadata",
        "build",
        "codex",
        "packages",
    }
)
_CLAUDE_KEYS = frozenset(
    {
        "output",
    }
)
_CODEX_KEYS = frozenset(
    {
        "output",
    }
)
MarketplaceYml = MarketplaceConfig


def _parse_author(raw: Any, index: int) -> dict[str, str] | None:
    """Normalize a curator-supplied ``author`` value to a Claude-Code-
    compliant object ``{name, email?, url?}``.

    Accepts either a non-empty string (treated as ``name``) or a mapping
    with at least ``name`` and only the permitted keys. Returns ``None``
    when ``raw`` is ``None``. Raises :class:`MarketplaceYmlError` on any
    other shape.
    """
    if raw is None:
        return None
    ctx = f"packages[{index}].author"
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            raise MarketplaceYmlError(f"'{ctx}' must be a non-empty string or object with 'name'")
        return {"name": name}
    if isinstance(raw, dict):
        unknown = set(raw.keys()) - _AUTHOR_OBJECT_KEYS
        if unknown:
            raise MarketplaceYmlError(
                f"'{ctx}' has unknown key(s): "
                f"{', '.join(sorted(unknown))}; allowed: "
                f"{', '.join(sorted(_AUTHOR_OBJECT_KEYS))}"
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MarketplaceYmlError(f"'{ctx}.name' is required and must be a non-empty string")
        out: dict[str, str] = {"name": name.strip()}
        for key in ("email", "url"):
            val = raw.get(key)
            if val is None:
                continue
            if not isinstance(val, str) or not val.strip():
                raise MarketplaceYmlError(f"'{ctx}.{key}' must be a non-empty string")
            out[key] = val.strip()
        return out
    raise MarketplaceYmlError(f"'{ctx}' must be a string or object, got {type(raw).__name__}")


def _require_str(
    data: dict[str, Any],
    key: str,
    *,
    context: str = "",
) -> str:
    """Return a non-empty string value or raise ``MarketplaceYmlError``."""
    path = f"{context}.{key}" if context else key
    value = data.get(key)
    if value is None:
        raise MarketplaceYmlError(f"'{path}' is required")
    if not isinstance(value, str) or not value.strip():
        raise MarketplaceYmlError(f"'{path}' must be a non-empty string")
    return value.strip()


def _validate_semver(version: str, *, context: str = "version") -> None:
    """Raise if *version* is not a valid semver string."""
    if not _SEMVER_RE.match(version):
        raise MarketplaceYmlError(
            f"'{context}' value '{version}' is not valid semver (expected x.y.z)"
        )


def _validate_source(source: str, *, index: int) -> None:
    """Validate ``source`` field shape and path safety.

    Accepts either ``owner/repo`` (remote) or ``./...`` (local path).
    """
    ctx = f"packages[{index}].source"
    if not SOURCE_RE.match(source):
        raise MarketplaceYmlError(
            f"'{ctx}' must match '<owner>/<repo>' or './<path>' shape, got '{source}'"
        )
    is_local = bool(LOCAL_SOURCE_RE.match(source))
    try:
        # Local paths legitimately start with ``.`` (current dir) and
        # may have trailing-slash forms like ``./``.  Allow ``.`` here.
        validate_path_segments(source, context=ctx, allow_current_dir=is_local)
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc


def _validate_tag_pattern(pattern: str, *, context: str) -> None:
    """Ensure *pattern* contains at least one recognised placeholder."""
    if not any(ph in pattern for ph in _TAG_PLACEHOLDERS):
        raise MarketplaceYmlError(
            f"'{context}' must contain at least one of "
            f"{', '.join(_TAG_PLACEHOLDERS)}, got '{pattern}'"
        )


def _check_unknown_keys(
    data: dict[str, Any],
    permitted: frozenset,
    *,
    context: str,
) -> None:
    """Raise on any key not in *permitted*."""
    unknown = set(data.keys()) - permitted
    if unknown:
        sorted_unknown = sorted(unknown)
        sorted_permitted = sorted(permitted)
        raise MarketplaceYmlError(
            f"Unknown key(s) in {context}: {', '.join(sorted_unknown)}. "
            f"Permitted keys: {', '.join(sorted_permitted)}"
        )


def _parse_owner(raw: Any) -> MarketplaceOwner:
    """Parse and validate the ``owner`` block."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'owner' must be a mapping with at least a 'name' key")
    name = _require_str(raw, "name", context="owner")
    email = raw.get("email")
    if email is not None:
        email = str(email).strip() or None
    url = raw.get("url")
    if url is not None:
        url = str(url).strip() or None
    return MarketplaceOwner(name=name, email=email, url=url)


def _parse_build(raw: Any) -> MarketplaceBuild:
    """Parse and validate the ``build`` block."""
    if raw is None:
        return MarketplaceBuild()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'build' must be a mapping")
    _check_unknown_keys(raw, _BUILD_KEYS, context="build")
    tag_pattern = raw.get("tagPattern", "v{version}")
    if not isinstance(tag_pattern, str) or not tag_pattern.strip():
        raise MarketplaceYmlError("'build.tagPattern' must be a non-empty string")
    tag_pattern = tag_pattern.strip()
    _validate_tag_pattern(tag_pattern, context="build.tagPattern")
    return MarketplaceBuild(tag_pattern=tag_pattern)


def _parse_versioning(raw: Any) -> MarketplaceVersioning:
    """Parse and validate the optional ``marketplace.versioning`` block."""
    if raw is None:
        return MarketplaceVersioning()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"'versioning' must be a mapping, got {type(raw).__name__}")
    _check_unknown_keys(raw, _VERSIONING_KEYS, context="versioning")
    strategy = raw.get("strategy", "lockstep")
    if not isinstance(strategy, str) or not strategy.strip():
        raise MarketplaceYmlError("'versioning.strategy' must be a non-empty string")
    strategy = strategy.strip()
    if strategy not in _VERSIONING_STRATEGIES:
        valid = ", ".join(sorted(_VERSIONING_STRATEGIES))
        raise MarketplaceYmlError(
            f"'versioning.strategy' must be one of: {valid}; got {strategy!r}"
        )
    return MarketplaceVersioning(strategy=strategy)


def _parse_claude(raw: Any, *, default_output: str) -> MarketplaceClaudeConfig:
    """Parse and validate the optional ``marketplace.claude`` block."""
    if raw is None:
        return MarketplaceClaudeConfig(output=default_output)
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'claude' must be a mapping")
    _check_unknown_keys(raw, _CLAUDE_KEYS, context="claude")

    output = raw.get("output", default_output)
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'claude.output' must be a non-empty string")
    output = output.strip()
    try:
        validate_path_segments(output, context="claude.output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    return MarketplaceClaudeConfig(output=output)


def _parse_codex(raw: Any) -> MarketplaceCodexConfig:
    """Parse and validate the optional ``marketplace.codex`` block."""
    if raw is None:
        return MarketplaceCodexConfig()
    if not isinstance(raw, dict):
        raise MarketplaceYmlError("'codex' must be a mapping")
    _check_unknown_keys(raw, _CODEX_KEYS, context="codex")

    output = raw.get("output", MARKETPLACE_OUTPUTS["codex"].default_output)
    if not isinstance(output, str) or not output.strip():
        raise MarketplaceYmlError("'codex.output' must be a non-empty string")
    output = output.strip()
    try:
        validate_path_segments(output, context="codex.output")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc

    return MarketplaceCodexConfig(output=output)


def _parse_outputs(
    raw: Any,
    warnings_sink: list[str] | None = None,
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    """Parse the marketplace output selector.

    Accepts:
    - ``None`` → default (claude only).
    - A list of strings → back-compat list form (emits deprecation warning).
    - A string → single-element back-compat list form.
    - A dict → new map form with optional per-format ``path:``.

    Returns ``(outputs_tuple, output_specs_tuple)``.
    """
    if raw is None:
        default_spec = MarketplaceOutputSpec(
            name="claude",
            path=MARKETPLACE_OUTPUTS["claude"].default_output,
            path_explicit=False,
        )
        return ("claude",), (default_spec,)

    # --- Map form (new) ---
    if isinstance(raw, dict):
        outputs: list[str] = []
        specs: list[MarketplaceOutputSpec] = []
        seen: set[str] = set()
        known = known_output_names()

        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise MarketplaceYmlError("'outputs' map keys must be non-empty strings")
            name = key.strip()
            if name not in known:
                raise MarketplaceYmlError(
                    f"Unknown marketplace output '{name}'. "
                    f"Permitted outputs: {', '.join(sorted(known))}"
                )
            if name in seen:
                raise MarketplaceYmlError(f"Duplicate marketplace output '{name}'")
            seen.add(name)

            # Value can be null/{}/mapping with optional path
            path_explicit = False
            path = MARKETPLACE_OUTPUTS[name].default_output
            if value is not None:
                if not isinstance(value, dict):
                    raise MarketplaceYmlError(f"'outputs.{name}' must be a mapping or null")
                raw_path = value.get("path")
                if raw_path is not None:
                    if not isinstance(raw_path, str) or not raw_path.strip():
                        raise MarketplaceYmlError(
                            f"'outputs.{name}.path' must be a non-empty string"
                        )
                    path = raw_path.strip()
                    path_explicit = True
                    try:
                        validate_path_segments(path, context=f"outputs.{name}.path")
                    except PathTraversalError as exc:
                        raise MarketplaceYmlError(str(exc)) from exc
                # Check for unknown keys inside the format entry
                _valid_output_entry_keys = {"path"}
                unknown = set(value.keys()) - _valid_output_entry_keys
                if unknown:
                    raise MarketplaceYmlError(
                        f"Unknown key(s) in 'outputs.{name}': {', '.join(sorted(unknown))}"
                    )

            outputs.append(name)
            specs.append(MarketplaceOutputSpec(name=name, path=path, path_explicit=path_explicit))

        if not outputs:
            raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")
        return tuple(outputs), tuple(specs)

    # --- List / string form (deprecated back-compat) ---
    if isinstance(raw, str):
        raw_items = [raw]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise MarketplaceYmlError("'outputs' must be a string, list, or mapping")

    outputs_list: list[str] = []
    specs_list: list[MarketplaceOutputSpec] = []
    seen_set: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, str) or not item.strip():
            raise MarketplaceYmlError(f"'outputs[{index}]' must be a non-empty string")
        output = item.strip()
        known_outputs = known_output_names()
        if output not in known_outputs:
            raise MarketplaceYmlError(
                f"Unknown marketplace output '{output}'. "
                f"Permitted outputs: {', '.join(sorted(known_outputs))}"
            )
        if output in seen_set:
            raise MarketplaceYmlError(f"Duplicate marketplace output '{output}'")
        seen_set.add(output)
        outputs_list.append(output)
        specs_list.append(
            MarketplaceOutputSpec(
                name=output,
                path=MARKETPLACE_OUTPUTS[output].default_output,
                path_explicit=False,
            )
        )

    if not outputs_list:
        raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")

    # Emit deprecation warning for list/string form
    names_str = ", ".join(outputs_list)
    map_lines = "\n".join(f"        {n}: {{}}" for n in outputs_list)
    deprecation_msg = (
        f"outputs: [{names_str}] is deprecated; use the map form:\n\n"
        f"      outputs:\n{map_lines}\n\n"
        f"    The list form will be removed in v0.15."
    )
    if warnings_sink is not None:
        warnings_sink.append(deprecation_msg)

    return tuple(outputs_list), tuple(specs_list)


def _parse_package_entry(raw: Any, index: int) -> PackageEntry:
    """Parse and validate a single ``packages`` entry."""
    if not isinstance(raw, dict):
        raise MarketplaceYmlError(f"packages[{index}] must be a mapping")

    # -- strict key check --
    _check_unknown_keys(raw, _PACKAGE_ENTRY_KEYS, context=f"packages[{index}]")

    name = _require_str(raw, "name", context=f"packages[{index}]")
    source = _require_str(raw, "source", context=f"packages[{index}]")
    _validate_source(source, index=index)
    is_local = bool(LOCAL_SOURCE_RE.match(source))

    # APM-only: subdir (irrelevant for local packages but harmless)
    subdir: str | None = raw.get("subdir")
    if subdir is not None:
        if not isinstance(subdir, str) or not subdir.strip():
            raise MarketplaceYmlError(f"'packages[{index}].subdir' must be a non-empty string")
        subdir = subdir.strip()
        try:
            validate_path_segments(subdir, context=f"packages[{index}].subdir")
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc

    # APM-only: version (semver range -- stored as string, not parsed here)
    version: str | None = raw.get("version")
    if version is not None:
        version = str(version).strip()
        if not version:
            raise MarketplaceYmlError(f"'packages[{index}].version' must be a non-empty string")

    # APM-only: ref
    ref: str | None = raw.get("ref")
    if ref is not None:
        ref = str(ref).strip()
        if not ref:
            raise MarketplaceYmlError(f"'packages[{index}].ref' must be a non-empty string")

    # At least one of version or ref must be present for REMOTE packages.
    # Local-path packages skip git resolution so the requirement does not
    # apply to them.
    if not is_local and version is None and ref is None:
        raise MarketplaceYmlError(
            f"packages[{index}] ('{name}'): remote packages require at "
            f"least one of 'version' or 'ref'"
        )

    # APM-only: tag_pattern
    tag_pattern: str | None = raw.get("tag_pattern")
    if tag_pattern is not None:
        if not isinstance(tag_pattern, str) or not tag_pattern.strip():
            raise MarketplaceYmlError(f"'packages[{index}].tag_pattern' must be a non-empty string")
        tag_pattern = tag_pattern.strip()
        _validate_tag_pattern(tag_pattern, context=f"packages[{index}].tag_pattern")

    # APM-only: include_prerelease
    include_prerelease = raw.get("include_prerelease", False)
    if not isinstance(include_prerelease, bool):
        raise MarketplaceYmlError(f"'packages[{index}].include_prerelease' must be a boolean")

    # Anthropic pass-through: description
    description: str | None = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            raise MarketplaceYmlError(f"'packages[{index}].description' must be a non-empty string")
        description = description.strip()

    # Anthropic pass-through: homepage
    homepage: str | None = raw.get("homepage")
    if homepage is not None:
        if not isinstance(homepage, str) or not homepage.strip():
            raise MarketplaceYmlError(f"'packages[{index}].homepage' must be a non-empty string")
        homepage = homepage.strip()

    # Anthropic pass-through: tags
    raw_tags = raw.get("tags")
    tags: tuple[str, ...] = ()
    if raw_tags is not None:
        if not isinstance(raw_tags, list):
            raise MarketplaceYmlError(f"'packages[{index}].tags' must be a list of strings")
        for i, item in enumerate(raw_tags):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'packages[{index}].tags[{i}]' must be a string, got {type(item).__name__}"
                )
        tags = tuple(str(t) for t in raw_tags)

    # Anthropic pass-through: keywords (alias for tags -- merged, deduplicated)
    raw_keywords = raw.get("keywords")
    if raw_keywords is not None:
        if not isinstance(raw_keywords, list):
            raise MarketplaceYmlError(f"'packages[{index}].keywords' must be a list of strings")
        for i, item in enumerate(raw_keywords):
            if not isinstance(item, str):
                raise MarketplaceYmlError(
                    f"'packages[{index}].keywords[{i}]' must be a string, got {type(item).__name__}"
                )
        # Merge: tags first, then keywords entries (deduplicated)
        seen = set(tags)
        merged = list(tags)
        for kw in raw_keywords:
            if kw not in seen:
                seen.add(kw)
                merged.append(kw)
        tags = tuple(merged)

    # S4: cap tags array length and item length
    if len(tags) > _MAX_TAGS_COUNT:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "packages[%d] ('%s'): tags truncated from %d to %d items",
            index,
            name,
            len(tags),
            _MAX_TAGS_COUNT,
        )
        tags = tags[:_MAX_TAGS_COUNT]
    tags = tuple(t[:_MAX_TAG_LENGTH] for t in tags)

    # Anthropic pass-through: author -- accept string OR object input,
    # normalize to ``{name, email?, url?}`` per the Claude Code plugin
    # manifest schema (json.schemastore.org/claude-code-plugin-manifest.json).
    author = _parse_author(raw.get("author"), index)

    # Anthropic pass-through: license (S3 -- must be str)
    license_val: str | None = raw.get("license")
    if license_val is not None:
        if not isinstance(license_val, str) or not license_val.strip():
            raise MarketplaceYmlError(f"'packages[{index}].license' must be a non-empty string")
        license_val = license_val.strip()

    # Anthropic pass-through: repository (S3 -- must be str)
    repository: str | None = raw.get("repository")
    if repository is not None:
        if not isinstance(repository, str) or not repository.strip():
            raise MarketplaceYmlError(f"'packages[{index}].repository' must be a non-empty string")
        repository = repository.strip()

    # Optional marketplace category. Claude output strips this; Codex output
    # requires and emits it.
    category: str | None = None
    raw_category = raw.get("category")
    if raw_category is not None:
        if not isinstance(raw_category, str) or not raw_category.strip():
            raise MarketplaceYmlError(f"'packages[{index}].category' must be a non-empty string")
        category = raw_category.strip()

    return PackageEntry(
        name=name,
        source=source,
        subdir=subdir,
        version=version,
        ref=ref,
        tag_pattern=tag_pattern,
        include_prerelease=include_prerelease,
        description=description,
        homepage=homepage,
        tags=tags,
        author=author,
        license=license_val,
        repository=repository,
        category=category,
        is_local=is_local,
    )
