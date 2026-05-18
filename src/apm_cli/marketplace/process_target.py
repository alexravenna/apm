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

import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..utils.path_security import validate_path_segments
from ._git_utils import redact_token as _redact_token
from ._target_executor import _process_single_target as _process_single_target  # noqa: PLC0414
from .errors import MarketplaceError  # noqa: F401
from .publisher import (
    ConsumerTarget,
    PublishOutcome,
    PublishPlan,
    PublishState,
    TargetResult,
    _sanitise_branch_segment,
)
from .tag_pattern import render_tag

logger = logging.getLogger(__name__)
_SAFE_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
_SHELL_META_RE = re.compile(r"[;&|`$(){}!<>\"\']")
_GIT_TIMEOUT = 60


def plan(
    self,
    targets: Sequence[ConsumerTarget],
    *,
    target_package: str | None = None,
    allow_downgrade: bool = False,
    allow_ref_change: bool = False,
) -> PublishPlan:
    """Compute a publish plan.

    Reads the local ``marketplace.yml`` to discover the marketplace
    name and version, validates all targets, and computes a
    deterministic branch name and commit message.

    Parameters
    ----------
    targets:
        Consumer repositories to update.
    target_package:
        If set, only update the reference for this specific package.
        If ``None``, bump the marketplace version across all targets.
    allow_downgrade:
        Allow version downgrades (new < old).
    allow_ref_change:
        Allow switching from an explicit ref to a version range.

    Returns
    -------
    PublishPlan
        Frozen plan ready for ``execute()``.

    Raises
    ------
    MarketplaceYmlError
        If ``marketplace.yml`` cannot be loaded or is invalid.
    PathTraversalError
        If any target's ``path_in_repo`` is a path traversal.
    """
    yml = self._load_yml()

    # Validate path_in_repo for each target
    for target in targets:
        validate_path_segments(
            target.path_in_repo,
            context=f"path_in_repo for {target.repo}",
        )

    # Validate repo and branch for each target
    for target in targets:
        # Repo must be a safe "owner/repo" slug with no shell metacharacters.
        if _SHELL_META_RE.search(target.repo):
            raise MarketplaceError(
                f"Consumer target repo '{target.repo}' contains prohibited shell metacharacters."
            )
        if not _SAFE_REPO_RE.match(target.repo):
            raise MarketplaceError(
                f"Consumer target repo '{target.repo}' must match "
                f"'owner/repo' (alphanumeric, dots, hyphens, underscores)."
            )
        # Branch must not contain traversal sequences or shell metacharacters.
        validate_path_segments(
            target.branch,
            context=f"consumer target branch for {target.repo}",
        )
        if _SHELL_META_RE.search(target.branch):
            raise MarketplaceError(
                f"Consumer target branch '{target.branch}' for "
                f"'{target.repo}' contains prohibited shell metacharacters."
            )

    # Compute short hash
    sorted_repos = sorted(t.repo for t in targets)
    hash_input = "|".join(sorted_repos) + "|" + yml.version
    if target_package:
        hash_input += "|" + target_package
    short_hash = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()[:8]  # noqa: S324

    # Compute branch name
    name_segment = _sanitise_branch_segment(yml.name)
    version_segment = _sanitise_branch_segment(yml.version)
    branch_name = f"apm/marketplace-update-{name_segment}-{version_segment}-{short_hash}"

    # Compute commit message
    commit_message = (
        f"chore(apm): bump {yml.name} to {yml.version}\n"
        f"\n"
        f"Updated by apm marketplace publish.\n"
        f"\n"
        f"APM-Publish-Id: {short_hash}"
    )

    # Compute tag for the new version
    tag_pattern = yml.build.tag_pattern
    new_ref = render_tag(tag_pattern, name=yml.name, version=yml.version)

    return PublishPlan(
        marketplace_name=yml.name,
        marketplace_version=yml.version,
        targets=tuple(targets),
        commit_message=commit_message,
        branch_name=branch_name,
        new_ref=new_ref,
        tag_pattern_used=tag_pattern,
        short_hash=short_hash,
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
        target_package=target_package,
    )


def execute(
    self,
    plan: PublishPlan,
    *,
    dry_run: bool = False,
    parallel: int = 4,
) -> list[TargetResult]:
    """Execute a publish plan.

    Iterates targets in parallel, updating each consumer's
    ``apm.yml`` with the new marketplace version.

    Parameters
    ----------
    plan:
        Plan computed by ``plan()``.
    dry_run:
        If ``True``, do not push changes to remote.
    parallel:
        Maximum number of concurrent target updates.

    Returns
    -------
    list[TargetResult]
        Results in the same order as ``plan.targets``.
    """
    state = PublishState.load(self._root)
    state.begin_run(plan)

    results: dict[int, TargetResult] = {}

    def _process(idx: int, target: ConsumerTarget) -> TargetResult:
        try:
            return self._process_single_target(target, plan, dry_run=dry_run)
        except Exception as exc:
            logger.debug("Target processing failed for %s", target.repo, exc_info=True)
            return TargetResult(
                outcome=PublishOutcome.FAILED,
                message=_redact_token(str(exc)),
            )

    workers = max(1, min(parallel, len(plan.targets)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(_process, idx, target): idx for idx, target in enumerate(plan.targets)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.debug("Future result failed for target %d", idx, exc_info=True)
                result = TargetResult(
                    target=plan.targets[idx],
                    outcome=PublishOutcome.FAILED,
                    message=_redact_token(str(exc)),
                )
            results[idx] = result
            state.record_result(result)

    state.finalise(self._clock())

    # Return in plan.targets order
    return [results[i] for i in range(len(plan.targets))]


def _run_git(
    self,
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a git command via the injectable runner."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    return self._runner(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        env=env,
    )


def safe_force_push(
    self,
    remote: str,
    branch_name: str,
    expected_trailer: str,
) -> bool:
    """Force-push only if the remote branch head has the expected trailer.

    Checks that the remote branch's HEAD commit message contains
    ``APM-Publish-Id: <expected_trailer>``.  If it does, performs
    a ``git push --force-with-lease``; otherwise refuses silently.

    Returns ``True`` on push success, ``False`` if refused or on
    any error.  Never raises for the trailer-mismatch case.
    """
    try:
        result = self._run_git(
            [
                "git",
                "log",
                "--format=%B",
                "-1",
                f"{remote}/{branch_name}",
            ],
            cwd=str(self._root),
        )
        commit_msg = result.stdout.strip()

        trailer_line = f"APM-Publish-Id: {expected_trailer}"
        if trailer_line not in commit_msg:
            return False

        self._run_git(
            [
                "git",
                "push",
                "--force-with-lease",
                remote,
                branch_name,
            ],
            cwd=str(self._root),
        )
        return True
    except subprocess.CalledProcessError:
        return False
