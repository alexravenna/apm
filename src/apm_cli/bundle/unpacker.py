"""Bundle unpacker  -- extracts and verifies APM bundles."""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..security.gate import ignore_non_content
from .unpacker_helpers import (
    collect_and_verify_bundle_files,
    copy_bundle_to_output,
    extract_archive_to_temp,
    locate_and_read_lockfile,
    scan_bundle_for_security,
)

_COPYTREE_IGNORE = ignore_non_content


@dataclass
class UnpackResult:
    """Result of an unpack operation."""

    extracted_dir: Path
    files: list[str] = field(default_factory=list)
    verified: bool = False
    dependency_files: dict[str, list[str]] = field(default_factory=dict)
    skipped_count: int = 0
    security_warnings: int = 0
    security_critical: int = 0
    pack_meta: dict = field(default_factory=dict)


def unpack_bundle(
    bundle_path: Path,
    output_dir: Path = Path("."),
    skip_verify: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> UnpackResult:
    """Extract and apply an APM bundle to a project directory.

    Additive-only semantics (v1): only writes files listed in the bundle's
    lockfile ``deployed_files``.  Never deletes existing files.  If a local
    file has the same name as a bundle file, the bundle file wins (overwrite).

    Args:
        bundle_path: Path to a ``.tar.gz`` archive or an unpacked bundle directory.
        output_dir: Target project directory to copy files into.
        skip_verify: If *True*, skip completeness verification against the lockfile.
        dry_run: If *True*, resolve the file list but write nothing to disk.
        force: If *True*, deploy even when critical hidden characters are found.

    Returns:
        :class:`UnpackResult` describing what was (or would be) extracted.

    Raises:
        FileNotFoundError: If the bundle's ``apm.lock.yaml`` is missing.
        ValueError: If verification finds files listed in the lockfile but
            absent from the bundle.
    """
    # 1. If archive, extract to temp dir
    cleanup_temp = False
    temp_dir = None
    if bundle_path.is_file() and bundle_path.name.endswith(".tar.gz"):
        source_dir, temp_dir, cleanup_temp = extract_archive_to_temp(bundle_path)
    elif bundle_path.is_dir():
        source_dir = bundle_path
        temp_dir = None
    else:
        raise FileNotFoundError(f"Bundle not found or unsupported format: {bundle_path}")

    try:
        # 2. Read apm.lock.yaml (or legacy apm.lock) from bundle
        lockfile, pack_meta = locate_and_read_lockfile(source_dir)

        # Collect deployed_files per dependency and deduplicated global list
        unique_files, dep_file_map, verified = collect_and_verify_bundle_files(
            lockfile, source_dir, skip_verify
        )

        # 3b. Security scan: check bundle contents for hidden Unicode characters
        security_warnings, security_critical = scan_bundle_for_security(source_dir, force)

        # Dry-run: return file list without writing
        if dry_run:
            return UnpackResult(
                extracted_dir=bundle_path,
                files=unique_files,
                verified=verified,
                dependency_files=dep_file_map,
                security_warnings=security_warnings,
                security_critical=security_critical,
                pack_meta=pack_meta,
            )

        # 4. Copy target files to output_dir (additive, no deletes)
        output_dir = Path(output_dir)
        skipped = copy_bundle_to_output(unique_files, source_dir, output_dir)

        return UnpackResult(
            extracted_dir=bundle_path,
            files=unique_files,
            verified=verified,
            dependency_files=dep_file_map,
            skipped_count=skipped,
            security_warnings=security_warnings,
            security_critical=security_critical,
            pack_meta=pack_meta,
        )
    finally:
        # Clean up temp dir if we created one
        if cleanup_temp and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
