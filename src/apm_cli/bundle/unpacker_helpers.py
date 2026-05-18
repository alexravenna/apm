"""Helper functions for bundle unpacking operations."""

import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PureWindowsPath

from ..deps.lockfile import LEGACY_LOCKFILE_NAME, LOCKFILE_NAME, LockFile
from ..utils.path_security import PathTraversalError, validate_path_segments


def extract_archive_to_temp(bundle_path: Path) -> tuple[Path, Path, bool]:
    """Extract tar.gz archive to temporary directory.

    Args:
        bundle_path: Path to the .tar.gz file.

    Returns:
        Tuple of (source_dir, temp_dir, cleanup_needed).

    Raises:
        ValueError: If archive contains unsafe paths or entries.
    """
    from ..config import get_apm_temp_dir

    temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            # Security: prevent path traversal and special entries
            for member in tar.getmembers():
                name = member.name
                if (
                    name.startswith("/")
                    or PureWindowsPath(name).drive
                    or PureWindowsPath(name).is_absolute()
                ):
                    raise ValueError(f"Refusing to extract path-traversal entry: {name}")
                try:
                    validate_path_segments(name, context="tar member")
                except PathTraversalError:
                    raise ValueError(f"Refusing to extract path-traversal entry: {name}") from None
                if member.issym() or member.islnk():
                    raise ValueError(f"Refusing to extract symlink/hardlink: {name}")
            # filter="data" was added in Python 3.12; use it when available
            if sys.version_info >= (3, 12):
                tar.extractall(temp_dir, filter="data")
            else:
                tar.extractall(temp_dir)  # noqa: S202
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    # Locate inner directory (the archive wraps a single top-level dir)
    children = list(temp_dir.iterdir())
    if len(children) == 1 and children[0].is_dir():  # noqa: SIM108
        source_dir = children[0]
    else:
        source_dir = temp_dir

    return source_dir, temp_dir, True


def locate_and_read_lockfile(source_dir: Path) -> tuple[LockFile | None, dict]:
    """Locate and read lockfile from bundle directory.

    Args:
        source_dir: Bundle source directory.

    Returns:
        Tuple of (lockfile, pack_metadata).

    Raises:
        FileNotFoundError: If lockfile is missing or corrupt.
    """
    import yaml

    lockfile_path = source_dir / LOCKFILE_NAME
    if not lockfile_path.exists():
        # Backward compat: older bundles used "apm.lock"
        legacy_lockfile_path = source_dir / LEGACY_LOCKFILE_NAME
        if legacy_lockfile_path.exists():
            lockfile_path = legacy_lockfile_path

    # Extract pack: metadata (written by apm pack) before structured parse
    pack_meta: dict = {}
    try:
        raw = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            val = raw.get("pack", {})
            pack_meta = val if isinstance(val, dict) else {}
    except Exception:
        pass  # non-critical -- proceed without metadata

    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        if not lockfile_path.exists():
            raise FileNotFoundError(
                f"{lockfile_path.name} not found in the bundle  -- the bundle may be incomplete."
            )
        raise FileNotFoundError(
            f"{lockfile_path.name} in the bundle could not be parsed  -- the bundle may be corrupt."
        )

    return lockfile, pack_meta


def collect_and_verify_bundle_files(
    lockfile: LockFile, source_dir: Path, skip_verify: bool
) -> tuple[list[str], dict[str, list[str]], bool]:
    """Collect deployed files and verify bundle completeness.

    Args:
        lockfile: The lockfile to extract files from.
        source_dir: Bundle source directory.
        skip_verify: Whether to skip verification.

    Returns:
        Tuple of (unique_files, dep_file_map, verified).

    Raises:
        ValueError: If verification fails and files are missing.
    """
    dep_file_map: dict[str, list[str]] = {}
    seen: set[str] = set()
    unique_files: list[str] = []
    for dep in lockfile.get_all_dependencies():
        dep_key = dep.get_unique_key()
        dep_files: list[str] = []
        for f in dep.deployed_files:
            dep_files.append(f)
            if f not in seen:
                seen.add(f)
                unique_files.append(f)
        if dep_files:
            dep_file_map[dep_key] = dep_files

    # Verify completeness
    verified = True
    if not skip_verify:
        missing = [f for f in unique_files if not (source_dir / f).exists()]
        if missing:
            raise ValueError(
                "Bundle verification failed  -- the following deployed files "
                "are missing from the bundle:\n" + "\n".join(f"  - {m}" for m in missing)
            )

    if skip_verify:
        verified = False

    return unique_files, dep_file_map, verified


def scan_bundle_for_security(source_dir: Path, force: bool) -> tuple[int, int]:
    """Scan bundle contents for hidden Unicode characters.

    Args:
        source_dir: Bundle source directory.
        force: Whether to force deployment on critical findings.

    Returns:
        Tuple of (warning_count, critical_count).

    Raises:
        ValueError: If critical findings are found and force is False.
    """
    from ..security.gate import BLOCK_POLICY, SecurityGate

    verdict = SecurityGate.scan_files(source_dir, policy=BLOCK_POLICY, force=force)
    security_warnings = verdict.warning_count
    security_critical = verdict.critical_count

    if verdict.should_block:
        affected = []
        for path, findings in verdict.findings_by_file.items():
            c = sum(1 for f in findings if f.severity == "critical")
            if c > 0:
                affected.append(f"  {path}  ({c} critical)")
        raise ValueError(
            f"Blocked: bundle contains {len(affected)} file(s) "
            f"with critical hidden characters\n\n"
            f"Affected files:\n" + "\n".join(affected) + "\n\n"
            "Next steps:\n"
            "  - Extract the bundle and run: apm audit --file <path> to inspect\n"
            "  - Run: apm unpack --force to deploy anyway "
            "(not recommended)\n\n"
            "Learn more: https://apm.github.io/apm/enterprise/security/"
        )

    return security_warnings, security_critical


def copy_bundle_to_output(
    unique_files: list[str],
    source_dir: Path,
    output_dir: Path,
) -> int:
    """Copy bundle files to output directory.

    Args:
        unique_files: List of files to copy.
        source_dir: Bundle source directory.
        output_dir: Target output directory.

    Returns:
        Number of skipped files.

    Raises:
        ValueError: If unsafe paths are detected.
    """
    output_dir_resolved = output_dir.resolve()
    skipped = 0
    for rel_path in unique_files:
        # Guard against absolute paths or path-traversal entries in deployed_files
        p = Path(rel_path)
        if p.is_absolute() or rel_path.startswith("/") or ".." in p.parts:
            raise ValueError(f"Refusing to unpack unsafe path from bundle lockfile: {rel_path!r}")
        dest = output_dir / rel_path
        if not dest.resolve().is_relative_to(output_dir_resolved):
            raise ValueError(f"Refusing to unpack path that escapes output directory: {rel_path!r}")
        src = source_dir / rel_path
        if src.is_symlink():
            # Security: skip symlinks to prevent scanning bypass
            skipped += 1
            continue
        if not src.exists():
            skipped += 1
            continue  # skip_verify may allow missing files
        if src.is_dir():
            from ..security.gate import ignore_non_content

            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore_non_content)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)

    return skipped
