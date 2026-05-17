"""MarketplaceBuilder -- load, resolve, compose, and write marketplace.json.

This module implements the full build pipeline:

1. **Load** -- parse ``marketplace.yml`` via ``yml_schema.load_marketplace_yml``.
2. **Resolve** -- for every package entry, call ``git ls-remote`` (via
   ``RefResolver``) and determine the concrete tag + SHA.
3. **Compose** -- produce an Anthropic-compliant ``marketplace.json`` dict
   with all APM-only fields stripped.
4. **Write** -- atomically write the JSON to disk (or skip on dry-run)
   and produce a ``BuildReport`` with diff statistics.

Hard rule: the output ``marketplace.json`` conforms byte-for-byte to
Anthropic's schema.  No APM-specific keys, no extensions, no renamed
fields.  ``packages`` in yml becomes ``plugins`` in json.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    RefNotFoundError,
)
from ..ref_resolver import RefResolver
from ..semver import SemVer, parse_semver, satisfies_range
from ..tag_pattern import build_tag_regex
from ..yml_schema import MarketplaceYml, PackageEntry
from .class_ import ResolvedPackage, ResolveResult, _strip_ref_prefix

logger = logging.getLogger(__name__)
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _resolve_entry(self, entry: PackageEntry) -> ResolvedPackage:
    """Resolve a single package entry to a concrete tag + SHA."""
    # Local-path packages skip git resolution entirely.
    if entry.is_local:
        return ResolvedPackage(
            name=entry.name,
            source_repo="",
            subdir=entry.source,
            ref="",
            sha="",
            requested_version=entry.version,
            tags=tuple(entry.tags),
            is_prerelease=False,
        )
    yml = self._load_yml()
    resolver = self._get_resolver()
    owner_repo = entry.source

    if entry.ref is not None:
        return self._resolve_explicit_ref(entry, resolver, owner_repo)
    # version range resolution
    return self._resolve_version_range(entry, resolver, owner_repo, yml)


def _resolve_explicit_ref(
    self,
    entry: PackageEntry,
    resolver: RefResolver,
    owner_repo: str,
) -> ResolvedPackage:
    """Resolve an entry with an explicit ``ref:`` field."""
    ref_text = entry.ref
    assert ref_text is not None  # noqa: S101

    # If it looks like a 40-char SHA, accept it directly
    if _SHA40_RE.match(ref_text):
        sv = parse_semver(ref_text.lstrip("vV"))
        return ResolvedPackage(
            name=entry.name,
            source_repo=owner_repo,
            subdir=entry.subdir,
            ref=ref_text,
            sha=ref_text,
            requested_version=entry.version,
            tags=entry.tags,
            is_prerelease=sv.is_prerelease if sv else False,
        )

    refs = resolver.list_remote_refs(owner_repo)

    # Try as tag first (only check tag refs)
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = _strip_ref_prefix(remote_ref.name)
        if tag_name == ref_text:
            sv = parse_semver(tag_name.lstrip("vV"))
            return ResolvedPackage(
                name=entry.name,
                source_repo=owner_repo,
                subdir=entry.subdir,
                ref=tag_name,
                sha=remote_ref.sha,
                requested_version=entry.version,
                tags=entry.tags,
                is_prerelease=sv.is_prerelease if sv else False,
            )

    # Try as full refname
    for remote_ref in refs:
        if remote_ref.name == ref_text:
            short = _strip_ref_prefix(remote_ref.name)
            is_branch = remote_ref.name.startswith("refs/heads/")
            if is_branch and not self._options.allow_head:
                raise HeadNotAllowedError(entry.name, short)
            sv = parse_semver(short.lstrip("vV"))
            return ResolvedPackage(
                name=entry.name,
                source_repo=owner_repo,
                subdir=entry.subdir,
                ref=short,
                sha=remote_ref.sha,
                requested_version=entry.version,
                tags=entry.tags,
                is_prerelease=sv.is_prerelease if sv else False,
            )

    # Try as branch name
    for remote_ref in refs:
        if remote_ref.name == f"refs/heads/{ref_text}":
            if not self._options.allow_head:
                raise HeadNotAllowedError(entry.name, ref_text)
            return ResolvedPackage(
                name=entry.name,
                source_repo=owner_repo,
                subdir=entry.subdir,
                ref=ref_text,
                sha=remote_ref.sha,
                requested_version=entry.version,
                tags=entry.tags,
                is_prerelease=False,
            )

    # HEAD special case
    if ref_text.upper() == "HEAD":
        if not self._options.allow_head:
            raise HeadNotAllowedError(entry.name, "HEAD")

    raise RefNotFoundError(entry.name, ref_text, owner_repo)


def _resolve_version_range(
    self,
    entry: PackageEntry,
    resolver: RefResolver,
    owner_repo: str,
    yml: MarketplaceYml,
) -> ResolvedPackage:
    """Resolve an entry using its ``version:`` semver range."""
    version_range = entry.version
    assert version_range is not None  # noqa: S101

    # Determine tag pattern: entry > build > default
    pattern = entry.tag_pattern or yml.build.tag_pattern

    tag_rx = build_tag_regex(pattern)
    refs = resolver.list_remote_refs(owner_repo)

    # Filter tags matching the pattern and extract versions
    candidates: list[tuple[SemVer, str, str]] = []  # (semver, tag_name, sha)
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/") :]
        m = tag_rx.match(tag_name)
        if not m:
            continue
        version_str = m.group("version")
        sv = parse_semver(version_str)
        if sv is None:
            continue

        # Prerelease filter
        include_pre = entry.include_prerelease or self._options.include_prerelease
        if sv.is_prerelease and not include_pre:
            continue

        # Range filter
        if satisfies_range(sv, version_range):
            candidates.append((sv, tag_name, remote_ref.sha))

    if not candidates:
        raise NoMatchingVersionError(
            entry.name,
            version_range,
            detail=f"pattern='{pattern}', remote='{owner_repo}'",
        )

    # Pick highest
    candidates.sort(key=lambda c: c[0], reverse=True)
    best_sv, best_tag, best_sha = candidates[0]

    return ResolvedPackage(
        name=entry.name,
        source_repo=owner_repo,
        subdir=entry.subdir,
        ref=best_tag,
        sha=best_sha,
        requested_version=version_range,
        tags=entry.tags,
        is_prerelease=best_sv.is_prerelease,
    )


def resolve(self) -> ResolveResult:
    """Resolve every entry concurrently.

    Returns
    -------
    ResolveResult
        Contains resolved entries and any errors encountered.

    Raises
    ------
    BuildError
        On any resolution failure (unless ``continue_on_error``).
    """
    yml = self._load_yml()
    entries = yml.packages
    if not entries:
        return ResolveResult(entries=(), errors=())

    results: dict[int, ResolvedPackage] = {}
    errors: list[tuple[str, str]] = []

    # Eagerly resolve auth + create the shared RefResolver before
    # spawning workers -- avoids a race on _ensure_auth() and
    # matches the pattern used in _prefetch_metadata().
    self._get_resolver()

    with ThreadPoolExecutor(max_workers=min(self._options.concurrency, len(entries))) as pool:
        future_to_index = {
            pool.submit(self._resolve_entry, entry): idx for idx, entry in enumerate(entries)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            entry = entries[idx]
            try:
                resolved = future.result(timeout=self._options.timeout_seconds)
                results[idx] = resolved
            except BuildError as exc:
                if self._options.continue_on_error:
                    errors.append((entry.name, str(exc)))
                else:
                    raise
            except Exception as exc:
                logger.debug("Unexpected error resolving '%s'", entry.name, exc_info=True)
                if self._options.continue_on_error:
                    errors.append((entry.name, str(exc)))
                else:
                    raise BuildError(
                        f"Unexpected error resolving '{entry.name}': {exc}",
                        package=entry.name,
                    ) from exc

    # Return in yml order
    ordered: list[ResolvedPackage] = []
    for idx in range(len(entries)):
        if idx in results:
            ordered.append(results[idx])
    return ResolveResult(entries=tuple(ordered), errors=tuple(errors))
