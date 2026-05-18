"""Format-specific agent file writers used by AgentIntegrator.

Private helpers extracted to keep ``agent_integrator.py`` under 500 lines.
Not part of the public API; access only via :class:`AgentIntegrator`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# Compiled once at import time; shared by both writer functions.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?",
    re.DOTALL,
)


def write_codex_agent(source: Path, target: Path) -> None:
    """Transform an ``.agent.md`` file to Codex ``.toml`` format.

    Parses YAML frontmatter for ``name`` and ``description``, uses
    the markdown body as ``developer_instructions``.
    """
    if source.is_symlink():
        raise ValueError(f"Refusing to read symlink source: {source}")
    import toml as _toml

    content = source.read_text(encoding="utf-8")

    name = source.stem
    if name.endswith(".agent"):
        name = name[: -len(".agent")]
    description = ""
    body = content

    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        body = content[fm_match.end() :]
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
            name = fm.get("name", name)
            description = fm.get("description", description)
        except Exception:
            pass

    doc = {
        "name": name,
        "description": description,
        "developer_instructions": body.strip(),
    }
    target.write_text(_toml.dumps(doc), encoding="utf-8")


def write_windsurf_agent_skill(
    source: Path,
    target: Path,
    resolve_links_fn: Callable[[str, Path, Path], tuple[str, int]],
    diagnostics: Any = None,
) -> int:
    """Transform an ``.agent.md`` file to a Windsurf Skill (``SKILL.md``).

    Windsurf Skills are the closest equivalent to a specialist persona:
    - Invocable with ``@skill-name`` (like ``@agent-name`` in Copilot)
    - Auto-invoked by Cascade when the description matches the task
    - Support a directory with supplementary resource files

    The conversion:
    - Keeps ``name`` (or derives from filename) and ``description``.
    - Strips agent-specific keys (``model``, ``tools``) and emits a
      diagnostic warning when those fields are dropped.
    - Preserves the markdown body verbatim.

    Args:
        source: Source ``.agent.md`` file.
        target: Destination ``SKILL.md`` path.
        resolve_links_fn: Bound method from the owning :class:`AgentIntegrator`
            instance (``self.resolve_links``).  Passed as a callback to avoid
            coupling this module to the integrator class hierarchy.
        diagnostics: Optional diagnostics collector; receives a ``warn`` call
            when agent-only frontmatter fields are dropped.

    Returns:
        Number of context links resolved in the output.
    """
    if source.is_symlink():
        raise ValueError(f"Refusing to read symlink source: {source}")
    content = source.read_text(encoding="utf-8")

    stem = source.name
    if stem.endswith(".agent.md"):
        stem = stem[:-9]
    elif stem.endswith(".chatmode.md"):
        stem = stem[:-12]
    else:
        stem = Path(stem).stem

    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        body = content[fm_match.end() :]
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            fm = {}
    else:
        body = content
        fm = {}

    dropped = [k for k in ("tools", "model") if fm.get(k)]
    if dropped and diagnostics is not None:
        diagnostics.warn(
            f"Windsurf skill conversion dropped frontmatter field(s) "
            f"{', '.join(dropped)} from {source.name}",
            detail="Windsurf Skills do not support agent-only fields; "
            "only name, description, and body are preserved.",
        )

    name = fm.get("name", stem)
    description = fm.get("description", "")

    # Use yaml.safe_dump to safely serialize values -- prevents YAML key
    # injection via multi-line name/description strings.
    fm_data: dict = {"name": name}
    if description:
        fm_data["description"] = description
    fm_yaml = yaml.safe_dump(  # yaml-io-exempt: serializes to string, not file handle
        fm_data, default_flow_style=False, allow_unicode=True
    ).rstrip("\n")

    result = f"---\n{fm_yaml}\n---\n" + body
    result, links_resolved = resolve_links_fn(result, source, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result, encoding="utf-8")
    return links_resolved
