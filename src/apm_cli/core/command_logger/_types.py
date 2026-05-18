"""Shared types and pure helpers for the command_logger package."""

from dataclasses import dataclass


def _strip_source_prefix(source: str) -> str:
    """Strip the ``org:`` / ``url:`` prefix from a policy source string."""
    if not source:
        return ""
    return source.removeprefix("org:").removeprefix("url:")


@dataclass
class _ValidationOutcome:
    """Result of package validation before install."""

    valid: list  # List of (canonical_name, already_present: bool) tuples
    invalid: list  # List of (package_name, reason: str) tuples
    marketplace_provenance: dict = None  # canonical -> {discovered_via, marketplace_plugin_name}

    @property
    def all_failed(self) -> bool:
        return len(self.valid) == 0 and len(self.invalid) > 0

    @property
    def has_failures(self) -> bool:
        return len(self.invalid) > 0

    @property
    def new_packages(self) -> list:
        """Packages that are valid and NOT already present."""
        return [(name, present) for name, present in self.valid if not present]
