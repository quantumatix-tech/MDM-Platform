"""Read-only Oracle E2E owner and fixture readiness preflight."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.oracle.config import (
    connect,
    connection_config,
    resolver_for_side,
    validated_oracle_context,
)
from tests.e2e.oracle.owner_provisioning import _fixture_privileges, _side_policy, preflight_owner


def run(cfg, *, driver):
    owners, services = validated_oracle_context(cfg)
    required = _fixture_privileges(cfg)
    result = {}
    opened = []
    try:
        for side in ("source", "target"):
            policy = _side_policy(cfg, side, provisioning=False)
            preloaded_source = (
                side == "source"
                and cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "preloaded"
            )
            endpoint = connection_config(cfg, side, owners[side], resolver_for_side(cfg.raw, side))
            conn = connect(endpoint, driver)
            opened.append(conn)
            result[side] = preflight_owner(
                conn,
                owner=owners[side],
                service=services[side],
                container=policy["expected_container_name"],
                required=required,
                tablespace=policy.get("permanent_tablespace"),
                quota_bytes=policy.get("quota_bytes"),
                require_quota=not preloaded_source,
                require_empty=(
                    side == "target"
                    or cfg.raw.get("e2e", {}).get("source_mode", "fixtures") == "fixtures"
                ),
            )
        result["source_fixture_prerequisites"] = "PASS"
        result["target_empty"] = result["target"]["object_count"] == 0
        return result
    finally:
        for conn in opened:
            with suppress(Exception):
                conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Oracle E2E owner preflight")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        import oracledb

        result = run(load_e2e_config(args.config), driver=oracledb)
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports only actionable failure text
        print(f"[ORACLE PREFLIGHT] FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    for side in ("source", "target"):
        print(f"[ORACLE PREFLIGHT] {side.upper()} PASS: {result[side]}")
    print("[ORACLE PREFLIGHT] SOURCE fixture prerequisites PASS; TARGET empty PASS")


if __name__ == "__main__":
    main()
