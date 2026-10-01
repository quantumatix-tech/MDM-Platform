"""Runtime prerequisite validation for the Migration Platform.

Validates the Python runtime against the minimum declared in
``pyproject.toml`` (``requires-python``). The floor is read from installed
package metadata so ``pyproject.toml`` remains the single source of truth;
there is no hardcoded copy of the version number in this module.

This check exists because ``requires-python`` is enforced by pip at install
time only. It does not protect an already-installed interpreter that was later
downgraded, and it does not run when Python starts the application directly.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as _pkg_metadata

DIST_NAME = "migration-platform"


def required_python() -> str:
    """Return the ``Requires-Python`` floor declared by the distribution.

    Returns an empty string when the package is not installed, in which case no
    floor can be enforced.
    """
    try:
        return _pkg_metadata(DIST_NAME).get("Requires-Python") or ""
    except PackageNotFoundError:
        return ""


def _parse_floor(spec: str) -> tuple[int, int]:
    """Parse the minimum ``(major, minor)`` from a ``Requires-Python`` spec.

    Handles the common forms (``>=3.11``, ``>=3.11,<4``, ``~=3.11``). Returns
    ``(0, 0)`` when no lower bound can be determined, so an unrecognised spec
    disables the check instead of failing closed on a valid interpreter.
    """
    for part in spec.split(","):
        part = part.strip()
        for op in (">=", "~="):
            if part.startswith(op):
                digits = "".join(c for c in part[len(op) :] if c.isdigit() or c == ".")
                pieces = digits.split(".")
                try:
                    major = int(pieces[0])
                    minor = int(pieces[1]) if len(pieces) > 1 else 0
                except (ValueError, IndexError):
                    return (0, 0)
                return (major, minor)
    return (0, 0)


def check_python_version() -> tuple[bool, str]:
    """Validate the running interpreter.

    Returns ``(ok, message)``. The message is actionable and safe to print:
    it contains no paths, hostnames, or credentials.
    """
    spec = required_python()
    if not spec:
        return (
            True,
            (
                "Python floor not enforced: migration-platform is not installed "
                "(no package metadata)."
            ),
        )
    floor = _parse_floor(spec)
    if floor == (0, 0):
        return (True, f"Python floor not enforced: unrecognised Requires-Python {spec!r}.")
    found = (sys.version_info.major, sys.version_info.minor)
    if found < floor:
        return (
            False,
            (
                "Migration Platform requires Python "
                f"{floor[0]}.{floor[1]} or newer.\n"
                f"  EXPECTED: Python >= {floor[0]}.{floor[1]}\n"
                f"  FOUND:    Python {found[0]}.{found[1]} "
                f"({sys.version.split()[0]})\n"
                f"  ACTION:   Upgrade Python, then reinstall: pip install -e ."
            ),
        )
    return (
        True,
        f"Python {found[0]}.{found[1]} satisfies the >= {floor[0]}.{floor[1]} requirement.",
    )


def assert_python_version() -> None:
    """Raise ``RuntimeError`` with an actionable message if the floor is unmet."""
    ok, message = check_python_version()
    if not ok:
        raise RuntimeError(message)
