"""Explicit opt-in Oracle acceptance owner provisioning command."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tests.e2e.acceptance import load_e2e_config
from tests.e2e.oracle.owner_provisioning import provision


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit Oracle E2E owner provisioning")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required explicit authorization to create missing configured owners",
    )
    args = parser.parse_args()
    try:
        import oracledb

        result = provision(load_e2e_config(args.config), driver=oracledb, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 - CLI boundary withholds unsafe provider/driver details
        print(f"[ORACLE OWNER PROVISIONING] FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print("[ORACLE OWNER PROVISIONING] " + str(result))


if __name__ == "__main__":
    main()
