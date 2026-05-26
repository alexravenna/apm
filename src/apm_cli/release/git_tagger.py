"""Git tagger for ``apm pack --create-tag --push``.

Plans, validates, creates, and pushes release tags derived from the
marketplace versioning strategy. All git side effects route through
:func:`apm_cli.utils.git_subprocess.run_git`; ``dry_run=True`` is strict
(no git side effects at all).

Auth boundary: this module never reads, stores, or forwards a
credential. ``git push`` relies on the user's existing git credential
setup (helper, SSH agent, or PAT in the remote URL) -- the same auth
they would use for ``git push origin v1.2.0`` by hand. The ``ls-remote``
call used to inspect existing remote tags inherits that same boundary.

Regression-trap invariant (test-locked): :meth:`GitTagger.push` builds
explicit ``refs/tags/<name>:refs/tags/<name>`` refspecs, one per planned
tag, and never invokes ``git push --tags`` (which would force-push every
unrelated local tag).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..marketplace._git_utils import redact_token
from ..marketplace.tag_pattern import render_tag
from ..utils.git_subprocess import run_git

__all__ = [
    "REFUSAL_DIRTY_TREE",
    "REFUSAL_GIT_FAILURE",
    "REFUSAL_NO_CHECK_VERSIONS",
    "REFUSAL_NO_MARKETPLACE",
    "REFUSAL_NO_REMOTE",
    "REFUSAL_PUSH_WITHOUT_TAG",
    "REFUSAL_TAG_EXISTS",
    "REFUSAL_VERSION_MISMATCH",
    "GitTagger",
    "TagCreationResult",
    "TagPlan",
    "TaggingRefusal",
]

# Refusal codes -- stable JSON contract for downstream consumers.
REFUSAL_DIRTY_TREE = "dirty_tree"
REFUSAL_TAG_EXISTS = "tag_exists"
REFUSAL_VERSION_MISMATCH = "version_mismatch"
REFUSAL_NO_REMOTE = "no_remote"
REFUSAL_NO_CHECK_VERSIONS = "no_check_versions"
REFUSAL_PUSH_WITHOUT_TAG = "push_without_tag"
REFUSAL_GIT_FAILURE = "git_failure"
REFUSAL_NO_MARKETPLACE = "no_marketplace"


@dataclass(frozen=True)
class TagPlan:
    """One tag the command intends to create."""

    name: str
    target_sha: str
    annotation: str
    source_package: str | None = None


@dataclass(frozen=True)
class TagCreationResult:
    """Outcome of a tag creation/push pass."""

    created: tuple[str, ...]
    pushed: tuple[str, ...]
    remote: str | None


class TaggingRefusal(Exception):
    """Pre-side-effect refusal. Carries a stable ``code`` for JSON output."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _package_to_dict(pkg: Any) -> dict[str, Any]:
    """Normalise a package entry (mapping, dataclass, or object) to a dict."""
    if isinstance(pkg, dict):
        return pkg
    return {
        "name": getattr(pkg, "name", None),
        "version": getattr(pkg, "version", None),
        "tag_pattern": getattr(pkg, "tag_pattern", None),
    }


class GitTagger:
    """Plan, preflight, create, and push release tags for a repo.

    Parameters
    ----------
    repo_root:
        Repository root directory. Subprocess CWD for every git call.
    dry_run:
        If True, :meth:`create` and :meth:`push` log intent but make no
        git calls. Preflight inspection (``status``, ``tag --list``,
        ``ls-remote``) still runs so the user gets accurate
        would-refuse signals.
    logger:
        Optional CommandLogger-like object. Looked up by attribute
        (``success``, ``info``, ``dry_run_notice``, ``error``); missing
        attributes are silently skipped so plain ``logging.Logger`` and
        ``MagicMock`` instances both work.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        dry_run: bool = False,
        logger: Any | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.dry_run = dry_run
        self.logger = logger

    # ------------------------------------------------------------------
    # Planning (pure; reads HEAD SHA via git rev-parse)
    # ------------------------------------------------------------------

    def plan_tags(
        self,
        strategy: str,
        marketplace_version: str | None,
        packages: list[Any],
        tag_pattern: str = "v{version}",
    ) -> list[TagPlan]:
        """Derive the tags this strategy implies.

        Strategies:

        * ``lockstep`` -- one tag ``v{marketplace_version}``.
        * ``tag_pattern`` -- one tag per package using the package's
          override ``tag_pattern`` (if any) else the default *tag_pattern*.
        * ``per_package`` -- one tag per package using ``{name}-v{version}``.

        Returns the planned tags in insertion order (lockstep produces
        exactly one; per-package strategies preserve the input order so
        the CLI rendering matches the manifest order).
        """
        target_sha = self._head_sha()
        plans: list[TagPlan] = []

        if strategy == "lockstep":
            if not marketplace_version:
                raise TaggingRefusal(
                    REFUSAL_VERSION_MISMATCH,
                    "Cannot derive lockstep tag: marketplace.version is missing.",
                )
            name = f"v{marketplace_version}"
            plans.append(
                TagPlan(
                    name=name,
                    target_sha=target_sha,
                    annotation=f"Release {name}",
                    source_package=None,
                )
            )
            return plans

        for pkg in packages:
            d = _package_to_dict(pkg)
            name = d.get("name") or ""
            version = d.get("version")
            if not name or not version:
                # --check-versions should have already failed on this;
                # skip silently to keep planning total.
                continue
            if strategy == "tag_pattern":
                pattern = d.get("tag_pattern") or tag_pattern
            elif strategy == "per_package":
                pattern = "{name}-v{version}"
            else:  # pragma: no cover - schema validates strategy upstream
                pattern = tag_pattern
            rendered = render_tag(pattern, name=name, version=version)
            plans.append(
                TagPlan(
                    name=rendered,
                    target_sha=target_sha,
                    annotation=f"Release {rendered}",
                    source_package=name,
                )
            )
        return plans

    # ------------------------------------------------------------------
    # Preflight (raises TaggingRefusal on any blocking condition)
    # ------------------------------------------------------------------

    def preflight(self, plans: list[TagPlan], *, remote: str | None) -> None:
        """Raise :class:`TaggingRefusal` on any blocking condition.

        Checks (in order):

        1. Working tree clean.
        2. No planned tag already exists locally.
        3. If *remote* is not None: the remote exists and none of the
           planned tags already exist on it.
        """
        if self._is_dirty():
            raise TaggingRefusal(
                REFUSAL_DIRTY_TREE,
                "Refusing to tag: working tree has uncommitted changes.",
                hint="commit or stash, then re-run.",
            )

        names = {p.name for p in plans}
        existing_local = self._existing_local_tags(names)
        if existing_local:
            first = sorted(existing_local)[0]
            raise TaggingRefusal(
                REFUSAL_TAG_EXISTS,
                f"Refusing to tag: '{first}' already exists.",
                hint=(
                    f"bump the marketplace version, or delete the existing tag with "
                    f"'git tag -d {first}'."
                ),
            )

        if remote is not None:
            if not self._remote_exists(remote):
                raise TaggingRefusal(
                    REFUSAL_NO_REMOTE,
                    f"Refusing to push: no '{remote}' remote configured.",
                    hint=(f"'git remote add {remote} <url>' or use --create-tag without --push."),
                )
            existing_remote = self._existing_remote_tags(remote, names)
            if existing_remote:
                first = sorted(existing_remote)[0]
                raise TaggingRefusal(
                    REFUSAL_TAG_EXISTS,
                    f"Refusing to push: '{first}' already exists on {remote}.",
                    hint=(
                        f"fetch with 'git fetch --tags' to sync, or delete remotely "
                        f"with 'git push {remote} :refs/tags/{first}'."
                    ),
                )

    # ------------------------------------------------------------------
    # Side effects (honour dry_run)
    # ------------------------------------------------------------------

    def create(self, plans: list[TagPlan]) -> list[str]:
        """Create annotated tags for *plans*. No-op in dry-run.

        Returns the list of tag names that were created (or would be).
        """
        names: list[str] = []
        for plan in plans:
            sha_short = plan.target_sha[:7] if plan.target_sha else "HEAD"
            if self.dry_run:
                self._log("dry_run_notice", f"Would create tag: {plan.name} (HEAD = {sha_short})")
                names.append(plan.name)
                continue
            self._run_or_raise(
                ["tag", "-a", "-m", plan.annotation, plan.name],
                op=f"git tag -a {plan.name}",
            )
            self._log("success", f"Created tag: {plan.name} (HEAD = {sha_short})")
            names.append(plan.name)
        return names

    def push(self, tag_names: list[str], *, remote: str) -> list[str]:
        """Push *tag_names* to *remote* by explicit refspec. No-op in dry-run.

        Never uses ``git push --tags`` (regression-trap-locked); the
        explicit refspec form guarantees only the planned tags move.
        """
        if not tag_names:
            return []
        if self.dry_run:
            for name in tag_names:
                self._log("dry_run_notice", f"Would push tag: {name} -> {remote}")
            return list(tag_names)
        refspecs = [f"refs/tags/{name}:refs/tags/{name}" for name in tag_names]
        self._run_or_raise(
            ["push", remote, *refspecs],
            op=f"git push {remote} ({len(refspecs)} tag(s))",
        )
        for name in tag_names:
            self._log("success", f"Pushed tag: {name} -> {remote}")
        return list(tag_names)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return run_git(args, cwd=self.repo_root)

    def _run_or_raise(self, args: list[str], *, op: str) -> subprocess.CompletedProcess:
        result = self._run(args)
        if result.returncode != 0:
            stderr = redact_token((result.stderr or "").strip())
            raise TaggingRefusal(
                REFUSAL_GIT_FAILURE,
                f"{op} failed (exit {result.returncode}): {stderr or 'no stderr'}",
            )
        return result

    def _head_sha(self) -> str:
        result = self._run(["rev-parse", "HEAD"])
        if result.returncode != 0:
            stderr = redact_token((result.stderr or "").strip())
            raise TaggingRefusal(
                REFUSAL_GIT_FAILURE,
                f"Cannot resolve HEAD: {stderr or 'no stderr'}",
            )
        return (result.stdout or "").strip()

    def _is_dirty(self) -> bool:
        result = self._run(["status", "--porcelain"])
        if result.returncode != 0:
            stderr = redact_token((result.stderr or "").strip())
            raise TaggingRefusal(
                REFUSAL_GIT_FAILURE,
                f"Cannot inspect working tree: {stderr or 'no stderr'}",
            )
        return bool((result.stdout or "").strip())

    def _existing_local_tags(self, candidates: set[str]) -> set[str]:
        if not candidates:
            return set()
        result = self._run(["tag", "--list"])
        if result.returncode != 0:
            return set()
        present = {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}
        return present & candidates

    def _remote_exists(self, remote: str) -> bool:
        result = self._run(["remote"])
        if result.returncode != 0:
            return False
        names = {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}
        return remote in names

    def _existing_remote_tags(self, remote: str, candidates: set[str]) -> set[str]:
        if not candidates:
            return set()
        # auth-delegated: ls-remote here inherits the user's existing
        # git credential setup -- APM does not read, store, or forward
        # any credential. The PAT/bearer protocol enforced by
        # AuthResolver does not apply: this is the same auth a user
        # would invoke with `git ls-remote origin` by hand.
        result = self._run(["ls-remote", "--tags", remote])
        if result.returncode != 0:
            # Network / auth failure -- skip remote check rather than
            # falsely refuse. The actual push will surface the real error.
            self._log(
                "info",
                f"Could not list remote tags from {remote}; skipping remote tag existence check.",
            )
            return set()
        present: set[str] = set()
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split("\t")
            if len(parts) < 2:
                continue
            ref = parts[1]
            if not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/") :]
            if name.endswith("^{}"):
                name = name[:-3]
            present.add(name)
        return present & candidates

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        fn = getattr(self.logger, level, None)
        if callable(fn):
            fn(message)
            return
        fn = getattr(self.logger, "info", None)
        if callable(fn):
            fn(message)
