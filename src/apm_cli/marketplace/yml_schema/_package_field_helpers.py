"""Low-level field validators and parsers for marketplace package entries.

Extracted from :mod:`parse_helpers` to keep that module at ≤500 lines.
All symbols that callers previously imported from ``parse_helpers`` are
re-exported there via a module-level import; nothing in this private
module is part of the public API.
"""

from __future__ import annotations

from typing import Any

from ...utils.path_security import PathTraversalError, validate_path_segments
from ..errors import MarketplaceYmlError

_MAX_TAGS_COUNT = 50
_MAX_TAG_LENGTH = 100
_AUTHOR_OBJECT_KEYS = frozenset({"name", "email", "url"})


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


def _optional_non_empty_string(raw: dict[str, Any], key: str, *, index: int) -> str | None:
    """Return a stripped optional string field from a package entry."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MarketplaceYmlError(f"'packages[{index}].{key}' must be a non-empty string")
    return value.strip()


def _optional_validated_path(raw: dict[str, Any], key: str, *, index: int) -> str | None:
    """Return a stripped path-like field after validating path safety."""
    value = _optional_non_empty_string(raw, key, index=index)
    if value is None:
        return None
    try:
        validate_path_segments(value, context=f"packages[{index}].{key}")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc
    return value


def _parse_package_tags(raw: dict[str, Any], key: str, *, index: int) -> tuple[str, ...]:
    """Parse a list-of-strings package metadata field."""
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MarketplaceYmlError(f"'packages[{index}].{key}' must be a list of strings")
    for item_index, item in enumerate(value):
        if not isinstance(item, str):
            raise MarketplaceYmlError(
                f"'packages[{index}].{key}[{item_index}]' must be a string, got {type(item).__name__}"
            )
    return tuple(str(item) for item in value)


def _merge_and_cap_tags(
    *, tags: tuple[str, ...], keywords: tuple[str, ...], index: int, name: str
) -> tuple[str, ...]:
    """Merge tags and keywords, then apply repository caps."""
    merged = list(tags)
    seen = set(tags)
    for keyword in keywords:
        if keyword not in seen:
            seen.add(keyword)
            merged.append(keyword)
    result = tuple(merged)
    if len(result) > _MAX_TAGS_COUNT:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "packages[%d] ('%s'): tags truncated from %d to %d items",
            index,
            name,
            len(result),
            _MAX_TAGS_COUNT,
        )
        result = result[:_MAX_TAGS_COUNT]
    return tuple(tag[:_MAX_TAG_LENGTH] for tag in result)
