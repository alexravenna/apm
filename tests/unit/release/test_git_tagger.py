"""Unit tests for ``apm_cli.release.git_tagger.GitTagger``."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.release.git_tagger import (
    REFUSAL_DIRTY_TREE,
    REFUSAL_GIT_FAILURE,
    REFUSAL_NO_REMOTE,
    REFUSAL_TAG_EXISTS,
    REFUSAL_VERSION_MISMATCH,
    GitTagger,
    TaggingRefusal,
)

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("git") is None,
    reason="git executable not on PATH",
)


# ---------------------------------------------------------------------------
# plan_tags
# ---------------------------------------------------------------------------


class TestPlanTags:
    def test_plan_tags_lockstep_produces_single_tag(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(
            strategy="lockstep",
            marketplace_version="1.2.0",
            packages=[{"name": "alpha", "version": "1.2.0"}],
        )
        assert len(plans) == 1
        assert plans[0].name == "v1.2.0"
        assert plans[0].source_package is None
        assert plans[0].annotation == "Release v1.2.0"
        assert len(plans[0].target_sha) == 40

    def test_plan_tags_per_package_produces_one_per_package(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(
            strategy="per_package",
            marketplace_version="1.0.0",
            packages=[
                {"name": "alpha", "version": "1.2.0"},
                {"name": "beta", "version": "2.0.0"},
            ],
        )
        assert [p.name for p in plans] == ["alpha-v1.2.0", "beta-v2.0.0"]
        assert [p.source_package for p in plans] == ["alpha", "beta"]

    def test_plan_tags_tag_pattern_honors_per_package_overrides(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(
            strategy="tag_pattern",
            marketplace_version="1.0.0",
            packages=[
                {"name": "alpha", "version": "1.2.0"},  # uses default
                {
                    "name": "beta",
                    "version": "2.0.0",
                    "tag_pattern": "release/{name}-{version}",
                },
            ],
            tag_pattern="v{version}",
        )
        assert plans[0].name == "v1.2.0"
        assert plans[1].name == "release/beta-2.0.0"

    def test_plan_tags_lockstep_without_version_raises(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        with pytest.raises(TaggingRefusal) as exc:
            tagger.plan_tags(
                strategy="lockstep",
                marketplace_version=None,
                packages=[],
            )
        assert exc.value.code == REFUSAL_VERSION_MISMATCH

    def test_plan_tags_accepts_dataclass_packages(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        pkg = SimpleNamespace(name="alpha", version="1.0.0", tag_pattern=None)
        plans = tagger.plan_tags(
            strategy="per_package",
            marketplace_version=None,
            packages=[pkg],
        )
        assert plans[0].name == "alpha-v1.0.0"


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_preflight_passes_when_all_clean(self, git_repo_factory):
        repo = git_repo_factory(with_remote=True)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        # Should not raise.
        tagger.preflight(plans, remote="origin")

    def test_preflight_raises_on_dirty_tree(self, git_repo_factory):
        repo = git_repo_factory(dirty=True)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        with pytest.raises(TaggingRefusal) as exc:
            tagger.preflight(plans, remote=None)
        assert exc.value.code == REFUSAL_DIRTY_TREE
        assert "uncommitted changes" in exc.value.message
        assert exc.value.hint is not None

    def test_preflight_raises_on_existing_local_tag(self, git_repo_factory):
        repo = git_repo_factory(existing_tags=["v1.2.0"])
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        with pytest.raises(TaggingRefusal) as exc:
            tagger.preflight(plans, remote=None)
        assert exc.value.code == REFUSAL_TAG_EXISTS
        assert "v1.2.0" in exc.value.message

    def test_preflight_raises_on_existing_remote_tag(self, git_repo_factory):
        repo = git_repo_factory(with_remote=True, remote_tags=["v1.2.0"])
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        with pytest.raises(TaggingRefusal) as exc:
            tagger.preflight(plans, remote="origin")
        assert exc.value.code == REFUSAL_TAG_EXISTS
        assert "origin" in exc.value.message

    def test_preflight_raises_on_missing_remote(self, git_repo_factory):
        repo = git_repo_factory(with_remote=False)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        with pytest.raises(TaggingRefusal) as exc:
            tagger.preflight(plans, remote="origin")
        assert exc.value.code == REFUSAL_NO_REMOTE

    def test_preflight_passes_when_remote_unreachable(self, git_repo_factory, run_git_cmd):
        """If ls-remote fails (e.g. network), skip remote-tag check rather than refuse."""
        repo = git_repo_factory()
        # Add a remote pointing at a non-existent local path.
        bogus = repo.parent / "does-not-exist.git"
        run_git_cmd(["remote", "add", "origin", str(bogus)], repo)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        # Should not raise: ls-remote returns non-zero, we degrade gracefully.
        tagger.preflight(plans, remote="origin")

    def test_preflight_raises_on_manifest_tag_version_mismatch(self, git_repo_factory):
        """A lockstep plan derived from None version must refuse."""
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        with pytest.raises(TaggingRefusal) as exc:
            tagger.plan_tags(strategy="lockstep", marketplace_version="", packages=[])
        assert exc.value.code == REFUSAL_VERSION_MISMATCH


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_is_no_op_in_dry_run(self, git_repo_factory, run_git_cmd):
        repo = git_repo_factory()
        logger = MagicMock()
        tagger = GitTagger(repo, dry_run=True, logger=logger)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        names = tagger.create(plans)
        assert names == ["v1.2.0"]
        # No tag actually created.
        out = run_git_cmd(["tag", "--list"], repo).stdout
        assert "v1.2.0" not in out
        # Dry-run notice emitted.
        logger.dry_run_notice.assert_called()

    def test_create_invokes_git_tag_minus_a_minus_m(self, git_repo_factory, run_git_cmd):
        repo = git_repo_factory()
        logger = MagicMock()
        tagger = GitTagger(repo, logger=logger)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        names = tagger.create(plans)
        assert names == ["v1.2.0"]
        # The tag exists and is annotated.
        kinds = run_git_cmd(["cat-file", "-t", "v1.2.0"], repo).stdout.strip()
        assert kinds == "tag"  # annotated, not "commit"
        msg = run_git_cmd(["tag", "-l", "--format=%(contents:subject)", "v1.2.0"], repo)
        assert "Release v1.2.0" in msg.stdout
        logger.success.assert_called()

    def test_create_propagates_failure_as_refusal(self, git_repo_factory):
        repo = git_repo_factory()
        tagger = GitTagger(repo)
        # Plan a tag with the same name as an existing tag we will create
        # behind tagger's back to force a collision.
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        # Sneakily create the tag.
        subprocess.run(["git", "tag", "v1.2.0"], cwd=str(repo), check=True)
        with pytest.raises(TaggingRefusal) as exc:
            tagger.create(plans)
        assert exc.value.code == REFUSAL_GIT_FAILURE


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


class TestPush:
    def test_push_is_no_op_in_dry_run(self, git_repo_factory, run_git_cmd):
        repo = git_repo_factory(with_remote=True)
        logger = MagicMock()
        tagger = GitTagger(repo, dry_run=True, logger=logger)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        # Create locally so we have a candidate (dry-run: nothing happens)
        names = tagger.create(plans)
        result = tagger.push(names, remote="origin")
        assert result == ["v1.2.0"]
        # Verify nothing pushed to the bare remote.
        remote_refs = run_git_cmd(["ls-remote", "--tags", "origin"], repo).stdout
        assert "v1.2.0" not in remote_refs
        logger.dry_run_notice.assert_called()

    def test_push_invokes_git_push_with_explicit_tag_refs_not_minus_minus_tags(
        self, git_repo_factory
    ):
        """REGRESSION TRAP: push must NEVER use 'git push --tags'.

        Locks in the explicit ``refs/tags/<name>:refs/tags/<name>`` form
        so the command can never silently force-push every local tag.
        """
        repo = git_repo_factory(with_remote=True)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(
            strategy="per_package",
            marketplace_version=None,
            packages=[
                {"name": "alpha", "version": "1.0.0"},
                {"name": "beta", "version": "2.0.0"},
            ],
        )
        tagger.create(plans)
        captured_calls: list[list[str]] = []

        from apm_cli.release import git_tagger as gt_module

        real_run = gt_module.run_git

        def spy(args, **kwargs):
            captured_calls.append(list(args))
            return real_run(args, **kwargs)

        with patch.object(gt_module, "run_git", side_effect=spy):
            tagger.push(["alpha-v1.0.0", "beta-v2.0.0"], remote="origin")

        push_calls = [c for c in captured_calls if c and c[0] == "push"]
        assert len(push_calls) == 1
        assert "--tags" not in push_calls[0]
        # Explicit refspecs for both tags, both directions.
        joined = " ".join(push_calls[0])
        assert "refs/tags/alpha-v1.0.0:refs/tags/alpha-v1.0.0" in joined
        assert "refs/tags/beta-v2.0.0:refs/tags/beta-v2.0.0" in joined

    def test_push_actually_lands_tag_on_remote(self, git_repo_factory, run_git_cmd):
        repo = git_repo_factory(with_remote=True)
        tagger = GitTagger(repo)
        plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.2.0", packages=[])
        tagger.create(plans)
        tagger.push(["v1.2.0"], remote="origin")
        remote_refs = run_git_cmd(["ls-remote", "--tags", "origin"], repo).stdout
        assert "refs/tags/v1.2.0" in remote_refs

    def test_push_empty_list_is_no_op(self, git_repo_factory):
        repo = git_repo_factory(with_remote=True)
        tagger = GitTagger(repo)
        result = tagger.push([], remote="origin")
        assert result == []


# ---------------------------------------------------------------------------
# Subprocess failure -> token-scrubbed propagation
# ---------------------------------------------------------------------------


def test_subprocess_failure_propagates_with_token_scrubbed_stderr(git_repo_factory):
    repo = git_repo_factory()
    tagger = GitTagger(repo)

    # Fake a push failure whose stderr contains an auth token in a URL.
    fake_result = subprocess.CompletedProcess(
        args=["git", "push", "origin"],
        returncode=128,
        stdout="",
        stderr="fatal: unable to access 'https://abcdef123456@example.com/repo': denied",
    )

    from apm_cli.release import git_tagger as gt_module

    with patch.object(gt_module, "run_git", return_value=fake_result):
        with pytest.raises(TaggingRefusal) as exc:
            tagger.push(["v1.0.0"], remote="origin")

    assert exc.value.code == REFUSAL_GIT_FAILURE
    # Token must be redacted.
    assert "abcdef123456" not in exc.value.message
    assert "***" in exc.value.message


# ---------------------------------------------------------------------------
# Misc: tag names with slashes, head detection
# ---------------------------------------------------------------------------


def test_plan_tags_allows_slash_in_tag_name(git_repo_factory):
    repo = git_repo_factory()
    tagger = GitTagger(repo)
    plans = tagger.plan_tags(
        strategy="tag_pattern",
        marketplace_version=None,
        packages=[{"name": "alpha", "version": "1.0.0", "tag_pattern": "release/{version}"}],
    )
    assert plans[0].name == "release/1.0.0"


def test_log_levels_fall_back_to_info(git_repo_factory):
    """Loggers missing a level still receive the message via ``info``."""
    repo = git_repo_factory()

    class InfoOnlyLogger:
        def __init__(self):
            self.messages: list[str] = []

        def info(self, msg):
            self.messages.append(msg)

    logger = InfoOnlyLogger()
    tagger = GitTagger(repo, dry_run=True, logger=logger)
    plans = tagger.plan_tags(strategy="lockstep", marketplace_version="1.0.0", packages=[])
    tagger.create(plans)
    assert any("Would create tag" in m for m in logger.messages)
