"""Validation logic and type enums for APM packages.

All public names from this package are importable directly from
``apm_cli.models.validation`` for full backward compatibility.

Sub-modules
-----------
_types
    Core enums and data-classes (PackageType, PackageContentType, etc.).
_detection
    File-system evidence collection and the package-type detection cascade.
_validators
    Per-type validation helpers and the public ``validate_apm_package`` entry-point.
"""

from ._detection import DetectionEvidence, detect_package_type, gather_detection_evidence
from ._types import (
    InvalidVirtualPackageExtensionError,
    PackageContentType,
    PackageType,
    ValidationError,
    ValidationResult,
)
from ._validators import validate_apm_package

__all__ = [
    "DetectionEvidence",
    "InvalidVirtualPackageExtensionError",
    "PackageContentType",
    "PackageType",
    "ValidationError",
    "ValidationResult",
    "detect_package_type",
    "gather_detection_evidence",
    "validate_apm_package",
]
