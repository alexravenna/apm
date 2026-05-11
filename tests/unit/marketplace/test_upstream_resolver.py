"""Unit tests for ``apm_cli.marketplace.upstream_resolver``.

Covers the three core invariants:
- Atomic-fetch (each upstream fetched once even with many packages).
- Repo-rename guard (canonical full_name mismatch fails closed).
- Precedence ladder for ref resolution.

Strict-parser rejections are also routed through the resolver into
the diagnostic stream as build errors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.marketplace.upstream_cache import UpstreamCache
from apm_cli.marketplace.upstream_resolver import (
    RepoRenameError,
    ResolvedUpstreamPackage,
    UpstreamResolutionError,
    UpstreamResolver,
)
from apm_cli.marketplace.yml_schema import Upstream, UpstreamPackageEntry

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_PLUGIN = "c" * 40


def make_upstream(
    *,
    alias: str = "gitnexus",
    repo: str = "abhigyanpatwari/GitNexus",
    ref: str | None = SHA_A,
    branch: str = "main",
    allow_head: bool = False,
    path: str = ".claude-plugin/marketplace.json",
    host: str = "github.com",
) -> Upstream:
    return Upstream(
        alias=alias,
        repo=repo,
        path=path,
        ref=ref,
        branch=branch,
        host=host,
        allow_head=allow_head,
    )


def make_entry(
    *,
    name: str = "gitnexus",
    upstream_alias: str = "gitnexus",
    plugin: str | None = None,
    ref: str | None = None,
    version: str | None = None,
    allow_head: bool = False,
    tag_pattern: str | None = None,
    include_prerelease: bool = False,
) -> UpstreamPackageEntry:
    return UpstreamPackageEntry(
        name=name,
        upstream_alias=upstream_alias,
        plugin=plugin,
        ref=ref,
        version=version,
        allow_head=allow_head,
        tag_pattern=tag_pattern,
        include_prerelease=include_prerelease,
    )


def gitnexus_manifest() -> dict:
    """A minimal upstream marketplace.json shaped like GitNexus."""
    return {
        "name": "gitnexus-marketplace",
        "owner": {"name": "abhigyanpatwari"},
        "plugins": [
            {
                "name": "gitnexus",
                "source": {
                    "type": "git-subdir",
                    "repo": "abhigyanpatwari/GitNexus",
                    "path": "gitnexus-claude-plugin",
                },
                "description": "GitNexus plugin",
            }
        ],
    }


def manifest_with_pinned_plugin(*, plugin_sha: str = SHA_PLUGIN) -> dict:
    return {
        "name": "marketplace",
        "owner": {"name": "abhigyanpatwari"},
        "plugins": [
            {
                "name": "gitnexus",
                "source": {
                    "type": "git-subdir",
                    "repo": "abhigyanpatwari/GitNexus",
                    "path": "gitnexus-claude-plugin",
                    "sha": plugin_sha,
                },
            }
        ],
    }


def ref_to_sha_static(value: str) -> RefToSha:
    """Return a ref_to_sha callable that always returns *value*."""

    def _resolver(
        host: str,
        owner: str,
        repo: str,
        ref: str,
        *,
        allow_head: bool = False,
    ) -> str:
        return value

    return _resolver


# Type alias for clarity in test code
RefToSha = object


# ---------------------------------------------------------------------------
# Atomic-fetch invariant
# ---------------------------------------------------------------------------


class TestAtomicFetch:
    def test_single_upstream_fetched_once_for_many_packages(self, tmp_path: Path):
        """Multiple packages on the same upstream must trigger only one fetch."""
        cache = UpstreamCache(
            base_dir=tmp_path,
            fetch_callback=MagicMock(return_value=gitnexus_manifest()),
        )
        # Track how many times the fetch_callback was called.
        fetch_callback = cache._fetch_callback  # MagicMock
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        entries = [
            make_entry(name="acme-gitnexus", plugin="gitnexus"),
            make_entry(name="acme-gitnexus-2", plugin="gitnexus"),
            make_entry(name="acme-gitnexus-3", plugin="gitnexus"),
        ]
        resolved, _ = resolver.resolve_all(entries)
        # All three resolved successfully.
        assert len(resolved) == 3
        # And the upstream JSON was fetched exactly once.
        assert fetch_callback.call_count == 1

    def test_separate_upstreams_each_fetched_once(self, tmp_path: Path):
        """Distinct upstreams each fetch independently."""
        manifest_a = gitnexus_manifest()
        manifest_b = {
            "name": "other-marketplace",
            "owner": {"name": "other-owner"},
            "plugins": [
                {
                    "name": "other",
                    "source": {
                        "type": "git-subdir",
                        "repo": "other-owner/other-repo",
                        "path": "plugin",
                    },
                }
            ],
        }

        # Fetch returns different content based on the cache key host/owner
        def fetch(key, _auth):
            if key.repo == "GitNexus":
                return manifest_a
            return manifest_b

        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=fetch)
        upstreams = {
            "gitnexus": make_upstream(),
            "other": make_upstream(alias="other", repo="other-owner/other-repo", ref=SHA_B),
        }
        resolver = UpstreamResolver(
            upstreams=upstreams,
            cache=cache,
            ref_to_sha=(
                lambda host, owner, repo, ref, *, allow_head=False: (
                    SHA_A if repo == "GitNexus" else SHA_B
                )
            ),
        )
        entries = [
            make_entry(name="acme-gitnexus", upstream_alias="gitnexus", plugin="gitnexus"),
            make_entry(name="acme-other", upstream_alias="other", plugin="other"),
        ]
        resolved, _ = resolver.resolve_all(entries)
        assert len(resolved) == 2


# ---------------------------------------------------------------------------
# Repo-rename guard
# ---------------------------------------------------------------------------


class TestRepoRenameGuard:
    def test_rename_detected_raises(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream(repo="abhigyanpatwari/GitNexus")
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
            canonical_full_name=lambda h, o, r: "evil-actor/GitNexus",
        )
        entry = make_entry()
        with pytest.raises(RepoRenameError, match="repo identity mismatch"):
            resolver.resolve_package(entry)

    def test_rename_is_case_insensitive_match(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream(repo="abhigyanpatwari/GitNexus")
        # GitHub may report different casing; allow it.
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
            canonical_full_name=lambda h, o, r: "ABHIGYANPATWARI/gitnexus",
        )
        # Should NOT raise.
        result = resolver.resolve_package(make_entry())
        assert isinstance(result, ResolvedUpstreamPackage)

    def test_canonical_resolver_failure_fails_closed(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()

        def boom(h, o, r):
            raise RuntimeError("api 503")

        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
            canonical_full_name=boom,
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry())
        assert exc_info.value.code == "canonical-name-unavailable"

    def test_canonical_resolver_returning_empty_skips_check(self, tmp_path: Path):
        """Empty canonical means 'unknown'; allowed for offline rebuilds."""
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
            canonical_full_name=lambda h, o, r: "",
        )
        result = resolver.resolve_package(make_entry())
        assert result.upstream_canonical_full_name == "abhigyanpatwari/GitNexus"


# ---------------------------------------------------------------------------
# Precedence ladder
# ---------------------------------------------------------------------------


class TestPrecedenceLadder:
    def test_curator_ref_wins(self, tmp_path: Path):
        cache = UpstreamCache(
            base_dir=tmp_path, fetch_callback=lambda k, a: manifest_with_pinned_plugin()
        )
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        result = resolver.resolve_package(make_entry(ref=SHA_B))
        assert result.plugin_ref == SHA_B
        assert result.plugin_sha == SHA_B
        assert result.pin_source == "curator-ref"

    def test_curator_version_uses_resolver(self, tmp_path: Path):
        cache = UpstreamCache(
            base_dir=tmp_path, fetch_callback=lambda k, a: manifest_with_pinned_plugin()
        )
        upstream = make_upstream()

        def version_resolver(host, owner, repo, range_, *, tag_pattern, include_prerelease):
            assert range_ == "^1.0.0"
            return "v1.2.3", SHA_B

        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
            version_range_resolver=version_resolver,
        )
        result = resolver.resolve_package(make_entry(version="^1.0.0"))
        assert result.plugin_ref == "v1.2.3"
        assert result.plugin_sha == SHA_B
        assert result.pin_source == "curator-version"

    def test_curator_version_without_resolver_raises(self, tmp_path: Path):
        cache = UpstreamCache(
            base_dir=tmp_path, fetch_callback=lambda k, a: manifest_with_pinned_plugin()
        )
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry(version="^1.0.0"))
        assert exc_info.value.code == "version-resolver-missing"

    def test_upstream_pinned_sha(self, tmp_path: Path):
        cache = UpstreamCache(
            base_dir=tmp_path,
            fetch_callback=lambda k, a: manifest_with_pinned_plugin(plugin_sha=SHA_PLUGIN),
        )
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        result = resolver.resolve_package(make_entry())
        assert result.plugin_sha == SHA_PLUGIN
        assert result.pin_source == "upstream-pin"

    def test_same_repo_fallback_uses_manifest_sha(self, tmp_path: Path):
        """GitNexus shape: plugin lives in same repo as marketplace, no plugin pin."""
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream(ref=SHA_A)
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        result = resolver.resolve_package(make_entry())
        assert result.plugin_sha == SHA_A
        assert result.pin_source == "upstream-registration-ref"
        assert result.upstream_manifest_sha == SHA_A

    def test_unpinned_no_fallback_raises(self, tmp_path: Path):
        """Plugin in different repo, no curator pin, no plugin pin -> fail."""
        manifest = {
            "name": "marketplace",
            "owner": {"name": "abhigyanpatwari"},
            "plugins": [
                {
                    "name": "gitnexus",
                    "source": {
                        "type": "github",
                        "repo": "different-owner/different-repo",
                    },
                }
            ],
        }
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: manifest)
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry())
        assert exc_info.value.code == "package-unpinned"

    def test_allow_head_emits_warning_returns_none_ref(self, tmp_path: Path):
        manifest = {
            "name": "marketplace",
            "owner": {"name": "abhigyanpatwari"},
            "plugins": [
                {
                    "name": "gitnexus",
                    "source": {
                        "type": "github",
                        "repo": "different-owner/different-repo",
                    },
                }
            ],
        }
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: manifest)
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        result = resolver.resolve_package(make_entry(allow_head=True))
        assert result.plugin_ref is None
        assert result.plugin_sha is None
        assert result.pin_source == "branch-head"
        codes = [d.code for d in resolver.diagnostics]
        assert "package-tracks-upstream-head" in codes


# ---------------------------------------------------------------------------
# Upstream-level errors and diagnostics
# ---------------------------------------------------------------------------


class TestUpstreamLevelErrors:
    def test_unknown_alias(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        resolver = UpstreamResolver(
            upstreams={},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry(upstream_alias="nope"))
        assert exc_info.value.code == "unknown-upstream-alias"

    def test_missing_plugin_in_upstream(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry(plugin="does-not-exist"))
        assert exc_info.value.code == "missing-plugin"

    def test_unpinned_upstream_without_allow_head(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream(ref=None, allow_head=False)
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry())
        assert exc_info.value.code == "upstream-unpinned"

    def test_unpinned_upstream_with_allow_head_emits_warning(self, tmp_path: Path):
        cache = UpstreamCache(
            base_dir=tmp_path,
            fetch_callback=lambda k, a: manifest_with_pinned_plugin(),
        )
        upstream = make_upstream(ref=None, allow_head=True)
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        result = resolver.resolve_package(make_entry())
        assert result.plugin_sha == SHA_PLUGIN
        codes = [d.code for d in resolver.diagnostics]
        assert "upstream-tracks-head" in codes

    def test_strict_rejection_surfaces_as_error_diagnostic(self, tmp_path: Path):
        """Strict-parser rejections become resolver-level error diagnostics."""
        manifest = {
            "name": "marketplace",
            "owner": {"name": "abhigyanpatwari"},
            "plugins": [
                {
                    "name": "gitnexus",
                    "source": {
                        "type": "git-subdir",
                        "repo": "abhigyanpatwari/GitNexus",
                        "path": "gitnexus-claude-plugin",
                    },
                },
                {
                    "name": "broken",
                    "source": {"type": "npm"},  # unsupported-source-type
                },
            ],
        }
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: manifest)
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        # Resolving the well-formed entry succeeds; the rejection is
        # still surfaced through the diagnostic stream.
        result = resolver.resolve_package(make_entry())
        assert result is not None
        codes = [d.code for d in resolver.diagnostics]
        assert any(c.startswith("upstream-rejection:") for c in codes)
        assert any(d.level == "error" for d in resolver.diagnostics)


# ---------------------------------------------------------------------------
# resolve_all error aggregation
# ---------------------------------------------------------------------------


class TestResolveAll:
    def test_continues_on_per_package_failure(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=ref_to_sha_static(SHA_A),
        )
        entries = [
            make_entry(name="ok", plugin="gitnexus"),
            make_entry(name="bad", plugin="does-not-exist"),
            make_entry(name="ok2", plugin="gitnexus"),
        ]
        resolved, diagnostics = resolver.resolve_all(entries)
        assert len(resolved) == 2
        # The failure becomes a diagnostic, not an exception.
        assert any(d.code == "missing-plugin" for d in diagnostics)


# ---------------------------------------------------------------------------
# Ref-to-SHA validation
# ---------------------------------------------------------------------------


class TestRefToShaValidation:
    def test_invalid_sha_returned_by_callback_raises(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()
        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            # Returns a short string, not a 40-char SHA.
            ref_to_sha=ref_to_sha_static("abc123"),
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry())
        assert exc_info.value.code == "invalid-sha"

    def test_ref_resolution_failure_reports_named_error(self, tmp_path: Path):
        cache = UpstreamCache(base_dir=tmp_path, fetch_callback=lambda k, a: gitnexus_manifest())
        upstream = make_upstream()

        def boom(host, owner, repo, ref):
            raise RuntimeError("network down")

        resolver = UpstreamResolver(
            upstreams={upstream.alias: upstream},
            cache=cache,
            ref_to_sha=boom,
        )
        with pytest.raises(UpstreamResolutionError) as exc_info:
            resolver.resolve_package(make_entry())
        assert exc_info.value.code == "ref-resolution-failed"
