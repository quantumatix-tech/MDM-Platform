#!/usr/bin/env python3
"""
Unit tests for the compatibility matrix YAML and generator.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = REPO_ROOT / "docs" / "compatibility_matrix.yaml"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_compatibility_matrix.py"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


class TestCompatibilityMatrixYAML:
    """Tests for the compatibility matrix YAML structure."""

    def test_yaml_exists(self):
        assert YAML_PATH.exists(), f"YAML file not found at {YAML_PATH}"

    def test_yaml_parses(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data is not None, "YAML parsed to None"
        assert "compatibility_matrix" in data, "Missing 'compatibility_matrix' root key"

    def test_required_root_fields(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        matrix = data["compatibility_matrix"]
        assert "application_version" in matrix, "Missing application_version"
        assert "entries" in matrix, "Missing entries"
        assert "recorded_at" in matrix, "Missing recorded_at"
        assert "recorded_by" in matrix, "Missing recorded_by"

    def test_application_version_matches_pyproject(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        yaml_version = data["compatibility_matrix"]["application_version"]

        with PYPROJECT_PATH.open("r", encoding="utf-8") as f:
            pyproject = f.read()
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject)
        assert match, "Could not find version in pyproject.toml"
        pyproject_version = match.group(1)

        assert yaml_version == pyproject_version, (
            f"YAML version {yaml_version} does not match pyproject.toml version {pyproject_version}"
        )

    def test_entries_is_list(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data["compatibility_matrix"]["entries"], list), "entries must be a list"

    def test_required_fields_per_entry(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        required_fields = [
            "engine",
            "source_version",
            "target_version",
            "source_deployment",
            "target_deployment",
            "flow",
            "driver_version",
            "python_version",
            "status",
            "validation_level",
            "evidence",
            "known_limitations",
        ]

        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            for field in required_fields:
                assert field in entry, f"Entry {i} missing required field: {field}"

    def test_allowed_status_values(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        allowed = {"TESTED", "SUPPORTED", "UNVERIFIED", "UNSUPPORTED", "KNOWN_ISSUE"}
        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            assert entry["status"] in allowed, f"Entry {i} has invalid status: {entry['status']}"

    def test_allowed_flow_values(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        allowed = {"local_to_local", "local_to_cloud", "cloud_to_local", "cloud_to_cloud"}
        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            assert entry["flow"] in allowed, f"Entry {i} has invalid flow: {entry['flow']}"

    def test_allowed_deployment_values(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        allowed = {"local", "cloud"}
        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            assert entry["source_deployment"] in allowed, f"Entry {i} has invalid source_deployment: {entry['source_deployment']}"
            assert entry["target_deployment"] in allowed, f"Entry {i} has invalid target_deployment: {entry['target_deployment']}"

    def test_evidence_not_empty_for_tested_supported(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            if entry["status"] in {"TESTED", "SUPPORTED"}:
                assert entry["evidence"].strip(), f"Entry {i} ({entry['status']}) has empty evidence"

    def test_no_duplicate_entries(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        seen = set()
        for i, entry in enumerate(data["compatibility_matrix"]["entries"]):
            key = (
                entry["engine"],
                entry["source_version"],
                entry["target_version"],
                entry["source_deployment"],
                entry["target_deployment"],
                entry["flow"],
            )
            assert key not in seen, f"Duplicate entry at index {i}: {key}"
            seen.add(key)

    def test_deterministic_ordering(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]

        def sort_key(e):
            return (
                e["engine"],
                e["source_version"],
                e["target_version"],
                e["source_deployment"],
                e["target_deployment"],
                e["flow"],
            )

        sorted_entries = sorted(entries, key=sort_key)
        assert entries == sorted_entries, "Entries are not in deterministic order"

    def test_no_secrets_in_yaml(self):
        """Verify no actual secret values are present in the YAML.

        Checks for assignment-like patterns (e.g. password = "value") rather than
        legitimate descriptive words such as "secret provider" or field names.
        """
        with YAML_PATH.open("r", encoding="utf-8") as f:
            content = f.read()

        # Patterns that indicate an actual secret VALUE was committed, not a
        # descriptive word or field name.  We look for YAML assignments where a
        # secret-ish key is paired with a non-empty, non-placeholder value.
        import re

        secret_value_patterns = [
            # password = "actual_value"  (not "*****", not empty)
            r'''(?i)password\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            # token = "actual_value"
            r'''(?i)token\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            # api_key = "actual_value"
            r'''(?i)api[_-]?key\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            # secret = "actual_value"  (but not "secret provider" or "secret_provider")
            r'''(?i)(?<![\w ])secret\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            # credential = "actual_value"
            r'''(?i)credential\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
        ]

        for pattern in secret_value_patterns:
            matches = re.findall(pattern, content)
            assert not matches, (
                f"Potential secret value found in YAML matching pattern {pattern}: {matches}"
            )

    def test_specific_entries_exist(self):
        """Verify specific expected entries exist based on repository evidence."""
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]

        def find_entry(engine, source_v, target_v, src_dep, tgt_dep, flow):
            for e in entries:
                if (e["engine"] == engine and
                    e["source_version"] == source_v and
                    e["target_version"] == target_v and
                    e["source_deployment"] == src_dep and
                    e["target_deployment"] == tgt_dep and
                    e["flow"] == flow):
                    return e
            return None

        # PostgreSQL 17.4 local -> local TESTED
        e = find_entry("postgresql", "17.4", "17.4", "local", "local", "local_to_local")
        assert e is not None, "PostgreSQL 17.4 local->local TESTED entry missing"
        assert e["status"] == "TESTED"

        # PostgreSQL 17.4 -> 16 local -> cloud TESTED
        e = find_entry("postgresql", "17.4", "16", "local", "cloud", "local_to_cloud")
        assert e is not None, "PostgreSQL 17.4->16 local->cloud TESTED entry missing"
        assert e["status"] == "TESTED"

        # PostgreSQL 16 -> 17.4 cloud -> local SUPPORTED
        e = find_entry("postgresql", "16", "17.4", "cloud", "local", "cloud_to_local")
        assert e is not None, "PostgreSQL 16->17.4 cloud->local SUPPORTED entry missing"
        assert e["status"] == "SUPPORTED"

        # MSSQL local -> local TESTED (version unspecified, 2022-latest container)
        e = find_entry("mssql", "unspecified (2022-latest container)", "unspecified (2022-latest container)", "local", "local", "local_to_local")
        assert e is not None, "MSSQL local->local TESTED entry missing"
        assert e["status"] == "TESTED"

        # MSSQL local -> cloud TESTED (version unspecified, 2022-latest container)
        e = find_entry("mssql", "unspecified (2022-latest container)", "unspecified (2022-latest container)", "local", "cloud", "local_to_cloud")
        assert e is not None, "MSSQL local->cloud TESTED entry missing"
        assert e["status"] == "TESTED"

        # MSSQL cloud -> local TESTED (version unspecified, 2022-latest container)
        e = find_entry("mssql", "unspecified (2022-latest container)", "unspecified (2022-latest container)", "cloud", "local", "cloud_to_local")
        assert e is not None, "MSSQL cloud->local TESTED entry missing"
        assert e["status"] == "TESTED"

        # MySQL 8.0 local -> local TESTED
        e = find_entry("mysql", "8.0", "8.0", "local", "local", "local_to_local")
        assert e is not None, "MySQL 8.0 local->local TESTED entry missing"
        assert e["status"] == "TESTED"

        # MongoDB UNVERIFIED
        e = find_entry("mongodb", "7.0", "7.0", "local", "local", "local_to_local")
        assert e is not None, "MongoDB UNVERIFIED entry missing"
        assert e["status"] == "UNVERIFIED"


class TestGenerator:
    """Tests for the generate_compatibility_matrix.py script."""

    def test_generator_runs(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--validate-only"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0, f"Generator validation failed: {result.stderr}"

    def test_generator_output_is_deterministic(self):
        result1 = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--checksum"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        result2 = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--checksum"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result1.returncode == 0
        assert result2.returncode == 0
        assert result1.stdout.strip() == result2.stdout.strip(), "Generator output is not deterministic"

    def test_generator_idempotent(self):
        """Running generator twice produces identical output."""
        result1 = subprocess.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        result2 = subprocess.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result1.returncode == 0
        assert result2.returncode == 0
        assert result1.stdout == result2.stdout, "Generator is not idempotent"

    def test_generator_no_network_calls(self):
        """Generator should not make network calls (verified by no imports of network libs)."""
        with GENERATOR_PATH.open("r", encoding="utf-8") as f:
            content = f.read()
        forbidden_imports = ["requests", "urllib", "http.client", "socket", "ftplib", "telnetlib"]
        for imp in forbidden_imports:
            assert f"import {imp}" not in content and f"from {imp}" not in content, f"Generator imports {imp}"

    def test_generator_no_secrets_in_output(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0
        output = result.stdout.lower()
        # Check for actual secret VALUE assignments, not descriptive words
        # like "secret provider" or field names in documentation.
        import re

        secret_value_patterns = [
            r'''(?i)password\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            r'''(?i)token\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            r'''(?i)api[_-]?key\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            r'''(?i)(?<![\w ])secret\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
            r'''(?i)credential\s*[:=]\s*["']?[^"'\s*{}]{3,}["']?''',
        ]
        for pattern in secret_value_patterns:
            matches = re.findall(pattern, output)
            assert not matches, (
                f"Potential secret value found in generated output matching pattern {pattern}: {matches}"
            )

    def test_generator_preserves_existing_markdown_not_overwritten(self):
        """Ensure generator doesn't blindly overwrite the existing rich Markdown."""
        existing_md = REPO_ROOT / "docs" / "compatibility_matrix.md"
        assert existing_md.exists(), "Existing compatibility_matrix.md should exist"

        with existing_md.open("r", encoding="utf-8") as f:
            existing_content = f.read()

        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0

        # Generated content should not equal existing content (existing has narrative)
        assert result.stdout != existing_content, "Generator output unexpectedly matches existing Markdown exactly"

    def test_generator_write_flag(self, tmp_path):
        output_file = tmp_path / "test_output.md"
        result = subprocess.run(
            [sys.executable, str(GENERATOR_PATH), "--output", str(output_file)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        assert "Compatibility Matrix (Generated)" in content
        assert "Application Version:" in content


class TestMatrixCompleteness:
    """Verify matrix covers all engines mentioned in repository."""

    def test_postgresql_entries_cover_documented_evidence(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]
        pg_entries = [e for e in entries if e["engine"] == "postgresql"]

        # Find the specific PostgreSQL 17.4 local -> local TESTED entry
        pg_174_local_local = None
        pg_174_16_local_cloud = None
        pg_16_174_cloud_local = None
        for e in pg_entries:
            if (e["source_version"] == "17.4" and e["target_version"] == "17.4" and
                e["source_deployment"] == "local" and e["target_deployment"] == "local" and
                e["flow"] == "local_to_local"):
                pg_174_local_local = e
            elif (e["source_version"] == "17.4" and e["target_version"] == "16" and
                  e["source_deployment"] == "local" and e["target_deployment"] == "cloud" and
                  e["flow"] == "local_to_cloud"):
                pg_174_16_local_cloud = e
            elif (e["source_version"] == "16" and e["target_version"] == "17.4" and
                  e["source_deployment"] == "cloud" and e["target_deployment"] == "local" and
                  e["flow"] == "cloud_to_local"):
                pg_16_174_cloud_local = e

        assert pg_174_local_local is not None, "PostgreSQL 17.4 local->local entry missing"
        assert pg_174_local_local["status"] == "TESTED", f"Expected TESTED, got {pg_174_local_local['status']}"

        assert pg_174_16_local_cloud is not None, "PostgreSQL 17.4->16 local->cloud entry missing"
        assert pg_174_16_local_cloud["status"] == "TESTED", f"Expected TESTED, got {pg_174_16_local_cloud['status']}"

        assert pg_16_174_cloud_local is not None, "PostgreSQL 16->17.4 cloud->local entry missing"
        assert pg_16_174_cloud_local["status"] == "SUPPORTED", f"Expected SUPPORTED, got {pg_16_174_cloud_local['status']}"

    def test_mssql_entries_cover_documented_evidence(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]
        mssql_entries = [e for e in entries if e["engine"] == "mssql"]

        statuses = {(e["flow"], e["status"]) for e in mssql_entries}
        assert ("local_to_local", "TESTED") in statuses
        assert ("local_to_cloud", "TESTED") in statuses
        assert ("cloud_to_local", "TESTED") in statuses

    def test_mysql_entries_match_evidence(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]
        mysql_entries = [e for e in entries if e["engine"] == "mysql"]

        # local_to_local should be TESTED (we have E2E evidence)
        local_local = next(e for e in mysql_entries if e["flow"] == "local_to_local")
        assert local_local["status"] == "TESTED", f"Expected TESTED, got {local_local['status']}"
        assert local_local["validation_level"] == "e2e", f"Expected e2e, got {local_local['validation_level']}"

        # Other three flows should be SUPPORTED (previously verified; historical evidence not recorded)
        for e in mysql_entries:
            if e["flow"] != "local_to_local":
                assert e["status"] == "SUPPORTED", f"MySQL entry {e['flow']} should be SUPPORTED"
                assert e["validation_level"] == "documented", f"MySQL entry {e['flow']} validation_level should be documented"
                assert "historical E2E evidence is not currently recorded" in e["evidence"], f"MySQL entry {e['flow']} evidence should mention historical evidence"

    def test_mongodb_all_unverified(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data["compatibility_matrix"]["entries"]
        mongo_entries = [e for e in entries if e["engine"] in {"mongodb", "cosmosdb"}]

        for e in mongo_entries:
            assert e["status"] == "UNVERIFIED", f"MongoDB/Cosmos entry {e['flow']} should be UNVERIFIED"


class TestRuntimeComponents:
    """Tests for the runtime.components section of the compatibility matrix."""

    ALLOWED_STATUSES = {"TESTED", "SUPPORTED", "UNVERIFIED"}
    REQUIRED_FIELDS = ["component", "version", "status", "evidence", "notes"]

    def _get_components(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        runtime = data["compatibility_matrix"].get("runtime")
        assert runtime is not None, "runtime section missing from compatibility_matrix"
        components = runtime.get("components")
        assert components is not None, "runtime.components missing"
        assert isinstance(components, list), "runtime.components must be a list"
        return components

    def test_runtime_section_exists(self):
        with YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "runtime" in data["compatibility_matrix"], "runtime section missing"
        assert "recorded_at" in data["compatibility_matrix"]["runtime"]
        assert "recorded_by" in data["compatibility_matrix"]["runtime"]
        assert "source" in data["compatibility_matrix"]["runtime"]

    def test_required_fields_per_component(self):
        components = self._get_components()
        for i, comp in enumerate(components):
            for field in self.REQUIRED_FIELDS:
                assert field in comp, f"runtime.components[{i}] missing required field '{field}'"

    def test_allowed_status_values(self):
        components = self._get_components()
        for i, comp in enumerate(components):
            assert comp["status"] in self.ALLOWED_STATUSES, (
                f"runtime.components[{i}] has invalid status: {comp['status']}"
            )

    def test_evidence_not_empty_for_tested_supported(self):
        components = self._get_components()
        for i, comp in enumerate(components):
            if comp["status"] in {"TESTED", "SUPPORTED"}:
                assert comp["evidence"].strip(), (
                    f"runtime.components[{i}] ({comp['status']}) has empty evidence"
                )

    def test_no_duplicate_components(self):
        components = self._get_components()
        seen = set()
        for i, comp in enumerate(components):
            name = comp["component"]
            assert name not in seen, f"runtime.components[{i}]: duplicate component '{name}'"
            seen.add(name)

    def test_expected_components_present(self):
        components = self._get_components()
        names = {c["component"] for c in components}
        expected = {
            "python", "psycopg", "pyodbc", "mysql-connector-python",
            "pymongo", "cryptography", "azure-identity", "boto3",
            "hvac", "rich", "pyyaml", "requests",
        }
        missing = expected - names
        assert not missing, f"Missing runtime components: {missing}"

    def test_python_is_tested(self):
        components = self._get_components()
        py = next(c for c in components if c["component"] == "python")
        assert py["status"] == "TESTED", f"python should be TESTED, got {py['status']}"

    def test_psycopg_is_tested(self):
        components = self._get_components()
        psycopg = next(c for c in components if c["component"] == "psycopg")
        assert psycopg["status"] == "TESTED", f"psycopg should be TESTED, got {psycopg['status']}"

    def test_pyodbc_is_tested(self):
        components = self._get_components()
        pyodbc = next(c for c in components if c["component"] == "pyodbc")
        assert pyodbc["status"] == "TESTED", f"pyodbc should be TESTED, got {pyodbc['status']}"

    def test_mysql_connector_is_tested(self):
        components = self._get_components()
        mysql = next(c for c in components if c["component"] == "mysql-connector-python")
        assert mysql["status"] == "TESTED", f"mysql-connector-python should be TESTED, got {mysql['status']}"

    def test_pymongo_is_supported(self):
        components = self._get_components()
        mongo = next(c for c in components if c["component"] == "pymongo")
        assert mongo["status"] == "SUPPORTED", f"pymongo should be SUPPORTED, got {mongo['status']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])