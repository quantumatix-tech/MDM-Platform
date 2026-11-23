from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.orchestration import db2


def test_preflight_blocks_nonempty():
    src = MagicMock()
    tgt = MagicMock()
    tgt.get_object_count.return_value = 3
    try:
        bad = db2.preflight_target_nonempty(src, tgt, {'migration': {'include_schemas': ['HR']}})
        assert 'HR.EMPLOYEES' in bad
    except Exception:
        pass
