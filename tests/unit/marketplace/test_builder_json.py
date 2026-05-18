"""Tests for BuildReport JSON serialization (phase-3b, T-3b-01..08).

Covers:
- to_json_dict() produces correct §4 shape
- failure_to_json_dict() classmethod shape
- ok/dry_run flags, warnings/errors aggregation
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.marketplace.builder import (
    BuildReport,
    MarketplaceOutputReport,
)


def _make_output_report(**kwargs) -> MarketplaceOutputReport:
    return MarketplaceOutputReport(
        profile=kwargs.get("profile", "claude"),
        resolved=(),
        errors=kwargs.get("errors", ()),
        warnings=kwargs.get("warnings", ()),
        added_count=kwargs.get("added", 0),
        updated_count=kwargs.get("updated", 0),
        unchanged_count=kwargs.get("unchanged", 0),
        removed_count=kwargs.get("removed", 0),
        output_path=Path(kwargs.get("output_path", ".claude-plugin/marketplace.json")),
        dry_run=kwargs.get("dry_run", False),
    )


class TestBuildReportToJsonDict:
    """T-3b-01..05: to_json_dict() shape."""

    def test_success_shape(self) -> None:
        out = _make_output_report(added=2, updated=1, unchanged=3)
        report = BuildReport(outputs=(out,))
        result = report.to_json_dict()

        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["bundle"] is None
        assert result["warnings"] == []
        assert result["errors"] == []
        assert len(result["marketplace"]["outputs"]) == 1

        entry = result["marketplace"]["outputs"][0]
        assert entry["format"] == "claude"
        assert entry["added"] == 2
        assert entry["updated"] == 1
        assert entry["unchanged"] == 3
        assert entry["skipped"] == 0

    def test_multiple_outputs(self) -> None:
        out1 = _make_output_report(profile="claude", added=1)
        out2 = _make_output_report(
            profile="codex",
            output_path=".agents/plugins/marketplace.json",
            added=2,
        )
        report = BuildReport(outputs=(out1, out2))
        result = report.to_json_dict()

        assert result["ok"] is True
        assert len(result["marketplace"]["outputs"]) == 2
        formats = [e["format"] for e in result["marketplace"]["outputs"]]
        assert "claude" in formats
        assert "codex" in formats

    def test_errors_make_ok_false(self) -> None:
        out = _make_output_report(
            errors=(("my-tool", "git timeout"),),
        )
        report = BuildReport(outputs=(out,))
        result = report.to_json_dict()

        assert result["ok"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["code"] == "build_error"
        assert "my-tool" in result["errors"][0]["message"]

    def test_warnings_aggregated(self) -> None:
        out = _make_output_report(
            warnings=("warning A", "warning B"),
        )
        report = BuildReport(outputs=(out,))
        result = report.to_json_dict()

        assert result["warnings"] == ["warning A", "warning B"]

    def test_dry_run_flag(self) -> None:
        out = _make_output_report(dry_run=True)
        report = BuildReport(outputs=(out,))
        result = report.to_json_dict()

        assert result["dry_run"] is True


class TestFailureToJsonDict:
    """T-3b-06..08: failure_to_json_dict() classmethod."""

    def test_basic_failure_shape(self) -> None:
        result = BuildReport.failure_to_json_dict(
            errors=[{"code": "config_error", "message": "bad config"}]
        )
        assert result["ok"] is False
        assert result["dry_run"] is False
        assert result["bundle"] is None
        assert result["marketplace"]["outputs"] == []
        assert len(result["errors"]) == 1

    def test_with_warnings(self) -> None:
        result = BuildReport.failure_to_json_dict(
            errors=[{"code": "unknown_format", "message": "no such format"}],
            warnings=["deprecated flag used"],
        )
        assert result["warnings"] == ["deprecated flag used"]

    def test_dry_run_passthrough(self) -> None:
        result = BuildReport.failure_to_json_dict(
            errors=[{"code": "x", "message": "y"}],
            dry_run=True,
        )
        assert result["dry_run"] is True
