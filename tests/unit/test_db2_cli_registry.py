from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from migration_platform import __main__
from core.connectors import DB2SourceConnector, DB2TargetConnector


def test_cli_registry_has_db2():
    assert 'db2' in __main__.SOURCE_CONNECTORS
    assert 'db2' in __main__.TARGET_CONNECTORS
    assert __main__.SOURCE_CONNECTORS['db2'] is DB2SourceConnector
    assert __main__.TARGET_CONNECTORS['db2'] is DB2TargetConnector
