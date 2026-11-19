from unittest.mock import patch

from core.connectors.db2.source import DB2SourceConnector
from core.connectors.base import GrantDef


def _connector():
    c = DB2SourceConnector({"include_schemas": ["TEST14"], "source": {"connection": {}}})
    c._conn = object()
    return c


def test_list_grants_mapping():
    fake = [
        {"object_type": "SCHEMA", "object_name": "TEST14", "column": None, "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "CREATEIN", "grantable": True},
        {"object_type": "TABLE", "object_name": "EMPLOYEES", "column": None, "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "SELECT", "grantable": False},
        {"object_type": "COLUMN", "object_name": "EMPLOYEES", "column": "SALARY", "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "UPDATE", "grantable": False},
        {"object_type": "SEQUENCE", "object_name": "SEQ_EMP_ID", "column": None, "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "USAGE", "grantable": False},
        {"object_type": "ROUTINE", "object_name": "FN_DOUBLE", "column": None, "grantee": "DB2ADMIN", "grantee_type": "U", "privilege": "EXECUTE", "grantable": False},
    ]
    with patch("core.connectors.db2.objects.grant.discover_grants", return_value=fake):
        grants = _connector().list_grants()
    assert len(grants) == 5
    assert all(isinstance(g, GrantDef) for g in grants)
    assert grants[2].object_type == "COLUMN"
    assert grants[4].object_type == "FUNCTION"


def test_list_grants_ygn_filtering_delegated():
    # discover_grants already filters N; ensure no exception on empty
    with patch("core.connectors.db2.objects.grant.discover_grants", return_value=[]):
        assert _connector().list_grants() == []
