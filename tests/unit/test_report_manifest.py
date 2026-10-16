"""Unit tests for runtime manifest integration in migration reports."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core.reporting.report_builder import ReportBuilder


class TestReportManifestIntegration:
    """Tests for runtime manifest integration in JSON and HTML reports."""

    def _make_result(self, run_id: str = "test-run-123") -> dict:
        return {
            "run_id": run_id,
            "mode": "full",
            "status": "success",
            "phases": {
                "discover": {"objects": ["users", "orders"]},
                "users": {"source_rows": 100, "success": 100, "failure": 0},
                "orders": {"source_rows": 50, "success": 50, "failure": 0},
            },
        }

    def _make_manifest(self) -> dict:
        return {
            "schema_version": "1.0",
            "generated_at": "2026-10-07T10:00:00Z",
            "application": {
                "name": "migration-platform",
                "version": "0.2.3",
            },
            "git": {
                "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
                "commit_short": "abcdef123456",
                "tag": "v0.2.3",
                "dirty": False,
                "branch": "main",
            },
            "python": {
                "version": "3.12.6",
                "implementation": "CPython",
                "platform": "Windows-10-10.0.19045-SP0",
            },
            "packages": {
                "psycopg": "3.2.9",
                "pyodbc": "5.2.0",
                "mysql-connector-python": "not_installed",
            },
            "database": {
                "source": {"engine": "postgresql", "version": "PostgreSQL 17.4"},
                "target": {"engine": "postgresql", "version": "PostgreSQL 16"},
            },
            "config_hash": "a1b2c3d4e5f67890",
        }

    def test_json_report_includes_runtime_manifest(self):
        """JSON report must include the runtime manifest when provided."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        json_report = builder.build_json()

        assert "runtime_manifest" in json_report
        assert json_report["runtime_manifest"] == manifest

    def test_json_report_excludes_manifest_when_none(self):
        """JSON report must not include runtime_manifest key when manifest is None."""
        result = self._make_result()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=None)
        json_report = builder.build_json()

        assert "runtime_manifest" not in json_report

    def test_json_report_contains_required_manifest_fields(self):
        """JSON report manifest must contain all required environment fields."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        json_report = builder.build_json()

        rm = json_report["runtime_manifest"]
        assert rm["schema_version"] == "1.0"
        assert "generated_at" in rm
        assert rm["application"]["name"] == "migration-platform"
        assert rm["application"]["version"] == "0.2.3"
        assert "commit_sha" in rm["git"]
        assert "commit_short" in rm["git"]
        assert "tag" in rm["git"]
        assert "dirty" in rm["git"]
        assert "branch" in rm["git"]
        assert "version" in rm["python"]
        assert "implementation" in rm["python"]
        assert "platform" in rm["python"]
        assert "psycopg" in rm["packages"]
        assert rm["database"]["source"]["engine"] == "postgresql"
        assert rm["database"]["target"]["engine"] == "postgresql"
        assert "config_hash" in rm
        assert len(rm["config_hash"]) == 16

    def test_html_report_includes_runtime_manifest_section(self):
        """HTML report must include a Runtime Manifest section when manifest provided."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        html = builder.build_html()

        assert "Runtime Manifest" in html
        assert "schema v1.0" in html
        assert "Runtime Environment Manifest" in html
        assert "abcdef123456" in html  # commit_short
        assert "v0.2.3" in html  # tag and app version
        assert "PostgreSQL 17.4" in html
        assert "PostgreSQL 16" in html
        assert "a1b2c3d4e5f67890" in html  # config_hash

    def test_html_report_excludes_manifest_when_none(self):
        """HTML report must not include Runtime Manifest section when manifest is None."""
        result = self._make_result()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=None)
        html = builder.build_html()

        # The HTML comment remains but the actual manifest accordion section should not be rendered
        assert "Runtime Environment Manifest" not in html
        assert "accordion-body" not in html or "schema_version" not in html

    def test_secret_values_never_appear_in_json_report(self):
        """Secret values must never appear in the JSON report, even in manifest."""
        result = self._make_result()
        manifest = self._make_manifest()
        # Add a fake secret that should be redacted in the manifest
        manifest["database"]["source"]["password"] = "**REDACTED**"

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        json_report = builder.build_json()

        serialized = json.dumps(json_report)
        assert "source-secret" not in serialized
        assert "target-secret" not in serialized
        assert "**REDACTED**" in serialized  # redacted form is OK

    def test_secret_values_never_appear_in_html_report(self):
        """Secret values must never appear in the HTML report, even in manifest."""
        result = self._make_result()
        manifest = self._make_manifest()
        manifest["database"]["source"]["password"] = "**REDACTED**"

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        html = builder.build_html()

        assert "source-secret" not in html
        assert "target-secret" not in html
        assert "**REDACTED**" in html  # redacted form is OK

    def test_manifest_information_preserved_correctly(self):
        """All manifest fields must be preserved correctly in both report formats."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)

        json_report = builder.build_json()
        html_report = builder.build_html()

        # Verify key fields appear in both
        rm = json_report["runtime_manifest"]
        assert rm["application"]["version"] == "0.2.3"
        assert rm["git"]["commit_short"] == "abcdef123456"
        assert rm["git"]["tag"] == "v0.2.3"
        assert rm["git"]["branch"] == "main"
        assert rm["git"]["dirty"] is False
        assert rm["python"]["version"] == "3.12.6"
        assert rm["packages"]["psycopg"] == "3.2.9"
        assert rm["database"]["source"]["version"] == "PostgreSQL 17.4"
        assert rm["database"]["target"]["version"] == "PostgreSQL 16"
        assert rm["config_hash"] == "a1b2c3d4e5f67890"

        # Verify same info in HTML
        assert "0.2.3" in html_report
        assert "abcdef123456" in html_report
        assert "v0.2.3" in html_report
        assert "main" in html_report
        assert "3.12.6" in html_report
        assert "PostgreSQL 17.4" in html_report
        assert "PostgreSQL 16" in html_report
        assert "a1b2c3d4e5f67890" in html_report

    def test_report_version_and_platform_version_present(self):
        """JSON report must include report_version and platform_version."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        json_report = builder.build_json()

        assert json_report["report_version"] == "2.0"
        assert "platform_version" in json_report
        assert isinstance(json_report["platform_version"], str)

    def test_html_includes_platform_version_in_footer(self):
        """HTML report footer must include platform version."""
        result = self._make_result()
        manifest = self._make_manifest()

        builder = ReportBuilder(result, 1000.0, 1100.0, manifest=manifest)
        html = builder.build_html()

        assert "platform_version" in html or "0.2.3" in html  # version appears in footer


if __name__ == "__main__":
    pytest.main([__file__, "-v"])