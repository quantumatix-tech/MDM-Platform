"""Checks for the official python-oracledb driver without a live database."""

from __future__ import annotations

import pytest

oracledb = pytest.importorskip("oracledb", reason="install project dependencies to test python-oracledb")


def test_oracledb_driver_is_available_and_reports_version():
    assert oracledb.__version__
    assert isinstance(oracledb.__version__, str)


def test_oracledb_starts_in_thin_mode_without_client_libraries():
    # Thin mode is the default and does not require Oracle Client libraries.
    assert oracledb.is_thin_mode() is True


def test_oracledb_exposes_connection_api_without_connecting():
    assert callable(oracledb.connect)
    assert callable(oracledb.makedsn)
