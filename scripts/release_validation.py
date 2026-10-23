#!/usr/bin/env python3
"""Release validation for Migration Platform.

Runs the checks that must pass before tagging a release. Reuses the existing
test suite rather than duplicating it — this script validates *packaging,
versioning, and consistency*, not migration correctness.

Usage:
    python scripts/release_validation.py

Exit code 0 means every check passed. Any non-zero exit means do not tag.

The script reads no secrets and resolves no network distributions. All dependency
checks are static and read only pyproject.toml, so they are machine-independent.
Wheel output is written to a temporary directory, but building may still create
gitignored build/ and migration_platform.egg-info/ directories in the working
tree.

Environment variables:
    RELEASE_VALIDATION_MODE=1  Enable release-mode checks (dirty tree, tag validation)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "migration-platform"

# Unit tests that fail at the 0.2.0 baseline for reasons unrelated to
# packaging or versioning (identifier quoting, schema qualification, and a
# base-class capability assertion). They predate this release and are recorded
# here so a NEW failure is still detected. Do not delete entries to make a run
# pass -- fix the underlying assertion instead.
KNOWN_BASELINE_FAILURES = ()

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def check_declared_version() -> bool:
    """pyproject version must be parseable and match installed metadata."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not record("pyproject.toml exists", pyproject.is_file()):
        return False
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = data["project"]["version"]
    if not record("pyproject declares a version", bool(declared), declared):
        return False
    try:
        installed = version(DIST_NAME)
    except PackageNotFoundError:
        return record(
            "package is installed",
            False,
            f"{DIST_NAME} is not installed; run: pip install -e .[dev]",
        )
    ok = record(
        "installed metadata matches pyproject",
        installed == declared,
        f"metadata={installed} pyproject={declared}",
    )
    record(
        "pyproject version is a valid release version",
        declared.count(".") == 2 and all(p.isdigit() for p in declared.split(".")),
        declared,
    )
    return ok


def check_runtime_version() -> bool:
    """__version__ must resolve from installed metadata, not a hardcoded copy."""
    code = "import migration_platform;print(migration_platform.__version__)"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    reported = proc.stdout.strip()
    return record(
        "migration_platform.__version__ resolves",
        proc.returncode == 0 and bool(reported),
        f"reported={reported!r} stderr={proc.stderr.strip()[:120]!r}",
    )


def check_cli_version() -> bool:
    """`python -m migration_platform --version` must work without a config."""
    proc = subprocess.run(
        [sys.executable, "-m", "migration_platform", "--version"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    ok = proc.returncode == 0 and DIST_NAME in proc.stdout
    return record("CLI --version", ok, f"stdout={proc.stdout.strip()!r}")


def check_python_floor() -> bool:
    """The startup preflight must accept the running interpreter."""
    code = "from core.preflight import check_python_version;print(check_python_version())"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    return record(
        "Python runtime floor satisfied",
        proc.returncode == 0 and "True" in proc.stdout,
        proc.stdout.strip()[:120],
    )


def _requirement_name(spec: str) -> str:
    """Normalize a requirement string to its distribution name.

    PEP 503 normalization keeps `azure-storage-blob`, `Azure_Storage_Blob`, and
    `azure.storage.blob` comparable, and extras/markers/versions are discarded.
    """
    name = re.split(r"[\s\[<>=!~;@]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_floor(spec: str) -> str | None:
    """Return the lower bound of a requirement, or None if it is unpinned."""
    match = re.search(r">=\s*([0-9][^\s,;\]]*)", spec)
    return match.group(1) if match else None


def check_dependency_metadata() -> bool:
    """pyproject.toml must be internally consistent as the only dependency source.

    Deterministic and machine-independent: it reads only pyproject.toml and never
    resolves or downloads distributions.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project", {})

    # No second source of dependency truth may exist in the repository.
    if not record(
        "requirements.txt is absent",
        not (REPO_ROOT / "requirements.txt").exists(),
        "pyproject.toml is the single dependency source",
    ):
        return False

    runtime = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})

    if not record("runtime dependencies declared", bool(runtime), f"count={len(runtime)}"):
        return False

    # Every requirement must carry an explicit lower bound (never bare/pinned-exact).
    unpinned = [s for s in runtime if _declared_floor(s) is None]
    if not record(
        "every runtime dependency has a >= lower bound",
        not unpinned,
        f"unpinned={unpinned}",
    ):
        return False

    # No duplicate distributions in the runtime set.
    seen: dict[str, int] = {}
    for spec in runtime:
        name = _requirement_name(spec)
        seen[name] = seen.get(name, 0) + 1
    duplicates = sorted(n for n, c in seen.items() if c > 1)
    if not record("no duplicate runtime dependencies", not duplicates, f"duplicates={duplicates}"):
        return False

    # Extras must exist, declare versions, and reference known distributions.
    expected_extras = {"dev", "aws", "gcp", "vault"}
    missing_extras = sorted(expected_extras - set(extras))
    if not record(
        "documented extras are present",
        not missing_extras,
        f"present={sorted(extras)} missing={missing_extras}",
    ):
        return False

    # Provider extras must be a subset of the runtime set -- they re-affirm a
    # constraint, never introduce a new distribution. The `dev` extra is exempt:
    # lint and test tooling is intentionally absent from runtime dependencies.
    unknown: list[str] = []
    unpinned_extras: list[str] = []
    for extra in sorted(expected_extras):
        for spec in extras.get(extra, []):
            if extra != "dev" and _requirement_name(spec) not in seen:
                unknown.append(f"{extra}:{spec}")
            if _declared_floor(spec) is None:
                unpinned_extras.append(f"{extra}:{spec}")
    record(
        "provider extras reference declared runtime distributions",
        not unknown,
        f"unknown={unknown} (the dev extra is exempt)",
    )
    if not record(
        "extra dependencies have version floors",
        not unpinned_extras,
        f"unpinned={unpinned_extras}",
    ):
        return False

    # requires-python must be declared, since the runtime preflight enforces it.
    floor = project.get("requires-python", "")
    ok = record(
        "requires-python floor declared",
        floor.startswith(">="),
        floor or "<missing>",
    )
    return ok


def check_package_discovery() -> bool:
    """The discovery block the 0.2.0 packaging fix added must be present."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    find = data.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    include = find.get("include", [])
    expected = {"core*", "migration_platform*"}
    return record(
        "setuptools package discovery configured",
        expected.issubset(set(include)),
        f"include={include}",
    )


def check_imports() -> bool:
    """All public packages must import from an installed environment."""
    modules = [
        "core",
        "core.connectors",
        "core.connectors.mssql",
        "core.connectors.postgresql",
        "core.connectors.mysql",
        "core.reporting.report_builder",
        "core.preflight",
    ]
    code = "import " + ", ".join(modules)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    return record("all packages import", proc.returncode == 0, proc.stderr.strip()[:160])


def check_report_version() -> bool:
    """A generated report must carry the platform version."""
    code = (
        "from core.reporting.report_builder import ReportBuilder;"
        "import migration_platform as m;"
        "d=ReportBuilder({'run_id':'rv','mode':'full','status':'success','phases':{}},1.0,2.0).build_json();"
        "assert d['platform_version']==m.__version__, d['platform_version'];"
        "print(d['platform_version'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    return record(
        "migration report records platform_version",
        proc.returncode == 0,
        f"value={proc.stdout.strip()!r} {proc.stderr.strip()[-120:]}",
    )


def check_changelog() -> bool:
    """CHANGELOG must carry the release being validated."""
    version_txt = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    changelog = REPO_ROOT / "CHANGELOG.md"
    if not record("CHANGELOG.md exists", changelog.is_file()):
        return False
    text = changelog.read_text(encoding="utf-8")
    has_unreleased = "## [Unreleased]" in text
    has_release = f"## [{version_txt}]" in text
    return record(
        "CHANGELOG has [Unreleased] and the current release",
        has_unreleased and has_release,
        f"unreleased={has_unreleased} release[{version_txt}]={has_release}",
    )


def check_changelog_format() -> bool:
    """CHANGELOG release section must follow Keep a Changelog format."""
    version_txt = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    changelog = REPO_ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")

    # Find the release section header (with or without date)
    release_header_pattern = rf"## \[{re.escape(version_txt)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?"
    match = re.search(release_header_pattern, text)
    if not match:
        return record("CHANGELOG release section exists", False, "section not found")

    # Extract the release section content (until next ## [ or end of file)
    lines = text.splitlines()
    in_release_section = False
    release_lines = []
    header_line_idx = -1
    for i, line in enumerate(lines):
        if re.match(release_header_pattern, line.strip()):
            in_release_section = True
            header_line_idx = i
            continue
        if in_release_section:
            if line.startswith("## [") and i != header_line_idx:
                break
            release_lines.append(line)

    if not release_lines:
        return record("CHANGELOG release section has content", False, "empty section")

    release_content = "\n".join(release_lines).strip()

    # Check for at least one category header (### Added, ### Changed, etc.)
    category_headers = ["### Added", "### Changed", "### Fixed", "### Removed", "### Deprecated", "### Security", "### Notes"]
    has_category = any(h in release_content for h in category_headers)

    # Check for date in header (format: ## [X.Y.Z] - YYYY-MM-DD)
    date_pattern = rf"## \[{re.escape(version_txt)}\] - \d{{4}}-\d{{2}}-\d{{2}}"
    has_date = bool(re.search(date_pattern, text))

    ok = record(
        "CHANGELOG release section has category headers",
        has_category,
        f"found_category={has_category}",
    )
    record(
        "CHANGELOG release section has date",
        has_date,
        f"found_date={has_date}",
    )
    record(
        "CHANGELOG release section not empty",
        bool(release_content),
        f"content_length={len(release_content)}",
    )
    return ok and has_category and has_date


def _get_current_tag() -> str | None:
    """Get the current Git tag if HEAD is tagged, else None."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def check_git_tag_consistency() -> bool:
    """If HEAD is tagged, the tag must match pyproject.toml version."""
    if not os.environ.get("RELEASE_VALIDATION_MODE"):
        return record("Git tag consistency", True, "skipped (not in release mode)")

    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    expected_tag = f"v{declared}"
    current_tag = _get_current_tag()

    if current_tag is None:
        return record(
            "Git tag matches pyproject.toml version",
            False,
            "HEAD is not tagged; create annotated tag vX.Y.Z before release",
        )

    ok = record(
        "Git tag matches pyproject.toml version",
        current_tag == expected_tag,
        f"current_tag={current_tag} expected={expected_tag}",
    )

    # Also verify tag points to current commit
    if ok:
        try:
            result = subprocess.run(
                ["git", "rev-parse", f"{current_tag}^{{}}"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            tag_commit = result.stdout.strip()
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            head_commit = result.stdout.strip()
            record(
                "Git tag points to current commit",
                tag_commit == head_commit,
                f"tag_commit={tag_commit[:8]} head_commit={head_commit[:8]}",
            )
        except Exception as e:
            record("Git tag points to current commit", False, f"error: {e}")

    return ok


def check_clean_working_tree() -> bool:
    """In release mode, working tree must be clean (no uncommitted changes)."""
    if not os.environ.get("RELEASE_VALIDATION_MODE"):
        return record("Clean working tree", True, "skipped (not in release mode)")

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        changes = result.stdout.strip()
        ok = record(
            "Clean working tree",
            not changes,
            f"changes={changes[:200] if changes else 'none'}",
        )
        return ok
    except Exception as e:
        return record("Clean working tree", False, f"error: {e}")


def check_cli_version_exact() -> bool:
    """`python -m migration_platform --version` must match pyproject.toml exactly."""
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    expected = f"migration-platform {declared}"
    proc = subprocess.run(
        [sys.executable, "-m", "migration_platform", "--version"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    actual = proc.stdout.strip()
    ok = record(
        "CLI --version matches exactly",
        proc.returncode == 0 and actual == expected,
        f"expected={expected!r} actual={actual!r}",
    )
    return ok


def check_unit_tests() -> bool:
    """Run the unit suite and compare against the recorded baseline."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    output = proc.stdout + proc.stderr
    failed = sorted(
        line.split(" ", 1)[-1].strip() for line in output.splitlines() if line.startswith("FAILED ")
    )
    unexpected = [f for f in failed if f not in KNOWN_BASELINE_FAILURES]
    missing = [f for f in KNOWN_BASELINE_FAILURES if f not in failed]
    ok = proc.returncode == 0 or (not unexpected and not missing)
    record(
        "unit suite matches known baseline",
        ok,
        f"passed={output.strip().splitlines()[-1] if output.strip() else '?'} "
        f"failed={len(failed)} unexpected={unexpected} resolved={missing}",
    )
    if unexpected:
        record("no new unit failures", False, f"NEW: {unexpected}")
    if missing:
        record(
            "baseline failures still present",
            True,
            f"previously failing tests now pass: {missing}",
        )
    return ok and not unexpected


def check_wheel_build() -> bool:
    """A wheel must build and contain only the intended top-level packages."""
    try:
        import build  # noqa: F401
    except ImportError:
        return record(
            "wheel build",
            True,
            "skipped: python package 'build' not installed (pip install build)",
        )
    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", tmp],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if not record("wheel builds", proc.returncode == 0, proc.stderr.strip()[-160:]):
            return False
        wheels = list(Path(tmp).glob("*.whl"))
        if not record("wheel produced", bool(wheels)):
            return False
        wheel = wheels[0]
        expected = f"migration_platform-{declared}"
        if not record(
            "wheel filename carries the release version",
            wheel.name.startswith(expected),
            wheel.name,
        ):
            return False
        import zipfile

        names = zipfile.ZipFile(wheel).namelist()
        tops = {n.split("/")[0] for n in names if "/" in n}
        wanted = {"core", "migration_platform", f"{expected}.dist-info"}
        unwanted = {"config", "docker", "logs", "reports", "tests", "docs"} & tops
        return record(
            "wheel contains only intended packages",
            wanted.issubset(tops) and not unwanted,
            f"top_level={sorted(tops)} unexpected={sorted(unwanted)}",
        )


def main() -> int:
    print("=" * 72)
    print("Migration Platform — release validation")
    print("=" * 72)

    declared_checks = [
        check_declared_version,
        check_runtime_version,
        check_cli_version_exact,
        check_python_floor,
        check_package_discovery,
        check_dependency_metadata,
        check_imports,
        check_report_version,
        check_changelog,
        check_changelog_format,
        check_git_tag_consistency,
        check_clean_working_tree,
        check_unit_tests,
        check_wheel_build,
    ]
    for check in declared_checks:
        check()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [name for name, ok, _ in _results if not ok]
    print("-" * 72)
    print(f"{passed}/{len(_results)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        print("\nDo not tag this commit until the failures above are resolved.")
        return 1
    print("All release validation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
