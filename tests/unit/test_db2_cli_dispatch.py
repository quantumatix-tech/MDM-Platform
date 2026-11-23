from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.orchestration import db2


def test_db2_dispatch():
    assert db2.should_use_db2_orchestration({'source': {'engine': 'db2'}, 'target': {'engine': 'db2'}}) is True
    assert db2.should_use_db2_orchestration({'source': {'engine': 'mssql'}, 'target': {'engine': 'mssql'}}) is False
