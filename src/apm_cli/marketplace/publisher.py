"""Marketplace publisher service -- update consumer repos with new versions.

Provides ``MarketplacePublisher`` for updating marketplace version
references in consumer repositories.  The publisher reads the local
``marketplace.yml``, computes a deterministic branch name and commit
message, then clones each consumer repo, updates its ``apm.yml``, and
pushes a feature branch.

This module is a library only -- no CLI wiring.  The CLI command
(``apm marketplace publish``) is wired in a later wave.

Design
------
* **Byte integrity**: the publisher NEVER modifies or regenerates
  ``marketplace.json`` content.  It only copies the file as-is from
  the marketplace source repo.
* **Token redaction**: stderr from git subprocesses is redacted via
  ``_git_utils.redact_token``.
* **Atomic writes**: state files and consumer ``apm.yml`` updates use
  write-tmp + ``os.fsync`` + ``os.replace``.
* **Error isolation**: failures in one target never abort other targets.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass  # noqa: F401
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any  # noqa: F401

from ..utils.path_security import (
    ensure_path_within,
    validate_path_segments,
)
from ._git_utils import redact_token as _redact_token
from ._io import atomic_write
from .ref_resolver import RefResolver
from .yml_schema import load_marketplace_yml

logger = logging.getLogger(__name__)

__all__ = [
    "ConsumerTarget",
    "MarketplacePublisher",
    "PublishOutcome",
    "PublishPlan",
    "PublishState",
    "TargetResult",
]

# ---------------------------------------------------------------------------
# Token redaction -- delegated to _git_utils; alias kept for call-site compat.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Branch name sanitisation
# ---------------------------------------------------------------------------

_BRANCH_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")

# Pattern for safe git remote URLs (HTTPS or SSH).
_SAFE_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

# Shell metacharacters that must never appear in branch names or repo slugs.
_SHELL_META_RE = re.compile(r"[;&|`$(){}!<>\"\']")


def _sanitise_branch_segment(text: str) -> str:
    """Replace characters that are unsafe for git branch names with hyphens."""
    return _BRANCH_UNSAFE_RE.sub("-", text)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
_BRANCH_SAFE_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


@dataclass(frozen=True)
class ConsumerTarget:
    """A consumer repository whose ``apm.yml`` should be updated."""

    repo: str  # e.g. "acme-org/service-a"
    branch: str = "main"  # base branch on the consumer to PR into
    path_in_repo: str = "apm.yml"  # location of the consumer's apm.yml

    def __post_init__(self) -> None:
        if not _REPO_RE.match(self.repo):
            raise ValueError(
                f"ConsumerTarget.repo must be in 'owner/name' format "
                f"using only alphanumerics, dots, hyphens, and underscores. "
                f"Got: {self.repo!r}"
            )
        if not _BRANCH_SAFE_RE.match(self.branch) or ".." in self.branch:
            raise ValueError(
                f"ConsumerTarget.branch contains disallowed characters. "
                f"Only alphanumerics, dots, hyphens, underscores, and "
                f"forward slashes are permitted (no '..' sequences). "
                f"Got: {self.branch!r}"
            )

        validate_path_segments(self.path_in_repo, context="consumer-targets path_in_repo")


@dataclass(frozen=True)
class PublishPlan:
    """Computed plan for a publish run -- frozen and deterministic."""

    marketplace_name: str  # name from the local marketplace.yml
    marketplace_version: str  # version from the local marketplace.yml
    targets: tuple[ConsumerTarget, ...]
    commit_message: str  # pre-computed, contains the APM trailer
    branch_name: str  # pre-computed, deterministic
    new_ref: str  # rendered tag, e.g. "v2.0.0"
    tag_pattern_used: str  # tag pattern, e.g. "v{version}"
    short_hash: str = ""  # deterministic hash suffix for the branch name
    allow_downgrade: bool = False
    allow_ref_change: bool = False
    target_package: str | None = None


class PublishOutcome(str, Enum):
    """Outcome of processing a single consumer target."""

    UPDATED = "updated"
    NO_CHANGE = "no-change"
    SKIPPED_DOWNGRADE = "skipped-downgrade"
    SKIPPED_REF_CHANGE = "skipped-ref-change"
    FAILED = "failed"


@dataclass(frozen=True)
class TargetResult:
    """Result of processing a single consumer target."""

    target: ConsumerTarget
    outcome: PublishOutcome
    message: str  # human-readable detail
    old_version: str | None = None
    new_version: str | None = None


# ---------------------------------------------------------------------------
# Transactional state file
# ---------------------------------------------------------------------------

_STATE_FILENAME = "publish-state.json"
_STATE_DIR = ".apm"
_MAX_HISTORY = 10
_SCHEMA_VERSION = 1


class PublishState:
    """Transactional state file for publish runs.

    State is persisted at ``.apm/publish-state.json`` relative to the
    marketplace repo root.  All writes are atomic (write-tmp + fsync +
    ``os.replace``).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._state_dir = self._root / _STATE_DIR
        self._state_path = self._state_dir / _STATE_FILENAME
        self._data: dict[str, Any] = {
            "schemaVersion": _SCHEMA_VERSION,
            "lastRun": None,
            "history": [],
        }

    @classmethod
    def load(cls, root: Path) -> PublishState:
        """Load state from disk or return a fresh instance.

        A missing file or corrupt JSON both result in a fresh state --
        no exception is raised.
        """
        instance = cls(root)
        if instance._state_path.exists():
            try:
                text = instance._state_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, dict):
                    instance._data = data
            except (json.JSONDecodeError, OSError):
                pass  # start fresh on corrupt state
        return instance

    def _atomic_write(self) -> None:
        """Write state atomically via temp file + fsync + os.replace.

        Path validation and directory creation happen here; the actual
        write is delegated to the shared ``atomic_write()`` helper from
        ``_io.py``.
        """
        ensure_path_within(self._state_dir, self._root)
        self._state_dir.mkdir(parents=True, exist_ok=True)

        content = json.dumps(self._data, indent=2) + "\n"
        atomic_write(self._state_path, content)

    def begin_run(self, plan: PublishPlan) -> None:
        """Start a new publish run -- writes ``startedAt``."""
        self._data["lastRun"] = {
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "finishedAt": None,
            "marketplaceName": plan.marketplace_name,
            "marketplaceVersion": plan.marketplace_version,
            "branchName": plan.branch_name,
            "results": [],
        }
        self._atomic_write()

    def record_result(self, result: TargetResult) -> None:
        """Append a target result to the current run."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["results"].append(
            {
                "repo": result.target.repo,
                "outcome": result.outcome.value,
                "message": result.message,
                "oldVersion": result.old_version,
                "newVersion": result.new_version,
            }
        )
        self._atomic_write()

    def finalise(self, finished_at: datetime) -> None:
        """Finalise the current run and rotate history."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["finishedAt"] = finished_at.isoformat()

        # Rotate history -- keep at most _MAX_HISTORY entries
        history = self._data.get("history", [])
        history.insert(0, dict(self._data["lastRun"]))
        self._data["history"] = history[:_MAX_HISTORY]
        self._atomic_write()

    def abort(self, reason: str) -> None:
        """Mark the current run as aborted."""
        if self._data.get("lastRun") is None:
            return
        self._data["lastRun"]["finishedAt"] = f"ABORTED: {reason}"
        self._atomic_write()

    @property
    def data(self) -> dict[str, Any]:
        """Return the raw state data (read-only snapshot for inspection)."""
        return dict(self._data)


# ---------------------------------------------------------------------------
# Publisher service
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 60


class MarketplacePublisher:
    """Update consumer repositories with new marketplace versions.

    Parameters
    ----------
    marketplace_root:
        Path to the marketplace repository root (must contain
        ``marketplace.yml``).
    ref_resolver:
        Optional ``RefResolver`` instance (reserved for future use).
    clock:
        Callable returning the current ``datetime`` (injectable for
        tests).
    runner:
        Callable with the same signature as ``subprocess.run``
        (injectable for tests).
    """

    def __init__(
        self,
        marketplace_root: Path,
        *,
        ref_resolver: RefResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._root = marketplace_root.resolve()
        self._ref_resolver = ref_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._runner = runner or subprocess.run
        self._yml = None

    def _load_yml(self):
        """Lazy-load marketplace.yml."""
        if self._yml is None:
            yml_path = self._root / "marketplace.yml"
            self._yml = load_marketplace_yml(yml_path)
        return self._yml

    # -- plan ---------------------------------------------------------------

    def plan(
        self,
        targets: Sequence[ConsumerTarget],
        *,
        target_package: str | None = None,
        allow_downgrade: bool = False,
        allow_ref_change: bool = False,
    ) -> PublishPlan:
        return _process_target.plan(
            self,
            targets,
            target_package=target_package,
            allow_downgrade=allow_downgrade,
            allow_ref_change=allow_ref_change,
        )

    # -- execute ------------------------------------------------------------

    def execute(
        self, plan: PublishPlan, *, dry_run: bool = False, parallel: int = 4
    ) -> list[TargetResult]:
        return _process_target.execute(self, plan, dry_run=dry_run, parallel=parallel)

    # -- per-target processing ----------------------------------------------

    def _process_single_target(
        self, target: ConsumerTarget, plan: PublishPlan, *, dry_run: bool = False
    ) -> TargetResult:
        return _process_target._process_single_target(self, target, plan, dry_run=dry_run)

    # -- git runner ---------------------------------------------------------

    def _run_git(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int = _GIT_TIMEOUT
    ) -> subprocess.CompletedProcess:
        return _process_target._run_git(self, cmd, cwd=cwd, timeout=timeout)

    # -- safe force push ----------------------------------------------------

    def safe_force_push(self, remote: str, branch_name: str, expected_trailer: str) -> bool:
        return _process_target.safe_force_push(self, remote, branch_name, expected_trailer)


from . import process_target as _process_target
