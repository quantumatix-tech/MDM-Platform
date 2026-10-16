"""Unit tests for release validation checks."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.release_validation import (
    _declared_floor,
    _requirement_name,
)


class TestVersionValidation:
    """Tests for version-related validation checks."""

    def test_declared_version_valid(self):
        """Test that pyproject.toml declares a valid version."""
        pyproject = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = data["project"]["version"]
        assert version == "0.2.3"
        assert version.count(".") == 2
        assert all(p.isdigit() for p in version.split("."))

    def test_installed_metadata_matches(self):
        """Test that installed metadata matches pyproject.toml."""
        pyproject = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        declared = data["project"]["version"]
        from importlib.metadata import version as pkg_version
        installed = pkg_version("migration-platform")
        assert installed == declared

    def test_cli_version_exact_match(self):
        """Test that CLI --version matches pyproject.toml exactly."""
        pyproject = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        declared = data["project"]["version"]
        expected = f"migration-platform {declared}"

        proc = subprocess.run(
            [sys.executable, "-m", "migration_platform", "--version"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == expected


class TestChangelogValidation:
    """Tests for CHANGELOG format validation."""

    def test_changelog_has_unreleased_and_current(self):
        """Test CHANGELOG has [Unreleased] and current release section."""
        changelog = REPO_ROOT / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        assert "## [Unreleased]" in text
        assert "## [0.2.3]" in text

    def test_changelog_release_section_format(self):
        """Test CHANGELOG release section follows Keep a Changelog format."""
        changelog = REPO_ROOT / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")

        # Check date format in header
        import re
        date_pattern = r"## \[0\.2\.3\] - \d{4}-\d{2}-\d{2}"
        assert re.search(date_pattern, text), "Release header missing date"

        # Check for category headers in 0.2.3 section
        # Extract 0.2.3 section
        lines = text.splitlines()
        in_section = False
        section_content = []
        for line in lines:
            if re.match(r"## \[0\.2\.3\](?: - \d{4}-\d{2}-\d{2})?", line.strip()):
                in_section = True
                continue
            if in_section:
                if line.startswith("## [") and "0.2.3" not in line:
                    break
                section_content.append(line)

        content = "\n".join(section_content)
        assert "### Added" in content or "### Changed" in content or "### Notes" in content

    def test_changelog_format_check_passes(self):
        """Test the check_changelog_format function passes for valid CHANGELOG."""
        # This will run the actual check function
        # We need to ensure the global _results is reset
        import scripts.release_validation as rv
        rv._results.clear()
        result = rv.check_changelog_format()
        assert result is True

    def test_changelog_format_rejects_missing_date(self, tmp_path):
        """Test CHANGELOG format check fails when date is missing."""
        changelog_content = """# Changelog

## [Unreleased]

## [1.0.0]

### Added
- New feature
"""
        changelog_file = tmp_path / "CHANGELOG.md"
        changelog_file.write_text(changelog_content)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        # Create a temporary pyproject.toml
        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        try:
            rv._results.clear()
            result = rv.check_changelog_format()
            assert result is False
        finally:
            rv.REPO_ROOT = original_root

    def test_changelog_format_rejects_empty_section(self, tmp_path):
        """Test CHANGELOG format check fails when section is empty."""
        changelog_content = """# Changelog

## [Unreleased]

## [1.0.0] - 2026-01-01

## [0.9.0] - 2025-12-01
"""
        changelog_file = tmp_path / "CHANGELOG.md"
        changelog_file.write_text(changelog_content)

        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            result = rv.check_changelog_format()
            assert result is False
        finally:
            rv.REPO_ROOT = original_root


class TestGitTagValidation:
    """Tests for Git tag consistency validation."""

    def test_get_current_tag_on_tagged_commit(self, tmp_path):
        """Test _get_current_tag returns tag when on tagged commit."""
        # Create a temp repo with a matching tag
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)

        # Tag with matching version
        subprocess.run(["git", "tag", "-a", "v1.0.0", "-m", "Release v1.0.0"], cwd=tmp_path, capture_output=True, check=False)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            tag = rv._get_current_tag()
            assert tag == "v1.0.0"
        finally:
            rv.REPO_ROOT = original_root

    def test_get_current_tag_returns_none_when_not_tagged(self, tmp_path):
        """Test _get_current_tag returns None when HEAD is not tagged."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)
        # NO TAG

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            tag = rv._get_current_tag()
            assert tag is None
        finally:
            rv.REPO_ROOT = original_root

    def test_git_tag_consistency_skipped_in_normal_mode(self):
        """Test git tag check is skipped when not in release mode."""
        import scripts.release_validation as rv
        rv._results.clear()
        if "RELEASE_VALIDATION_MODE" in os.environ:
            del os.environ["RELEASE_VALIDATION_MODE"]
        result = rv.check_git_tag_consistency()
        assert result is True  # Skipped returns True

    def test_git_tag_consistency_passes_when_matching(self, tmp_path):
        """Test git tag check passes when tag matches version."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)

        # Tag with matching version
        subprocess.run(["git", "tag", "-a", "v1.0.0", "-m", "Release v1.0.0"], cwd=tmp_path, capture_output=True, check=False)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            os.environ["RELEASE_VALIDATION_MODE"] = "1"
            result = rv.check_git_tag_consistency()
            assert result is True
        finally:
            del os.environ["RELEASE_VALIDATION_MODE"]
            rv.REPO_ROOT = original_root

    def test_git_tag_consistency_fails_when_mismatched(self, tmp_path):
        """Test git tag check fails when tag doesn't match version."""
        # Create a temp repo with a mismatched tag
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        # Create pyproject.toml with version 1.0.0
        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)

        # Create a commit
        subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)

        # Tag with WRONG version
        subprocess.run(["git", "tag", "-a", "v2.0.0", "-m", "Release v2.0.0"], cwd=tmp_path, capture_output=True, check=False)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            os.environ["RELEASE_VALIDATION_MODE"] = "1"
            result = rv.check_git_tag_consistency()
            assert result is False
        finally:
            del os.environ["RELEASE_VALIDATION_MODE"]
            rv.REPO_ROOT = original_root

    def test_git_tag_consistency_fails_when_no_tag(self, tmp_path):
        """Test git tag check fails when no tag exists in release mode."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        pyproject_content = """
[project]
name = "test"
version = "1.0.0"
"""
        (tmp_path / "pyproject.toml").write_text(pyproject_content)
        subprocess.run(["git", "add", "pyproject.toml"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)
        # NO TAG CREATED

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            os.environ["RELEASE_VALIDATION_MODE"] = "1"
            result = rv.check_git_tag_consistency()
            assert result is False
        finally:
            del os.environ["RELEASE_VALIDATION_MODE"]
            rv.REPO_ROOT = original_root


class TestCleanWorkingTree:
    """Tests for clean working tree validation."""

    def test_clean_working_tree_skipped_in_normal_mode(self):
        """Test dirty tree check is skipped when not in release mode."""
        import scripts.release_validation as rv
        rv._results.clear()
        if "RELEASE_VALIDATION_MODE" in os.environ:
            del os.environ["RELEASE_VALIDATION_MODE"]
        result = rv.check_clean_working_tree()
        assert result is True  # Skipped returns True

    def test_clean_working_tree_passes_when_clean(self, tmp_path):
        """Test dirty tree check passes when working tree is clean."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            os.environ["RELEASE_VALIDATION_MODE"] = "1"
            result = rv.check_clean_working_tree()
            assert result is True
        finally:
            del os.environ["RELEASE_VALIDATION_MODE"]
            rv.REPO_ROOT = original_root

    def test_clean_working_tree_fails_when_dirty(self, tmp_path):
        """Test dirty tree check fails when working tree has changes."""
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True, check=False)

        (tmp_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=False)

        # Modify file after commit
        (tmp_path / "README.md").write_text("# Modified")

        import scripts.release_validation as rv
        original_root = rv.REPO_ROOT
        rv.REPO_ROOT = tmp_path

        try:
            rv._results.clear()
            os.environ["RELEASE_VALIDATION_MODE"] = "1"
            result = rv.check_clean_working_tree()
            assert result is False
        finally:
            del os.environ["RELEASE_VALIDATION_MODE"]
            rv.REPO_ROOT = original_root


class TestDependencyHelpers:
    """Tests for dependency metadata helper functions."""

    def test_requirement_name_normalization(self):
        """Test PEP 503 requirement name normalization."""
        assert _requirement_name("psycopg[binary]>=3.0") == "psycopg"
        assert _requirement_name("mysql-connector-python>=8.0") == "mysql-connector-python"
        assert _requirement_name("azure-storage-blob>=12.0") == "azure-storage-blob"
        assert _requirement_name("Azure_Storage_Blob>=12.0") == "azure-storage-blob"
        assert _requirement_name("azure.storage.blob>=12.0") == "azure-storage-blob"

    def test_declared_floor_extraction(self):
        """Test lower bound extraction from requirement spec."""
        assert _declared_floor("psycopg[binary]>=3.0") == "3.0"
        assert _declared_floor("mysql-connector-python>=8.0") == "8.0"
        assert _declared_floor("requests>=2.0") == "2.0"
        assert _declared_floor("rich>=13.0") == "13.0"
        # No lower bound
        assert _declared_floor("psycopg") is None
        assert _declared_floor("psycopg==3.0") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])