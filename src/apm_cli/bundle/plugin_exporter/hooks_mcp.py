"""Hooks and MCP server merging utilities."""

_MAX_MERGE_DEPTH = 20


def deep_merge(base: dict, overlay: dict, *, overwrite: bool = False, _depth: int = 0) -> None:
    """Recursively merge *overlay* into *base*.

    When *overwrite* is False (default), existing base keys win.
    When *overwrite* is True, overlay keys overwrite base keys.

    Raises ``ValueError`` if nesting exceeds ``_MAX_MERGE_DEPTH``.
    """
    if _depth > _MAX_MERGE_DEPTH:
        raise ValueError(f"Hooks/MCP config exceeds maximum nesting depth ({_MAX_MERGE_DEPTH})")
    for key, value in overlay.items():
        if key not in base:
            base[key] = value
        elif overwrite:
            if isinstance(base[key], dict) and isinstance(value, dict):
                deep_merge(base[key], value, overwrite=True, _depth=_depth + 1)
            else:
                base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value, overwrite=False, _depth=_depth + 1)
