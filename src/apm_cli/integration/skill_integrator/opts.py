"""Dataclass parameter objects for skill integrator functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillPromoteOpts:
    """Optional arguments for :func:`_promote_sub_skills`."""

    warn: bool = True
    owned_by: dict[str, str] | None = None
    diagnostics: Any = None
    managed_files: Any = None
    force: bool = False
    project_root: Path | None = None
    logger: Any = None
    name_filter: Any = None  # set | None


@dataclass
class SkillOpts:
    """Optional arguments for skill integration functions.

    Used by ``_integrate_native_skill``, ``_integrate_skill_bundle``,
    and ``integrate_package_skill``.
    """

    diagnostics: Any = None
    managed_files: Any = None
    force: bool = False
    logger: Any = None
    targets: Any = None
    skill_subset: Any = None
