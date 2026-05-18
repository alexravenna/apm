"""Core enums and data-classes for APM package validation.

Public names are re-exported via ``apm_cli.models.validation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..apm_package import APMPackage


class PackageType(Enum):
    """Types of packages that APM can install.

    This enum is used internally to classify packages based on their content
    (presence of apm.yml, SKILL.md, hooks/, plugin.json, etc.).
    """

    APM_PACKAGE = "apm_package"  # Has apm.yml (.apm/ optional when deps declared)
    CLAUDE_SKILL = "claude_skill"  # Has SKILL.md, no apm.yml
    HOOK_PACKAGE = "hook_package"  # Has hooks/hooks.json, no apm.yml or SKILL.md
    HYBRID = "hybrid"  # Has both apm.yml and SKILL.md (root)
    MARKETPLACE_PLUGIN = "marketplace_plugin"  # Has plugin.json or .claude-plugin/
    SKILL_BUNDLE = "skill_bundle"  # Has skills/<name>/SKILL.md (nested), apm.yml optional
    INVALID = "invalid"  # None of the above


class PackageContentType(Enum):
    """Explicit package content type declared in apm.yml.

    This is the user-facing ``type`` field in apm.yml that controls how the
    package is processed during install/compile:
    - INSTRUCTIONS: Compile to AGENTS.md only, no skill created
    - SKILL: Install as native skill only, no AGENTS.md compilation
    - HYBRID: Both AGENTS.md instructions AND skill installation (default)
    - PROMPTS: Commands/prompts only, no instructions or skills
    """

    INSTRUCTIONS = "instructions"  # Compile to AGENTS.md only
    SKILL = "skill"  # Install as native skill only
    HYBRID = "hybrid"  # Both (default)
    PROMPTS = "prompts"  # Commands/prompts only

    @classmethod
    def from_string(cls, value: str) -> PackageContentType:
        """Parse a string value into a PackageContentType enum.

        Args:
            value: String value to parse (e.g., "instructions", "skill")

        Returns:
            PackageContentType: The corresponding enum value

        Raises:
            ValueError: If the value is not a valid package content type
        """
        if not value:
            raise ValueError("Package type cannot be empty")

        value_lower = value.lower().strip()
        for member in cls:
            if member.value == value_lower:
                return member

        valid_types = ", ".join(f"'{m.value}'" for m in cls)
        raise ValueError(f"Invalid package type '{value}'. Valid types are: {valid_types}")


class ValidationError(Enum):
    """Types of validation errors for APM packages."""

    MISSING_APM_YML = "missing_apm_yml"
    MISSING_APM_DIR = "missing_apm_dir"
    INVALID_YML_FORMAT = "invalid_yml_format"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_VERSION_FORMAT = "invalid_version_format"
    INVALID_DEPENDENCY_FORMAT = "invalid_dependency_format"
    EMPTY_APM_DIR = "empty_apm_dir"
    INVALID_PRIMITIVE_STRUCTURE = "invalid_primitive_structure"


class InvalidVirtualPackageExtensionError(ValueError):
    """Raised when a virtual package file has an invalid extension."""

    pass


@dataclass
class ValidationResult:
    """Result of APM package validation."""

    is_valid: bool
    errors: list[str]
    warnings: list[str]
    package: APMPackage | None = None
    package_type: PackageType | None = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID

    def __init__(self):
        self.is_valid = True
        self.errors = []
        self.warnings = []
        self.package = None
        self.package_type = None

    def add_error(self, error: str) -> None:
        """Add a validation error."""
        self.errors.append(error)
        self.is_valid = False

    def add_warning(self, warning: str) -> None:
        """Add a validation warning."""
        self.warnings.append(warning)

    def has_issues(self) -> bool:
        """Check if there are any errors or warnings."""
        return bool(self.errors or self.warnings)

    def summary(self) -> str:
        """Get a summary of validation results."""
        if self.is_valid and not self.warnings:
            return "[+] Package is valid"
        elif self.is_valid and self.warnings:
            return f"[!] Package is valid with {len(self.warnings)} warning(s)"
        else:
            return f"[x] Package is invalid with {len(self.errors)} error(s)"
