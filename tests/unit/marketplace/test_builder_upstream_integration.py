"""Integration tests for upstream-sourced packages in MarketplaceBuilder.

These tests exercise the full build pipeline end-to-end with both direct
and upstream-sourced ``packages[]`` entries. The upstream cache is wired
to an in-memory ``fetch_callback`` so no network access is required.

Coverage targets:

- Mixed-shape build: one direct + one upstream package both emit, with
  upstream entries appearing after direct entries (v1 emit order).
- Upstream-only build emits a valid marketplace.json that round-trips
  through ``parse_marketplace_json``.
- No ``metadata.apm.*`` keys are injected into the output.
- Curator overrides on description/version/tags win over upstream
  values; ``author``/``license``/``repository``/``homepage`` are
  curator-only (no upstream fallback).
- Upstream resolution errors raise ``BuildError`` BEFORE writing.
- Round-trip parse via ``parse_marketplace_json`` succeeds for every
  emitted plugin.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
)
from apm_cli.marketplace.errors import BuildError
from apm_cli.marketplace.models import parse_marketplace_json
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.upstream_cache import UpstreamCache
from apm_cli.marketplace.upstream_resolver import UpstreamResolver

SHA_DIRECT = "a" * 40
SHA_UPSTREAM_MANIFEST = "b" * 40
SHA_UPSTREAM_PLUGIN = "c" * 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockRefResolver:
    """In-process mock for RefResolver -- no subprocess calls."""

    def __init__(self, refs_by_remote: dict[str, list[RemoteRef]] | None = None) -> None:
        self._refs = refs_by_remote or {}

    def list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
        if owner_repo not in self._refs:
            from apm_cli.marketplace.errors import GitLsRemoteError

            raise GitLsRemoteError(
                package="",
                summary=f"Remote '{owner_repo}' not found.",
                hint="Check the source.",
            )
        return self._refs[owner_repo]

    def close(self) -> None:
        pass


def _gitnexus_manifest() -> dict:
    """Minimal upstream marketplace.json modelled on GitNexus."""
    return {
        "name": "gitnexus-marketplace",
        "owner": {"name": "abhigyanpatwari"},
        "plugins": [
            {
                "name": "gitnexus",
                "description": "Upstream-supplied description",
                "version": "1.0.0",
                "tags": ["upstream-tag"],
                "source": {
                    "type": "git-subdir",
                    "repo": "abhigyanpatwari/GitNexus",
                    "path": "gitnexus-claude-plugin",
                    "sha": SHA_UPSTREAM_PLUGIN,
                },
            }
        ],
    }


def _write_yml(tmp_path: Path, body: str) -> Path:
    yml_path = tmp_path / "apm.yml"
    yml_path.write_text(body, encoding="utf-8")
    return yml_path


def _patch_resolver_factory(
    builder: MarketplaceBuilder,
    *,
    cache: UpstreamCache,
    ref_to_sha_value: str = SHA_UPSTREAM_MANIFEST,
) -> None:
    """Replace ``_build_upstream_resolver`` with a test-controlled factory.

    Bypasses the network-touching default helpers (``ref_to_sha`` and
    ``canonical_full_name``). The provided cache supplies the upstream
    manifest in-memory.
    """

    def _factory(yml):  # type: ignore[no-untyped-def]
        upstreams_by_alias = {u.alias: u for u in yml.upstreams}

        def _ref_to_sha(host: str, owner: str, repo: str, ref: str) -> str:
            return ref_to_sha_value

        return UpstreamResolver(
            upstreams=upstreams_by_alias,
            cache=cache,
            ref_to_sha=_ref_to_sha,
            canonical_full_name=None,
        )

    builder._build_upstream_resolver = _factory  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Mixed-shape and upstream-only builds
# ---------------------------------------------------------------------------


_MIXED_YML = """\
name: acme-marketplace
description: ACME curated marketplace
version: 0.1.0
marketplace:
  owner:
    name: ACME Corp
  upstreams:
    - alias: gitnexus
      repo: abhigyanpatwari/GitNexus
      ref: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  packages:
    - name: direct-tool
      source: acme/direct-tool
      version: "^1.0.0"
    - name: acme-gitnexus
      upstream: gitnexus
      plugin: gitnexus
      description: ACME-curated GitNexus
      tags:
        - acme
        - approved
"""


def test_mixed_shape_build_emits_both_plugins(tmp_path: Path) -> None:
    """Direct + upstream packages both emit; upstream after direct."""
    yml_path = _write_yml(tmp_path, _MIXED_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver(  # type: ignore[assignment]
        {"acme/direct-tool": [RemoteRef(name="refs/tags/v1.0.0", sha=SHA_DIRECT)]}
    )
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()

    assert report.errors == ()
    assert len(report.resolved) == 1
    assert len(report.upstream_resolved) == 1

    # Output marketplace.json has both plugins; direct first, upstream second.
    output_path = report.output_path
    import json

    doc = json.loads(output_path.read_text(encoding="utf-8"))
    plugin_names = [p["name"] for p in doc["plugins"]]
    assert plugin_names == ["direct-tool", "acme-gitnexus"]


def test_upstream_emission_has_no_apm_metadata(tmp_path: Path) -> None:
    """Hard rule: no ``metadata.apm.*`` keys in emitted marketplace.json."""
    yml_path = _write_yml(tmp_path, _MIXED_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver(  # type: ignore[assignment]
        {"acme/direct-tool": [RemoteRef(name="refs/tags/v1.0.0", sha=SHA_DIRECT)]}
    )
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    import json

    doc = json.loads(report.output_path.read_text(encoding="utf-8"))

    # No top-level apm key, no per-plugin apm metadata.
    assert "apm" not in doc
    metadata = doc.get("metadata", {})
    assert "apm" not in metadata
    for plugin in doc["plugins"]:
        assert "apm" not in plugin
        plugin_meta = plugin.get("metadata", {})
        if isinstance(plugin_meta, dict):
            assert "apm" not in plugin_meta


def test_upstream_emission_curator_override_wins_for_description_and_tags(
    tmp_path: Path,
) -> None:
    """Curator overrides win over upstream values for description/tags."""
    yml_path = _write_yml(tmp_path, _MIXED_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver(  # type: ignore[assignment]
        {"acme/direct-tool": [RemoteRef(name="refs/tags/v1.0.0", sha=SHA_DIRECT)]}
    )
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    import json

    doc = json.loads(report.output_path.read_text(encoding="utf-8"))
    upstream_plugin = next(p for p in doc["plugins"] if p["name"] == "acme-gitnexus")
    assert upstream_plugin["description"] == "ACME-curated GitNexus"
    assert upstream_plugin["tags"] == ["acme", "approved"]
    # Version was not overridden -- falls back to upstream value.
    assert upstream_plugin["version"] == "1.0.0"


def test_upstream_emission_uses_git_subdir_shape_when_subdir_present(
    tmp_path: Path,
) -> None:
    """Upstream plugin source shape matches the direct-emit contract."""
    yml_path = _write_yml(tmp_path, _MIXED_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver(  # type: ignore[assignment]
        {"acme/direct-tool": [RemoteRef(name="refs/tags/v1.0.0", sha=SHA_DIRECT)]}
    )
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    import json

    doc = json.loads(report.output_path.read_text(encoding="utf-8"))
    upstream_plugin = next(p for p in doc["plugins"] if p["name"] == "acme-gitnexus")
    source = upstream_plugin["source"]
    # Matches direct-emit: outer "source" key with inner "source"
    # discriminator + ``url`` field for git-subdir.
    assert source["source"] == "git-subdir"
    assert source["url"] == "abhigyanpatwari/GitNexus"
    assert source["path"] == "gitnexus-claude-plugin"
    assert source["sha"] == SHA_UPSTREAM_PLUGIN


def test_round_trip_via_parse_marketplace_json(tmp_path: Path) -> None:
    """Every emitted plugin survives the lenient consumer parser."""
    yml_path = _write_yml(tmp_path, _MIXED_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    builder._resolver = _MockRefResolver(  # type: ignore[assignment]
        {"acme/direct-tool": [RemoteRef(name="refs/tags/v1.0.0", sha=SHA_DIRECT)]}
    )
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    import json

    doc = json.loads(report.output_path.read_text(encoding="utf-8"))
    manifest = parse_marketplace_json(doc)
    parsed_names = sorted(p.name for p in manifest.plugins)
    assert parsed_names == ["acme-gitnexus", "direct-tool"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


_UPSTREAM_ONLY_YML = """\
name: acme-marketplace
description: ACME curated marketplace
version: 0.1.0
marketplace:
  owner:
    name: ACME Corp
  upstreams:
    - alias: gitnexus
      repo: abhigyanpatwari/GitNexus
      ref: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  packages:
    - name: acme-gitnexus
      upstream: gitnexus
      plugin: gitnexus
"""


def test_upstream_resolution_error_raises_build_error_before_writing(
    tmp_path: Path,
) -> None:
    """Unknown upstream alias must raise BuildError; no marketplace.json written."""
    yml = _UPSTREAM_ONLY_YML.replace("plugin: gitnexus", "plugin: does-not-exist")
    yml_path = _write_yml(tmp_path, yml)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True, continue_on_error=True)
    builder = MarketplaceBuilder(yml_path, options)
    _patch_resolver_factory(builder, cache=cache)

    with pytest.raises(BuildError):
        builder.build()

    # Output path must not exist -- fail-closed gate prevented write.
    output_path = tmp_path / ".claude-plugin" / "marketplace.json"
    assert not output_path.exists()


def test_upstream_only_build_emits_valid_marketplace(tmp_path: Path) -> None:
    """Build with only upstream packages produces a valid marketplace.json."""
    yml_path = _write_yml(tmp_path, _UPSTREAM_ONLY_YML)
    cache = UpstreamCache(
        base_dir=tmp_path / ".cache",
        fetch_callback=MagicMock(return_value=_gitnexus_manifest()),
    )
    options = BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, options)
    _patch_resolver_factory(builder, cache=cache)

    report = builder.build()
    assert report.errors == ()
    assert len(report.upstream_resolved) == 1

    import json

    doc = json.loads(report.output_path.read_text(encoding="utf-8"))
    assert [p["name"] for p in doc["plugins"]] == ["acme-gitnexus"]
    # Round-trip parse must succeed.
    parsed = parse_marketplace_json(doc)
    assert len(parsed.plugins) == 1
