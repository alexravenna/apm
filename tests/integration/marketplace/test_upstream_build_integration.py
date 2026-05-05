"""Integration-with-fixtures tests for the upstream build pipeline.

These tests exercise the full MarketplaceBuilder end-to-end with a
pre-populated UpstreamCache (no network calls) to validate:

1. A mixed direct+upstream apm.yml produces the expected
   marketplace.json and apm.lock.yaml entries.
2. Rebuilding with BuildOptions(offline=True) from the populated lockfile
   produces byte-identical output (reproducibility invariant).

No real git ls-remote or HTTP calls are made.  Both the direct package
and the upstream plugin use pinned SHA refs (no tag resolution needed);
the upstream manifest is served from a fixture dict injected into
UpstreamCache before the build.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.upstream_cache import UpstreamCache, UpstreamCacheKey, compute_cache_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SHA_MANIFEST = "a" * 40
_SHA_PLUGIN = "b" * 40
_SHA_DIRECT = "c" * 40

_UPSTREAM_MANIFEST: dict = {
    "name": "fixture-upstream",
    "owner": {"name": "fixture-org"},
    "plugins": [
        {
            "name": "upstream-plugin",
            "description": "A plugin from the upstream fixture.",
            "source": {
                "type": "github",
                "repo": "fixture-org/upstream-plugin",
                "ref": "main",
                "sha": _SHA_PLUGIN,
            },
        }
    ],
}

_MIXED_YML = f"""\
name: fixture-marketplace
description: Integration fixture with direct + upstream packages
version: 1.0.0
marketplace:
  owner:
    name: Fixture Org
    email: fixture@example.com
    url: https://example.com
  metadata:
    pluginRoot: plugins
    category: testing
  upstreams:
    - alias: fixture-upstream
      repo: fixture-org/fixture-upstream
      path: .apm/marketplace.json
      ref: {_SHA_MANIFEST}
  packages:
    - name: direct-pkg
      description: A direct package.
      source: fixture-org/direct-pkg
      ref: {_SHA_DIRECT}
    - name: upstream-pkg
      description: Curated from upstream.
      upstream: fixture-upstream
      plugin: upstream-plugin
"""


def _write_yml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "apm.yml"
    p.write_text(content, encoding="utf-8")
    return p


def _pre_populate_cache(cache_dir: Path, manifest: dict) -> None:
    """Write the fixture manifest into UpstreamCache so no network fetch
    is needed during the build."""
    cache = UpstreamCache(base_dir=cache_dir, fetch_callback=lambda k, a: manifest)
    key: UpstreamCacheKey = compute_cache_key(
        host="github.com",
        owner="fixture-org",
        repo="fixture-upstream",
        sha=_SHA_MANIFEST,
        path=".apm/marketplace.json",
    )
    cache.put(key, manifest)


def _make_builder(yml_path: Path, cache_dir: Path, *, offline: bool = False) -> MarketplaceBuilder:
    """Build a MarketplaceBuilder with the upstream cache injected."""
    opts = BuildOptions(dry_run=False, offline=offline)
    builder = MarketplaceBuilder(yml_path, options=opts)
    # Inject the pre-populated cache so upstream resolution never hits the
    # network.  We replace the factory's ``build()`` return value.
    upstream_manifest = _UPSTREAM_MANIFEST

    original_build_resolver = builder._build_upstream_resolver

    def _patched_build_resolver(yml):
        resolver = original_build_resolver(yml)
        # Swap the cache to one backed by our fixture directory.
        resolver._cache = UpstreamCache(
            base_dir=cache_dir,
            fetch_callback=lambda k, a: upstream_manifest,
        )
        return resolver

    builder._build_upstream_resolver = _patched_build_resolver
    return builder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpstreamBuildIntegration:
    """Full-pipeline integration tests for upstream resolution."""

    def test_mixed_build_emits_both_entries(self, tmp_path: Path) -> None:
        """A mixed direct+upstream apm.yml must produce a marketplace.json
        that contains BOTH plugin entries, with no ``metadata.apm.*`` keys
        injected."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _pre_populate_cache(cache_dir, _UPSTREAM_MANIFEST)
        yml_path = _write_yml(tmp_path, _MIXED_YML)

        builder = _make_builder(yml_path, cache_dir)
        report = builder.build()

        assert not report.errors, f"Build had errors: {report.errors}"

        out_path = tmp_path / ".claude-plugin" / "marketplace.json"
        assert out_path.exists(), "marketplace.json was not produced"

        marketplace = json.loads(out_path.read_text(encoding="utf-8"))
        plugins = marketplace.get("plugins", [])
        plugin_names = {p["name"] for p in plugins}

        assert "direct-pkg" in plugin_names, f"direct-pkg missing from {plugin_names}"
        assert "upstream-pkg" in plugin_names, f"upstream-pkg missing from {plugin_names}"

        # No APM-specific metadata keys in the emitted JSON (pass-through
        # contract: emitted marketplace.json must be Anthropic-conformant).
        raw_text = out_path.read_text(encoding="utf-8")
        assert "apm" not in raw_text.split('"metadata"', 1)[-1].split('"plugins"')[0], (
            "APM-specific metadata keys found in emitted marketplace.json"
        )

    def test_lockfile_records_upstream_provenance(self, tmp_path: Path) -> None:
        """After a successful build the lockfile must record upstream
        provenance: manifest_sha and the resolved plugin sha."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _pre_populate_cache(cache_dir, _UPSTREAM_MANIFEST)
        yml_path = _write_yml(tmp_path, _MIXED_YML)

        builder = _make_builder(yml_path, cache_dir)
        builder.build()

        lock_path = tmp_path / "apm.lock.yaml"
        assert lock_path.exists(), "apm.lock.yaml was not produced"

        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
        upstreams = lock.get("upstreams", {})
        assert "fixture-upstream" in upstreams, (
            f"upstream alias not in lockfile. Keys: {list(upstreams)}"
        )
        alias_entry = upstreams["fixture-upstream"]
        assert alias_entry.get("manifest_sha") == _SHA_MANIFEST, (
            f"manifest_sha mismatch: {alias_entry.get('manifest_sha')}"
        )
        plugins_lock = alias_entry.get("plugins", {})
        assert "upstream-plugin" in plugins_lock, (
            f"upstream-plugin not in lockfile plugins: {list(plugins_lock)}"
        )
        assert plugins_lock["upstream-plugin"].get("resolved_sha") == _SHA_PLUGIN, (
            f"resolved_sha mismatch: {plugins_lock['upstream-plugin'].get('resolved_sha')}"
        )

    def test_offline_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        """Rebuilding with offline=True after an initial build must produce
        byte-identical marketplace.json output (reproducibility invariant).

        The initial build populates apm.lock.yaml with pinned SHAs.  The
        offline rebuild reads those SHAs from the lock instead of calling
        the network, and must produce the same emitted JSON."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        _pre_populate_cache(cache_dir, _UPSTREAM_MANIFEST)
        yml_path = _write_yml(tmp_path, _MIXED_YML)

        # ---- first build -------------------------------------------------------
        builder = _make_builder(yml_path, cache_dir)
        report1 = builder.build()

        assert not report1.errors, f"First build had errors: {report1.errors}"
        out_path = tmp_path / ".claude-plugin" / "marketplace.json"
        first_output = out_path.read_bytes()

        # ---- offline rebuild ---------------------------------------------------
        # Both direct (pinned SHA) and upstream (locked manifest SHA) bypass
        # the network entirely in offline mode.
        builder2 = _make_builder(yml_path, cache_dir, offline=True)
        report2 = builder2.build()
        assert not report2.errors, f"Offline rebuild had errors: {report2.errors}"

        second_output = out_path.read_bytes()
        assert first_output == second_output, (
            "Offline rebuild produced different marketplace.json output.\n"
            f"First:  {first_output[:200]}\n"
            f"Second: {second_output[:200]}"
        )
