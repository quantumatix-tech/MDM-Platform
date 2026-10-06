#!/usr/bin/env python3
"""
Deterministic generator for the compatibility matrix Markdown representation.

Reads docs/compatibility_matrix.yaml and produces a Markdown document.
Does not perform network calls or expose secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS = [
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

ALLOWED_STATUSES = {"TESTED", "SUPPORTED", "UNVERIFIED", "UNSUPPORTED", "KNOWN_ISSUE"}
ALLOWED_FLOWS = {"local_to_local", "local_to_cloud", "cloud_to_local", "cloud_to_cloud"}
ALLOWED_DEPLOYMENTS = {"local", "cloud"}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_matrix(data: dict[str, Any]) -> list[str]:
    """Validate the compatibility matrix structure. Returns list of errors."""
    errors = []

    if "compatibility_matrix" not in data:
        errors.append("Root key 'compatibility_matrix' missing")
        return errors

    matrix = data["compatibility_matrix"]

    if "application_version" not in matrix:
        errors.append("Missing 'application_version' in compatibility_matrix")
    if "entries" not in matrix:
        errors.append("Missing 'entries' in compatibility_matrix")
        return errors

    seen_keys = set()
    for i, entry in enumerate(matrix["entries"]):
        for field in REQUIRED_FIELDS:
            if field not in entry:
                errors.append(f"Entry {i}: missing required field '{field}'")

        if entry.get("status") not in ALLOWED_STATUSES:
            errors.append(f"Entry {i}: invalid status '{entry.get('status')}'")

        if entry.get("flow") not in ALLOWED_FLOWS:
            errors.append(f"Entry {i}: invalid flow '{entry.get('flow')}'")

        if entry.get("source_deployment") not in ALLOWED_DEPLOYMENTS:
            errors.append(f"Entry {i}: invalid source_deployment '{entry.get('source_deployment')}'")

        if entry.get("target_deployment") not in ALLOWED_DEPLOYMENTS:
            errors.append(f"Entry {i}: invalid target_deployment '{entry.get('target_deployment')}'")

        if entry.get("status") in {"TESTED", "SUPPORTED"} and not entry.get("evidence", "").strip():
            errors.append(f"Entry {i}: evidence field must not be empty for TESTED/SUPPORTED entries")

        key = (
            entry.get("engine"),
            entry.get("source_version"),
            entry.get("target_version"),
            entry.get("source_deployment"),
            entry.get("target_deployment"),
            entry.get("flow"),
        )
        if key in seen_keys:
            errors.append(f"Entry {i}: duplicate entry for {key}")
        seen_keys.add(key)

    return errors


def sort_key(entry: dict[str, Any]) -> tuple:
    """Deterministic sort key for entries."""
    return (
        entry["engine"],
        entry["source_version"],
        entry["target_version"],
        entry["source_deployment"],
        entry["target_deployment"],
        entry["flow"],
    )


def generate_markdown(matrix: dict[str, Any]) -> str:
    """Generate Markdown representation of the compatibility matrix."""
    entries = sorted(matrix["entries"], key=sort_key)
    app_version = matrix.get("application_version", "unknown")
    recorded_at = matrix.get("recorded_at", "unknown")
    recorded_by = matrix.get("recorded_by", "unknown")

    lines = [
        "# Compatibility Matrix (Generated)",
        "",
        f"**Application Version:** {app_version}",
        f"**Source Recorded:** {recorded_at} by {recorded_by}",
        "",
        "---",
        "",
        "## Engine Compatibility Entries",
        "",
    ]

    current_engine = None
    for entry in entries:
        if entry["engine"] != current_engine:
            current_engine = entry["engine"]
            lines.append(f"### {current_engine.upper()}")
            lines.append("")

        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.append(f"| Source Version | {entry['source_version']} |")
        lines.append(f"| Target Version | {entry['target_version']} |")
        lines.append(f"| Source Deployment | {entry['source_deployment']} |")
        lines.append(f"| Target Deployment | {entry['target_deployment']} |")
        lines.append(f"| Flow | {entry['flow']} |")
        lines.append(f"| Driver Version | {entry['driver_version']} |")
        lines.append(f"| Python Version | {entry['python_version']} |")
        lines.append(f"| Status | **{entry['status']}** |")
        lines.append(f"| Validation Level | {entry['validation_level']} |")
        lines.append(f"| Evidence | {entry['evidence']} |")
        if entry["known_limitations"]:
            lines.append(f"| Known Limitations | {entry['known_limitations']} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Summary by Engine and Status")
    lines.append("")

    summary = Counter()
    for entry in entries:
        summary[(entry["engine"], entry["status"])] += 1

    lines.append("| Engine | Status | Count |")
    lines.append("|---|---|---|")
    for (engine, status), count in sorted(summary.items()):
        lines.append(f"| {engine} | {status} | {count} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*This document is generated from `docs/compatibility_matrix.yaml`.*")
    lines.append("*Do not edit directly; modify the YAML source and regenerate.*")

    return "\n".join(lines)


def compute_checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate compatibility matrix Markdown from YAML")
    parser.add_argument(
        "--input",
        default="docs/compatibility_matrix.yaml",
        help="Path to input YAML file",
    )
    parser.add_argument(
        "--output",
        help="Path to output Markdown file (default: stdout)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate the YAML, do not generate output",
    )
    parser.add_argument(
        "--checksum",
        action="store_true",
        help="Print content checksum for idempotency verification",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        return 1

    data = load_yaml(input_path)
    errors = validate_matrix(data)

    if errors:
        print("Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if args.validate_only:
        print("Validation passed")
        return 0

    markdown = generate_markdown(data["compatibility_matrix"])

    if args.checksum:
        print(compute_checksum(markdown))
        return 0

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(markdown, encoding="utf-8")
        print(f"Generated {output_path}")
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main())