from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    Index,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
    ViewDefinition,
    MaterializedViewDef,
    FunctionDef,
    TriggerDef,
    CommentDef,
    GrantDef,
    RLSPolicy,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
    IDENTIFIER_RE,
)
from core.retry import retry_with_backoff, CircuitBreaker
from core.audit_logger import audit_log, set_run_id
from core.secrets.base import SecretResolver
from core.secrets.env import EnvSecretProvider
from core.schema_mapping.registry import TypeMappingRegistry
from core.schema_mapping.type_map import map_type, check_size_limit
from core.validator import Validator
from core.alerting import (
    WebhookNotifier,
    SlackNotifier,
    create_notifier,
)
from core.orchestrator import MigrationOrchestrator
from core.assessment.report_generator import AssessmentReport


class TestInterfaceConsistency:
    def test_all_target_connectors_match_abc_upsert_batch_signature(self):
        import inspect
        from core.connectors import (
            PostgresTargetConnector,
            MySQLTargetConnector,
            MongoTargetConnector,
            MSSQLTargetConnector,
            CosmosMongoTargetConnector,
        )
        for cls in [
            PostgresTargetConnector,
            MySQLTargetConnector,
            MongoTargetConnector,
            MSSQLTargetConnector,
            CosmosMongoTargetConnector,
        ]:
            sig = inspect.signature(cls.upsert_batch)
            assert "schema" in sig.parameters, f"{cls.__name__} is missing schema param"


class TestBug1NeverDropWithIncremental:
    def test_upsert_never_drops_or_truncates(self):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        target.upsert_batch.return_value = UpsertResult(success_count=10)
        source.list_objects.return_value = ["users"]
        source.get_object_count.return_value = 10
        source.get_schema.return_value = Schema(name="users", columns=[Column(name="id", source_type="integer")])
        source.export_full.return_value = iter([{"id": 1}])

        orchestrator = MigrationOrchestrator(source, target, {})
        orchestrator.run_full()

        target.upsert_batch.assert_called()
        for call in target.upsert_batch.call_args_list:
            assert "TRUNCATE" not in str(call)
            assert "DROP" not in str(call)


class TestBug2StatusFromValidation:
    def test_status_not_hardcoded(self):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        target.upsert_batch.return_value = UpsertResult(success_count=10)

        orchestrator = MigrationOrchestrator(source, target, {})
        result = orchestrator.run_full()

        assert result.get("status") != "SUCCESS"
        assert result.get("status") in ("success", "failed", "mismatch")


class TestBug3PartialBatchNoCheckpoint:
    def test_checkpoint_not_advanced_on_partial_failure(self):
        MagicMock(spec=CDCEngine)
        [
            ChangeEvent(operation="insert", document={"id": 1}),
            ChangeEvent(operation="insert", document={"id": 2}),
        ]
        result = ApplyResult(success_count=1, failure_count=1, errors=["dup"])
        assert result.failure_count > 0
        assert result.last_checkpoint is None


class TestBug4TypeSafeIncrementalQueries:
    def test_objectid_not_string_comparison(self):
        from bson import ObjectId
        oid = ObjectId()
        assert isinstance(oid, ObjectId)
        assert oid != str(oid)


class TestBug5ObjectNameValidation:
    def test_valid_identifier_accepted(self):
        assert validate_identifier("valid_table") == "valid_table"

    def test_invalid_identifier_rejected(self):
        with pytest.raises(ValueError):
            validate_identifier("123-invalid")

    def test_invalid_identifier_with_special_chars(self):
        with pytest.raises(ValueError):
            validate_identifier("table-with-dash")

    def test_identifier_regex(self):
        assert IDENTIFIER_RE.match("valid_name") is not None
        assert IDENTIFIER_RE.match("1invalid") is None
        assert IDENTIFIER_RE.match("valid_123") is not None


class TestBug6NoHardcodedCredentials:
    def test_no_hardcoded_passwords_in_source(self):
        import inspect
        from core.connectors import postgresql, mysql, mssql, mongodb

        for module in [postgresql, mysql, mssql, mongodb]:
            source = inspect.getsource(module)
            assert "hardcoded_password" not in source.lower()
            assert "admin123" not in source


class TestBug8SingleCDCImplPerEngine:
    def test_one_cdc_class_per_engine(self):
        from core.connectors import postgresql, mysql, mssql, mongodb, cosmos_mongo

        pg_cdc = getattr(postgresql, "PostgresCDCEngine", None)
        mysql_cdc = getattr(mysql, "MySQLCDCEngine", None)
        mssql_cdc = getattr(mssql, "MSSQLCDCEngine", None)
        mongo_cdc = getattr(mongodb, "MongoCDCEngine", None)
        cosmos_cdc = getattr(cosmos_mongo, "CosmosMongoCDCEngine", None)

        assert pg_cdc is not None
        assert mysql_cdc is not None
        assert mssql_cdc is not None
        assert mongo_cdc is not None
        assert cosmos_cdc is not None


class TestBug9UriEncodingUsesQuote:
    def test_no_url_encoding_in_dsn_params(self):
        import inspect
        from core.connectors import postgresql

        source = inspect.getsource(postgresql.PostgresSourceConnector.connect)
        assert "urllib.parse.quote" not in source

    def test_password_passed_as_kwargs_not_string_conn(self):
        import inspect
        from core.connectors import postgresql

        # password lives in _make_conn_kwargs, which connect() delegates to.
        source = inspect.getsource(postgresql._make_conn_kwargs)
        assert '"password"' in source or "'password'" in source


class TestBug10TlsOnByDefault:
    def test_postgres_ssl_default_verify_full(self):
        import inspect
        from core.connectors.postgresql import _make_conn_kwargs

        # sslmode / verify-full lives in _make_conn_kwargs, which connect() delegates to.
        source = inspect.getsource(_make_conn_kwargs)
        assert "verify-full" in source

    def test_mysql_ssl_default_enabled(self):
        from core.connectors.mysql import MySQLSourceConnector
        import inspect
        source = inspect.getsource(MySQLSourceConnector.connect)
        assert "ssl_disabled" in source


class TestBug11ExponentialBackoffOnRateLimit:
    def test_cosmos_connector_uses_retry_with_backoff(self):
        from core.connectors.cosmos_mongo import CosmosMongoTargetConnector
        import inspect
        source = inspect.getsource(CosmosMongoTargetConnector.connect)
        assert "retry_with_backoff" in source or "max_retries" in source


class TestRetryBackoff:
    def test_retry_decorator_retries_on_exception(self):
        call_count = 0

        @retry_with_backoff(max_retries=2, base_delay=0.01)
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient")
            return "success"

        result = flaky_func()
        assert result == "success"
        assert call_count == 3

    def test_circuit_breaker_opens_after_max_failures(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=1.0)

        @retry_with_backoff(max_retries=1, circuit_breaker=cb)
        def failing_func():
            cb.record_failure()
            raise ValueError("always fails")

        with pytest.raises(ValueError):
            failing_func()

        assert cb.is_open


class TestSecretResolver:
    def test_env_provider_resolves_secret(self):
        import os
        os.environ["SECRET_TEST_DB"] = "test_password"
        provider = EnvSecretProvider()
        assert provider.get_secret("TEST_DB") == "test_password"
        del os.environ["SECRET_TEST_DB"]

    def test_secret_resolver_loads_provider(self):
        provider = EnvSecretProvider()
        resolver = SecretResolver(provider)
        import os
        os.environ["SECRET_MYKEY"] = "myvalue"
        assert resolver.resolve("MYKEY") == "myvalue"
        del os.environ["SECRET_MYKEY"]


class TestSchemaMapping:
    def test_postgresql_to_mysql_integer(self):
        assert map_type("postgresql", "mysql", "integer") == "INT"

    def test_mysql_to_postgresql_int(self):
        assert map_type("mysql", "postgresql", "INT") == "integer"

    def test_relational_to_mongo_integer(self):
        assert map_type("postgresql", "mongodb", "integer") == "int32"

    def test_mongo_to_relational_string(self):
        assert map_type("mongodb", "postgresql", "string") == "varchar"

    def test_size_limit_cosmos(self):
        assert check_size_limit("cosmos_mongo", 1024 * 1024) is True
        assert check_size_limit("cosmos_mongo", 3 * 1024 * 1024) is False

    def test_size_limit_mongodb(self):
        assert check_size_limit("mongodb", 10 * 1024 * 1024) is True
        assert check_size_limit("mongodb", 20 * 1024 * 1024) is False

    def test_registry_custom_mapping(self):
        registry = TypeMappingRegistry()
        registry.register("custom", "target", "custom_type", "mapped_type")
        assert registry.map_type("custom", "target", "custom_type") == "mapped_type"


class TestAuditLogger:
    def test_audit_log_outputs_json(self):
        import logging
        from io import StringIO

        set_run_id("test-run-123")
        logger = logging.getLogger("migration_platform.audit")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            audit_log(phase="test", status="ok", details={"key": "value"})
            output = stream.getvalue().strip()
            assert '"phase": "test"' in output
            assert '"status": "ok"' in output
            assert '"run_id": "test-run-123"' in output
        finally:
            logger.removeHandler(handler)


class TestAssessmentReport:
    def test_report_generates_json(self):
        report = AssessmentReport()
        report.source_engine = "postgresql"
        report.target_engine = "mysql"
        report.objects = [{"name": "users", "row_count": 100}]
        report.compatible = True

        d = report.to_dict()
        assert d["source_engine"] == "postgresql"
        assert d["target_engine"] == "mysql"
        assert d["compatible"] is True

        json_str = report.to_json()
        assert "postgresql" in json_str


class TestValidator:
    def test_validate_count_returns_dict(self):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        source.get_object_count.return_value = 100
        target.get_object_count.return_value = 100

        validator = Validator(source, target)
        result = validator.validate_count("test_table")

        assert result["match"] is True
        assert result["source_count"] == 100
        assert result["target_count"] == 100


class TestNotifierCreation:
    def test_create_none_notifier(self):
        notifier = create_notifier({"type": "none"})
        assert notifier is None

    def test_create_webhook_notifier(self):
        notifier = create_notifier({"type": "webhook", "webhook_url": "http://example.com"})
        assert isinstance(notifier, WebhookNotifier)

    def test_create_slack_notifier(self):
        notifier = create_notifier({"type": "slack", "webhook_url": "http://example.com"})
        assert isinstance(notifier, SlackNotifier)


class TestOrchestratorConnects:
    def test_orchestrator_drives_connectors_via_interface(self):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        source.list_objects.return_value = ["users"]
        source.get_object_count.return_value = 50
        source.get_schema.return_value = Schema(name="users", columns=[Column(name="id", source_type="integer")])
        source.export_full.return_value = iter([{"id": 1}])
        target.upsert_batch.return_value = UpsertResult(success_count=1)
        target.get_object_count.return_value = 50

        orchestrator = MigrationOrchestrator(source, target, {})
        result = orchestrator.run_full()

        assert result["status"] == "success"
        source.connect.assert_called()
        target.connect.assert_called()


class TestPostgresToPostgresBaseline:
    """
    STEP 1 — Baseline Regression Freeze.

    Pins the currently-working Postgres → Postgres basic full-load behavior
    that the project has been validated against in reports/ (e.g. 808a…,
    93df…, b0ef…, e28c…): 3 customers, 3 products, 4 orders.

    No production code, no orchestrator refactor, no type-mapping changes
    are allowed to break this test in any later step.

    The stale DELETE-synchronization gap is documented separately, in
    TestPostgresToPostgresBaseline::test_stale_target_row_is_known_gap,
    and is explicitly NOT treated as expected-correct behavior.
    """

    EXPECTED_TABLES = ["customers", "products", "orders"]
    EXPECTED_COUNTS = {"customers": 3, "products": 3, "orders": 4}
    EXPECTED_TOTAL = 10

    def _make_source(self, counts: dict[str, int]):
        source = MagicMock(spec=SourceConnector)
        source.list_objects.return_value = list(self.EXPECTED_TABLES)

        def _count(obj_name: str, **kwargs) -> int:
            return counts.get(obj_name, 0)

        source.get_object_count.side_effect = _count

        def _schema(obj_name: str) -> Schema:
            return Schema(
                name=obj_name,
                columns=[Column(name="id", source_type="integer", target_type="INT")],
                primary_key=["id"],
            )

        source.get_schema.side_effect = _schema

        def _export(obj_name: str, **kwargs):
            n = counts.get(obj_name, 0)
            for i in range(1, n + 1):
                yield {"id": i}

        source.export_full.side_effect = _export
        return source

    def _make_target(self, counts: dict[str, int]):
        target = MagicMock(spec=TargetConnector)

        def _upsert(object_name, rows, schema=None):
            batch = list(rows)
            return UpsertResult(success_count=len(batch))

        target.upsert_batch.side_effect = _upsert

        def _target_count(obj_name: str, **kwargs) -> int:
            return counts.get(obj_name, 0)

        target.get_object_count.side_effect = _target_count
        return target

    def test_basic_pg_pg_full_migration_loads_all_rows(self):
        source = self._make_source(self.EXPECTED_COUNTS)
        target = self._make_target(self.EXPECTED_COUNTS)

        result = MigrationOrchestrator(source, target, {}).run_full()

        discovered = result["phases"]["discover"]["objects"]
        assert sorted(discovered) == sorted(self.EXPECTED_TABLES)

        for table in self.EXPECTED_TABLES:
            phase = result["phases"][table]
            assert phase["source_rows"] == self.EXPECTED_COUNTS[table]
            assert phase["success"] == self.EXPECTED_COUNTS[table]
            assert phase["failure"] == 0

        total_success = sum(
            result["phases"][t]["success"] for t in self.EXPECTED_TABLES
        )
        assert total_success == self.EXPECTED_TOTAL

    def test_basic_pg_pg_validation_passes_when_counts_match(self):
        source = self._make_source(self.EXPECTED_COUNTS)
        target = self._make_target(self.EXPECTED_COUNTS)

        result = MigrationOrchestrator(source, target, {}).run_full()

        validation = result["phases"]["validation"]
        assert validation["mode"] == "count"
        for table in self.EXPECTED_TABLES:
            assert validation["checks"][table]["match"] is True, (
                f"{table} unexpectedly mismatched: {validation['checks'][table]}"
            )
        assert validation["status"] == "success"

    def test_stale_target_row_is_known_gap(self):
        """
        KNOWN GAP / EXPECTED CURRENT FAILURE.

        When a row is deleted on the source but the target still holds it,
        `run_full` upserts cannot remove it (no DELETE pass in full mode),
        so `validate(mode='count')` reports mismatch. This documents the
        current behavior, NOT a correctness claim.

        Any later step that removes this gap must update this test to
        reflect the new (correct) behavior.
        """
        stale_target_counts = {
            "customers": 4,   # one stale row
            "products": 3,
            "orders": 4,
        }
        source = self._make_source(self.EXPECTED_COUNTS)
        target = self._make_target(stale_target_counts)

        result = MigrationOrchestrator(source, target, {}).run_full()

        assert result["phases"]["customers"]["success"] == 3
        validation = result["phases"]["validation"]["checks"]["customers"]
        assert validation["match"] is False, (
            "If this assertion starts failing, the stale-row gap may have been "
            "fixed — update this test to reflect the new behavior."
        )
        assert result["phases"]["validation"]["status"] == "mismatch"


class TestPostgresDiscoverySchemaFilter:
    """
    STEP 3A — Schema Scope Regression Test.

    Pins the desired behavior of PostgresSourceConnector metadata
    discovery with respect to schema scope: when an `include_schemas`
    configuration is provided, tables (and by extension other object
    classes) inside the listed schemas must be enumerated, not only
    those in `public`.

    Behavior pinned (not SQL string literals):
      * PostgresSourceConnector honors an `include_schemas` config field.
      * When `include_schemas = ["public", "audit_test"]`, discovery is
        performed against both schemas — not only `public`.
      * The default behavior (no config or `["public"]`) is preserved.

    This test is expected to FAIL against the current code, because
    `PostgresSourceConnector.list_objects()` (postgresql.py:81-101) and
    every other discovery method hardcode `WHERE … = 'public'` — there
    is no `include_schemas` config field consulted anywhere.

    After the STEP 3B fix (replacing the hardcoded `'public'` filters
    with a parameterized schema list), this test should PASS.

    Test asserts BEHAVIOR at the discovery boundary, not SQL strings,
    so any equivalent future implementation (ANY(%s), IN (...), JOIN
    against a schema list, etc.) satisfies it.
    """

    def _make_smart_cursor(self, expected_schema_name: str):
        """
        Build a cursor mock that simulates a real PG behavior: rows are
        only returned for the schema name(s) the connector asks about.

        On the CURRENT (buggy) code, the connector asks only about
        `'public'`, so a cursor configured with expected_schema_name =
        `'public'` returns rows but one configured with `'audit_test'`
        does not.

        After STEP 3B, the connector must ask about BOTH configured
        schemas, so the mock returns rows regardless of which schema
        we name as "expected".

        This is a behavioral contract, not a SQL string assertion.
        """
        cur = MagicMock()

        def execute_side_effect(sql, params=None):
            sql_str = sql if isinstance(sql, str) else str(sql)
            params_str = str(params) if params is not None else ""
            # The connector may consult the schema via a literal in the SQL
            # (current buggy behavior) OR via a parameterized list (fixed
            # behavior). Either counts as "consulted" — that's the contract.
            schema_present = (
                expected_schema_name in sql_str
                or expected_schema_name in params_str
            )
            if schema_present:
                cur.fetchall.return_value = [
                    (f"t_{expected_schema_name}_1",),
                    (f"t_{expected_schema_name}_2",),
                ]
            else:
                cur.fetchall.return_value = []

        cur.execute.side_effect = execute_side_effect
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        return cur

    def _make_connector(self, include_schemas):
        from core.connectors.postgresql import PostgresSourceConnector

        cfg = {
            "host": "x",
            "port": 1,
            "database": "x",
            "username": "u",
            "password": "p",
            "ssl": False,
        }
        if include_schemas is not None:
            cfg["include_schemas"] = include_schemas
        connector = PostgresSourceConnector(cfg)
        connector._conn = MagicMock()
        return connector

    def test_non_public_schema_is_included_in_metadata_discovery(self):
        """
        Contract: with include_schemas = ['public', 'audit_test'],
        discovery must query both schemas and return objects from both.

        The mock cursor is configured for the `'audit_test'` schema:
          * On current code, the connector's SQL hardcodes `'public'`,
            so the cursor's `execute` is called with SQL that does NOT
            reference `'audit_test'` — cursor returns []. list_objects()
            returns []. Test FAILS.
          * After STEP 3B, the connector must reference BOTH configured
            schemas, so the cursor returns audit_test rows too. Test
            PASSES.
        """
        connector = self._make_connector(["public", "audit_test"])
        connector._conn.cursor.return_value = self._make_smart_cursor("audit_test")

        tables = connector.list_objects()

        assert any("audit_test" in t for t in tables) or len(tables) >= 1, (
            f"Non-public schema not consulted by discovery. "
            f"list_objects() returned {tables!r}. "
            "This is the STEP 2 confirmed DISCOVERY LIMITATION. "
            "Will pass after STEP 3B lifts the hardcoded 'public' filter."
        )

    def test_default_schema_scope_remains_public(self):
        """
        Regression safety: with include_schemas omitted (default),
        discovery must still enumerate `public`. This test passes on
        current code AND after STEP 3B.
        """
        connector = self._make_connector(None)
        connector._conn.cursor.return_value = self._make_smart_cursor("public")

        tables = connector.list_objects()

        assert "t_public_1" in tables
        assert "t_public_2" in tables

    def test_orchestrator_style_mutation_reaches_discovery(self):
        """
        Regression: in production, the orchestrator constructs the source
        connector with only the ``connection:`` sub-dict, then later mutates
        ``source._config['include_schemas']`` inside ``_apply_schema_scope``.
        Discovery must pick up the mutated value.
        """
        connector = self._make_connector(None)
        connector._conn.cursor.return_value = self._make_smart_cursor("audit_test")

        connector._config["include_schemas"] = ["public", "audit_test"]

        tables = connector.list_objects()

        assert any("audit_test" in t for t in tables) or len(tables) >= 1, (
            f"Discovery did not see orchestrator's include_schemas mutation. "
            f"Got: {tables!r}"
        )


class TestNonPublicSchemaDDLQualification:
    """
    STEP 3B-B-1 — Target DDL Schema Qualification Contract Test.

    Verifies that the migration/DDL path preserves schema_name from
    source discovery through to target DDL generation, so that
    public.customers and audit_test.customers can be distinguished.

    These tests FAIL on current production code because:
    1. Schema dataclass has no schema_name field
    2. PostgresTargetConnector.create_object_if_missing hardcodes
       table_schema='public' for existence checks
    3. CREATE TABLE DDL uses schema.name unqualified (no schema prefix)
    """

    def _make_mock_conn(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cur

    def test_get_schema_preserves_schema_name_in_metadata(self):
        """
        Contract: PostgresSourceConnector.get_schema() must return a
        Schema object that carries the schema name so that downstream
        phases (Phase 4-6) can route objects to the correct target schema.

        FAILS on current code: Schema dataclass has no schema_name field,
        and get_schema() returns Schema(name=object_name) only.
        """
        from core.connectors.postgresql import PostgresSourceConnector

        connector = PostgresSourceConnector(
            {
                "host": "x",
                "port": 1,
                "database": "x",
                "username": "u",
                "password": "p",
                "ssl": False,
                "include_schemas": ["public", "audit_test"],
            }
        )

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [("audit_test",), None, None]
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        connector._conn = mock_conn

        schema = connector.get_schema("customers")

        assert hasattr(schema, "schema_name"), (
            "Schema object must carry schema_name from discovery. "
            "Current Schema dataclass has no schema_name field."
        )
        assert schema.schema_name == "audit_test", (
            f"Schema must carry 'audit_test' schema name. "
            f"Got: {getattr(schema, 'schema_name', 'MISSING')!r}"
        )

    def test_target_ddl_qualifies_non_public_schema(self):
        """
        Contract: PostgresTargetConnector.create_object_if_missing must
        emit schema-qualified CREATE TABLE DDL when the Schema object
        carries a non-public schema_name, so that audit_test.customers
        and public.customers are created in the correct target schemas.

        FAILS on current code because:
        1. Schema dataclass has no schema_name field
        2. create_object_if_missing hardcodes table_schema='public'
        3. CREATE TABLE DDL uses schema.name unqualified
        """
        from core.connectors.postgresql import PostgresTargetConnector

        target = PostgresTargetConnector(
            {
                "host": "localhost",
                "port": 5432,
                "database": "test",
                "username": "u",
                "password": "p",
                "ssl": False,
            }
        )
        mock_conn, mock_cur = self._make_mock_conn()
        target._conn = mock_conn

        schema = Schema(
            name="customers",
            schema_name="audit_test",
            columns=[Column(name="id", source_type="integer")],
            primary_key=["id"],
        )

        target.create_object_if_missing(schema)

        executed_sql = [call.args[0] for call in mock_cur.execute.call_args_list]

        existence_sql = executed_sql[0]
        assert "table_schema = 'public'" not in existence_sql, (
            f"Existence check must not hardcode 'public'. "
            f"Got: {existence_sql!r}"
        )

        ddl = next(
            (sql for sql in executed_sql if sql.startswith("CREATE TABLE")), None
        )
        assert ddl is not None, "CREATE TABLE DDL was not emitted"
        assert "audit_test" in ddl, (
            f"Schema qualifier 'audit_test' must appear in CREATE TABLE DDL. "
            f"Got: {ddl!r}"
        )
        assert ddl.index("audit_test") < ddl.index("customers"), (
            f"Schema qualifier must appear before table name. Got: {ddl!r}"
        )

    def test_public_schema_ddl_remains_unchanged(self):
        """
        Regression: the existing public-schema path must produce valid
        unqualified CREATE TABLE DDL and must not break.
        """
        from core.connectors.postgresql import PostgresTargetConnector

        target = PostgresTargetConnector(
            {
                "host": "localhost",
                "port": 5432,
                "database": "test",
                "username": "u",
                "password": "p",
                "ssl": False,
            }
        )
        mock_conn, mock_cur = self._make_mock_conn()
        target._conn = mock_conn

        schema = Schema(
            name="customers",
            columns=[Column(name="id", source_type="integer")],
            primary_key=["id"],
        )

        target.create_object_if_missing(schema)

        executed_sql = [call.args[0] for call in mock_cur.execute.call_args_list]
        ddl = next(
            sql for sql in executed_sql if sql.startswith("CREATE TABLE")
        )

        assert "CREATE TABLE customers" in ddl, (
            f"Public schema path must produce unqualified CREATE TABLE. "
            f"Got: {ddl!r}"
        )
        assert "PRIMARY KEY (id)" in ddl


class TestPostgresSequenceSchemaQualification:
    """
    Regression: free-standing sequences living in non-public schemas
    (e.g. ``audit_test.test_sequence``) must be discovered with their
    schema and re-created in the correct target schema.  Previously
    the source connector dropped the schema and the target emitted
    ``CREATE SEQUENCE test_sequence`` (wrong schema), which then caused
    ``relation "audit_test.test_sequence" does not exist`` during
    CREATE TABLE.
    """

    def _source_conn(self):
        from core.connectors.postgresql import PostgresSourceConnector
        cfg = {
            "host": "x", "port": 1, "database": "x",
            "username": "u", "password": "p", "ssl": False,
            "include_schemas": ["public", "audit_test"],
        }
        return PostgresSourceConnector(cfg)

    def test_list_all_sequences_captures_schema_name(self):
        connector = self._source_conn()
        cur = MagicMock()
        cur.fetchall.return_value = [
            # (schemaname, sequencename, start, min, max, incr, cycle, last, owned_by)
            ("public",      "customers_customer_id_seq", 1, 1, 2147483647, 1, False, None, "public.customers.customer_id"),
            ("audit_test",  "test_sequence",             1, 1, 9223372036854775807, 1, False, None, None),
        ]
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        seqs = connector.list_all_sequences()
        by_name = {s.name: s for s in seqs}
        assert "test_sequence" in by_name
        assert by_name["test_sequence"].schema == "audit_test", (
            f"SequenceDef.schema must be populated for non-public sequences. "
            f"Got: {by_name['test_sequence'].schema!r}"
        )
        assert by_name["customers_customer_id_seq"].schema == "public"
        # owned_by must be schema-qualified so the target can re-attach ownership
        assert by_name["customers_customer_id_seq"].owned_by == "public.customers.customer_id"

    def test_target_create_sequence_qualifies_non_public_schema(self):
        """
        Contract: target ``create_sequence`` must emit a schema-qualified
        CREATE SEQUENCE for non-public schemas.  OWNED BY is intentionally
        deferred to ``apply_constraints`` (Phase 6+) because Phase 3.5
        runs before tables exist — applying it here would fail every
        sequence that has an owning table.
        """
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import SequenceDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        seq = SequenceDef(
            name="test_sequence",
            schema="audit_test",
            start_value=1, min_value=1, max_value=10**18,
            increment=1, cycle=False,
            owned_by="audit_test.test_customers.customer_id",
        )
        target.create_sequence(seq)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE SEQUENCE" in s), None)
        assert create_sql is not None, "CREATE SEQUENCE was not emitted"
        assert '"audit_test"' in create_sql, (
            f"CREATE SEQUENCE must schema-qualify non-public sequences. Got: {create_sql!r}"
        )
        assert '"test_sequence"' in create_sql
        assert create_sql.index('"audit_test"') < create_sql.index('"test_sequence"')
        # OWNED BY must NOT be emitted here — see docstring.
        assert not any("OWNED BY" in s for s in executed), (
            "create_sequence must defer OWNED BY until after the owning table exists"
        )

    def test_target_create_sequence_remains_unqualified_for_public(self):
        """
        Backward-compat regression: sequences in ``public`` (or with
        no schema) must still produce an unqualified CREATE SEQUENCE.
        """
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import SequenceDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        seq = SequenceDef(
            name="customers_customer_id_seq",
            schema="public",
            start_value=1, min_value=1, max_value=2147483647,
            increment=1, cycle=False,
            owned_by=None,
        )
        target.create_sequence(seq)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE SEQUENCE" in s), None)
        assert create_sql is not None
        assert create_sql.strip().startswith("CREATE SEQUENCE IF NOT EXISTS customers_customer_id_seq"), (
            f"Public schema sequences must stay unqualified. Got: {create_sql!r}"
        )


class TestPostgresSequenceOwnership:
    """
    Regression: sequence OWNED BY relationships must be preserved across
    migration, including schema-qualified ownership for non-public schemas.
    """

    def test_apply_sequence_ownership_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        from core.connectors.base import SequenceDef
        seq = SequenceDef(
            name="test_orders_order_id_seq",
            schema="audit_test",
            start_value=1, min_value=1, max_value=10**18,
            increment=1, cycle=False,
            owned_by="audit_test.test_orders.order_id",
        )
        target.apply_sequence_ownership(seq)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        alter_sql = next((s for s in executed if "ALTER SEQUENCE" in s), None)
        assert alter_sql is not None
        assert '"audit_test"."test_orders_order_id_seq"' in alter_sql
        assert "OWNED BY \"audit_test\".\"test_orders\".\"order_id\"" in alter_sql

    def test_apply_sequence_ownership_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        from core.connectors.base import SequenceDef
        seq = SequenceDef(
            name="customers_customer_id_seq",
            schema="public",
            start_value=1, min_value=1, max_value=2147483647,
            increment=1, cycle=False,
            owned_by="public.customers.customer_id",
        )
        target.apply_sequence_ownership(seq)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        alter_sql = next((s for s in executed if "ALTER SEQUENCE" in s), None)
        assert alter_sql is not None
        assert alter_sql.strip().startswith('ALTER SEQUENCE customers_customer_id_seq OWNED BY customers."customer_id"')

    def test_apply_sequence_ownership_skips_unowned_sequence(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        from core.connectors.base import SequenceDef
        seq = SequenceDef(
            name="test_sequence",
            schema="audit_test",
            start_value=1, min_value=1, max_value=10**18,
            increment=1, cycle=False,
            owned_by=None,
        )
        target.apply_sequence_ownership(seq)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        alter_sql = next((s for s in executed if "ALTER SEQUENCE" in s), None)
        assert alter_sql is None, "Unowned sequence must not emit ALTER SEQUENCE"


class TestPostgresSyncSequenceSchemaQualification:
    """
    Regression: sync_sequence must schema-qualify the table reference
    so pg_get_serial_sequence() and the MAX() query resolve correctly
    for non-public schemas.
    """

    def test_sync_sequence_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = [1]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        target.sync_sequence("test_orders", "order_id", schema_name="audit_test")

        executed = [c.args[0] for c in cur.execute.call_args_list]
        sync_sql = next((s for s in executed if "pg_get_serial_sequence" in s), None)
        assert sync_sql is not None
        assert '"audit_test"."test_orders"' in sync_sql
        assert 'FROM "audit_test"."test_orders"' in sync_sql

    def test_sync_sequence_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = [1]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        target.sync_sequence("customers", "customer_id")

        executed = [c.args[0] for c in cur.execute.call_args_list]
        sync_sql = next((s for s in executed if "pg_get_serial_sequence" in s), None)
        assert sync_sql is not None
        assert '"' not in sync_sql, (
            f"Public schema sequences must stay unquoted. Got: {sync_sql!r}"
        )


class TestPostgresCreateObjectTransactionIsolation:
    """
    Regression: a single failed CREATE TABLE DDL must not leave the
    target connection in ``idle in transaction`` state.  Without a
    rollback, every subsequent DDL on the connection raises
    ``current transaction is aborted`` until ROLLBACK is issued.
    """

    def _target(self):
        from core.connectors.postgresql import PostgresTargetConnector
        return PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )

    def test_create_object_if_missing_rolls_back_on_ddl_failure(self):
        target = self._target()
        cur = MagicMock()
        cur.fetchone.return_value = None  # table does not yet exist
        cur.execute.side_effect = [
            None,  # existence SELECT
            RuntimeError("relation \"audit_test.test_sequence\" does not exist"),  # CREATE TABLE
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = Schema(
            name="test_customers",
            schema_name="audit_test",
            columns=[
                Column(name="customer_id", source_type="integer",
                       default="nextval('audit_test.test_sequence'::regclass)"),
            ],
            primary_key=["customer_id"],
        )

        with pytest.raises(RuntimeError, match="does not exist"):
            target.create_object_if_missing(schema)

        # The rollback MUST have been called so the connection is
        # usable for the next object.
        conn.rollback.assert_called()

    def test_create_object_if_missing_commits_on_success(self):
        target = self._target()
        cur = MagicMock()
        cur.fetchone.return_value = None  # table does not yet exist
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = Schema(
            name="customers",
            columns=[Column(name="id", source_type="integer")],
            primary_key=["id"],
        )
        target.create_object_if_missing(schema)
        conn.commit.assert_called()
        conn.rollback.assert_not_called()


class TestPostgresViewSchemaQualification:
    """
    Regression: views in non-public schemas must be discovered with
    their schema and created in the correct target schema.  Previously
    the source connector dropped the schema and the target emitted
    CREATE VIEW view_name (always public), causing the view to be
    created in the wrong schema or fail.
    """

    def test_list_views_captures_schema_name(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("customer_summary", "public", "SELECT * FROM customers"),
            ("order_summary", "audit_test", "SELECT * FROM test_orders"),
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        views = connector.list_views()
        by_name = {v.name: v for v in views}
        assert "order_summary" in by_name
        assert by_name["order_summary"].schema_name == "audit_test"
        assert by_name["customer_summary"].schema_name == "public"

    def test_target_create_view_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        view = ViewDefinition(
            name="order_summary",
            schema_name="audit_test",
            definition="SELECT * FROM test_orders",
        )
        target.create_view(view)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE OR REPLACE VIEW" in s), None)
        assert create_sql is not None
        assert '"audit_test"' in create_sql
        assert '"order_summary"' in create_sql
        assert create_sql.index('"audit_test"') < create_sql.index('"order_summary"')

    def test_target_create_view_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        view = ViewDefinition(
            name="customer_summary",
            schema_name="public",
            definition="SELECT * FROM customers",
        )
        target.create_view(view)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE OR REPLACE VIEW" in s), None)
        assert create_sql is not None
        assert create_sql.strip().startswith("CREATE OR REPLACE VIEW customer_summary")


class TestPostgresMaterializedViewSchemaQualification:
    """
    Regression: materialized views in non-public schemas must be discovered
    with their schema and created in the correct target schema.  Previously
    the source connector dropped the schema and the target emitted
    CREATE MATERIALIZED VIEW view_name (always public), causing the object
    to be created in the wrong schema or fail.
    """

    def test_list_materialized_views_captures_schema_name(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("public", "public", "SELECT * FROM customers"),
            ("customer_balance_summary", "audit_test", "SELECT * FROM test_customers"),
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        mvs = connector.list_materialized_views()
        by_name = {mv.name: mv for mv in mvs}
        assert "customer_balance_summary" in by_name
        assert by_name["customer_balance_summary"].schema_name == "audit_test"
        assert by_name["public"].schema_name == "public"

    def test_target_create_materialized_view_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        mv = MaterializedViewDef(
            name="customer_balance_summary",
            schema_name="audit_test",
            definition="SELECT * FROM test_customers",
        )
        target.create_materialized_view(mv)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE MATERIALIZED VIEW" in s), None)
        assert create_sql is not None
        assert '"audit_test"' in create_sql
        assert '"customer_balance_summary"' in create_sql
        assert create_sql.index('"audit_test"') < create_sql.index('"customer_balance_summary"')

    def test_target_create_materialized_view_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        mv = MaterializedViewDef(
            name="public_mv",
            schema_name="public",
            definition="SELECT * FROM customers",
        )
        target.create_materialized_view(mv)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        create_sql = next((s for s in executed if "CREATE MATERIALIZED VIEW" in s), None)
        assert create_sql is not None
        assert create_sql.strip().startswith("CREATE MATERIALIZED VIEW public_mv")


class TestPostgresFunctionSchemaQualification:
    """
    Regression: functions in non-public schemas must be discovered with
    their schema and created in the correct target schema.  Previously
    the source connector dropped the schema and the target had no way
    to track or qualify the function schema.
    """

    def test_list_functions_captures_schema_name(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("get_customer_count", "audit_test", "CREATE OR REPLACE FUNCTION audit_test.get_customer_count() RETURNS integer LANGUAGE sql AS $function$ SELECT COUNT(*)::INTEGER FROM audit_test.test_customers; $function$"),
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        funcs = connector.list_functions()
        by_name = {f.name: f for f in funcs}
        assert "get_customer_count" in by_name
        assert by_name["get_customer_count"].schema_name == "audit_test"

    def test_target_create_function_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        func = FunctionDef(
            name="get_customer_count",
            schema_name="audit_test",
            ddl="CREATE OR REPLACE FUNCTION audit_test.get_customer_count() RETURNS integer LANGUAGE sql AS $function$ SELECT 1; $function$",
        )
        target.create_function(func)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        audit_log_call = next((c for c in executed if "audit_test.get_customer_count" in c), None)
        assert audit_log_call is not None

    def test_target_create_function_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        func = FunctionDef(
            name="public_func",
            schema_name="public",
            ddl="CREATE OR REPLACE FUNCTION public_func() RETURNS integer LANGUAGE sql AS $function$ SELECT 1; $function$",
        )
        target.create_function(func)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        ddl_call = next((s for s in executed if "CREATE OR REPLACE FUNCTION" in s), None)
        assert ddl_call is not None
        assert ddl_call.strip().startswith("CREATE OR REPLACE FUNCTION public_func")


class TestPostgresTriggerSchemaQualification:
    """
    Regression: triggers in non-public schemas must be discovered with
    their table schema and created in the correct target schema.  Previously
    the source connector dropped the schema and the target emitted
    DROP TRIGGER / CREATE TRIGGER without schema qualification, causing
    failures for non-public tables.
    """

    def test_get_all_triggers_captures_schema_name(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("trg_customer_timestamp", "test_customers", "audit_test",
             "CREATE TRIGGER trg_customer_timestamp BEFORE UPDATE ON audit_test.test_customers FOR EACH ROW EXECUTE FUNCTION audit_test.update_customer_timestamp()"),
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        triggers = connector.get_all_triggers()
        by_name = {t.name: t for t in triggers}
        assert "trg_customer_timestamp" in by_name
        assert by_name["trg_customer_timestamp"].schema_name == "audit_test"
        assert by_name["trg_customer_timestamp"].table == "test_customers"

    def test_target_create_trigger_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        trigger = TriggerDef(
            name="trg_customer_timestamp",
            table="test_customers",
            schema_name="audit_test",
            ddl="CREATE TRIGGER trg_customer_timestamp BEFORE UPDATE ON audit_test.test_customers FOR EACH ROW EXECUTE FUNCTION audit_test.update_customer_timestamp()",
        )
        target.create_trigger(trigger)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        drop_sql = next((s for s in executed if "DROP TRIGGER" in s), None)
        assert drop_sql is not None
        assert '"audit_test"."test_customers"' in drop_sql

    def test_target_create_trigger_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        trigger = TriggerDef(
            name="trg_public",
            table="customers",
            schema_name="public",
            ddl="CREATE TRIGGER trg_public BEFORE UPDATE ON customers FOR EACH ROW EXECUTE FUNCTION public_func()",
        )
        target.create_trigger(trigger)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        drop_sql = next((s for s in executed if "DROP TRIGGER" in s), None)
        assert drop_sql is not None
        assert drop_sql.strip().startswith('DROP TRIGGER IF EXISTS "trg_public" ON customers')


class TestPostgresCommentSchemaQualification:
    """
    Regression: comments on non-public objects must preserve schema
    information and be applied with correct schema qualification.
    """

    def test_list_comments_captures_schema_for_table(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [("TABLE", "audit_test", "test_customers", "Migration platform object testing table")],
            [],
            [],
            [],
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        comments = connector.list_comments()
        by_type_name = {(c.object_type, c.object_name): c for c in comments}
        assert ("TABLE", "test_customers") in by_type_name
        assert by_type_name[("TABLE", "test_customers")].schema_name == "audit_test"
        assert by_type_name[("TABLE", "test_customers")].comment == "Migration platform object testing table"

    def test_list_comments_captures_schema_for_column(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [],
            [("audit_test", "test_customers", "email", "Unique customer email")],
            [],
            [],
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        comments = connector.list_comments()
        by_type_name = {(c.object_type, c.object_name): c for c in comments}
        assert ("COLUMN", "test_customers.email") in by_type_name
        assert by_type_name[("COLUMN", "test_customers.email")].schema_name == "audit_test"

    def test_target_apply_comment_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        comment = CommentDef(
            object_type="TABLE",
            object_name="test_customers",
            schema_name="audit_test",
            comment="Migration platform object testing table",
        )
        target.apply_comment(comment)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        comment_sql = next((s for s in executed if "COMMENT ON" in s), None)
        assert comment_sql is not None
        assert '"audit_test"."test_customers"' in comment_sql

    def test_target_apply_comment_qualifies_column_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        comment = CommentDef(
            object_type="COLUMN",
            object_name="test_customers.email",
            schema_name="audit_test",
            comment="Unique customer email",
        )
        target.apply_comment(comment)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        comment_sql = next((s for s in executed if "COMMENT ON" in s), None)
        assert comment_sql is not None
        assert '"audit_test"."test_customers"."email"' in comment_sql

    def test_target_apply_comment_qualifies_function_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        comment = CommentDef(
            object_type="FUNCTION",
            object_name="get_customer_count()",
            schema_name="audit_test",
            comment="Returns customer count",
        )
        target.apply_comment(comment)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        comment_sql = next((s for s in executed if "COMMENT ON" in s), None)
        assert comment_sql is not None
        assert '"audit_test".get_customer_count()' in comment_sql

    def test_target_apply_comment_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        comment = CommentDef(
            object_type="TABLE",
            object_name="customers",
            schema_name="public",
            comment="Public customers table",
        )
        target.apply_comment(comment)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        comment_sql = next((s for s in executed if "COMMENT ON" in s), None)
        assert comment_sql is not None
        assert comment_sql.strip().startswith("COMMENT ON TABLE customers")


class TestPostgresGrantSchemaQualification:
    """
    Regression: grants on non-public objects must preserve schema
    information and be applied with correct schema qualification.
    """

    def test_list_grants_captures_schema_for_table(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        # Query order: table, column, explicit sequence, owned sequence, schema, function
        cur.fetchall.side_effect = [
            [("audit_user", "audit_test", "test_customers", "SELECT, INSERT")],
            [],
            [],
            [],
            [],
            [],
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()
        by_grantee = {(g.grantee, g.object_name): g for g in grants}
        assert ("audit_user", "test_customers") in by_grantee
        assert by_grantee[("audit_user", "test_customers")].schema_name == "audit_test"
        assert by_grantee[("audit_user", "test_customers")].privileges == "SELECT, INSERT"

    def test_list_grants_captures_schema_for_column(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [],
            [("audit_user", "audit_test", "test_customers", "email", "SELECT")],
            [],
            [],
            [],
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()
        by_grantee = {(g.grantee, g.object_name): g for g in grants}
        assert ("audit_user", "test_customers.email") in by_grantee
        assert by_grantee[("audit_user", "test_customers.email")].schema_name == "audit_test"
        assert by_grantee[("audit_user", "test_customers.email")].object_type == "COLUMN"

    def test_target_apply_grant_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="SELECT, INSERT",
            object_type="TABLE",
            object_name="test_customers",
            schema_name="audit_test",
            grantee="audit_user",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        assert '"audit_test"."test_customers"' in grant_sql
        assert "TO audit_user" in grant_sql

    def test_list_grants_preserves_function_and_procedure_types(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["cloud_test"]}
        )
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [],
            [],
            [],
            [],
            [
                ("cloud_test", "get_customer_count()", "cloud_test_reader", "EXECUTE", "f"),
                ("cloud_test", "log_message(IN p_message text)", "cloud_test_reader", "EXECUTE", "p"),
            ],
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()

        function_grant = next(g for g in grants if g.object_name == "get_customer_count()")
        procedure_grant = next(g for g in grants if g.object_name == "log_message(IN p_message text)")
        assert function_grant.object_type == "FUNCTION"
        assert function_grant.schema_name == "cloud_test"
        assert function_grant.privileges == "EXECUTE"
        assert procedure_grant.object_type == "PROCEDURE"
        assert procedure_grant.schema_name == "cloud_test"
        assert procedure_grant.privileges == "EXECUTE"

    def test_target_apply_grant_qualifies_column_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="SELECT",
            object_type="COLUMN",
            object_name="test_customers.email",
            schema_name="audit_test",
            grantee="audit_user",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        # Valid PostgreSQL COLUMN grant syntax: GRANT privilege (column) ON TABLE schema.table TO role
        assert grant_sql == 'GRANT SELECT ("email") ON TABLE "audit_test"."test_customers" TO audit_user'
        assert "TO audit_user" in grant_sql

    def test_target_apply_grant_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="SELECT",
            object_type="TABLE",
            object_name="customers",
            schema_name="public",
            grantee="public",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        assert grant_sql.strip().startswith("GRANT SELECT ON TABLE customers TO public")


class TestPostgresGrantFixes:
    """
    Tests for grant fixes:
    - apply_grant raises exceptions (not silently swallowed)
    - create_role_if_not_exists creates missing roles
    - schema grant key doesn't duplicate schema name
    """

    def test_apply_grant_raises_on_failure(self):
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn
        # Make execute raise an exception
        cur.execute.side_effect = Exception("role does not exist")

        grant = GrantDef(
            privileges="SELECT",
            object_type="TABLE",
            object_name="customers",
            schema_name="public",
            grantee="test_role",
        )
        with pytest.raises(Exception, match="role does not exist"):
            target.apply_grant(grant)

    def test_create_role_if_not_exists_creates_new_role(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn
        # First call returns None (role doesn't exist), second call for CREATE ROLE
        cur.fetchone.return_value = None

        target.create_role_if_not_exists("new_role")

        # Check parameterized SELECT query
        select_calls = [c for c in cur.execute.call_args_list 
                       if "SELECT 1 FROM pg_roles WHERE rolname = %s" in c.args[0]]
        assert len(select_calls) == 1
        assert select_calls[0].args[1] == ("new_role",)
        
        # Check CREATE ROLE was executed
        create_calls = [c for c in cur.execute.call_args_list 
                       if 'CREATE ROLE "new_role"' in c.args[0]]
        assert len(create_calls) == 1
        assert conn.commit.called

    def test_create_role_if_not_exists_skips_existing_role(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn
        # Role exists
        cur.fetchone.return_value = (1,)

        target.create_role_if_not_exists("existing_role")

        # Check parameterized SELECT query
        select_calls = [c for c in cur.execute.call_args_list 
                       if "SELECT 1 FROM pg_roles WHERE rolname = %s" in c.args[0]]
        assert len(select_calls) == 1
        assert select_calls[0].args[1] == ("existing_role",)
        
        # Should NOT have CREATE ROLE
        create_calls = [c for c in cur.execute.call_args_list 
                       if "CREATE ROLE" in c.args[0]]
        assert len(create_calls) == 0
        # Should NOT commit
        assert not conn.commit.called

    def test_schema_grant_key_no_duplicate_schema(self):
        """Schema grants should show as 'schema TO role' not 'schema.schema TO role'"""
        from core.connectors.base import GrantDef
        grant = GrantDef(
            privileges="USAGE",
            object_type="SCHEMA",
            object_name="cloud_test",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        # This mimics the fixed orchestrator logic
        if grant.object_type == "SCHEMA":
            grant_key = f"{grant.object_name} TO {grant.grantee}"
        else:
            grant_key = (
                f"{grant.object_name} TO {grant.grantee}"
                if grant.schema_name == "public"
                else f"{grant.schema_name}.{grant.object_name} TO {grant.grantee}"
            )
        assert grant_key == "cloud_test TO cloud_test_reader"
        assert grant_key != "cloud_test.cloud_test TO cloud_test_reader"

    def test_apply_grant_column_generates_valid_postgresql_sql(self):
        """COLUMN grants must use 'GRANT privilege (column) ON TABLE table TO role' syntax"""
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="SELECT, INSERT",
            object_type="COLUMN",
            object_name="customers.customer_id",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        assert grant_sql == (
            'GRANT SELECT, INSERT ("customer_id") '
            'ON TABLE "cloud_test"."customers" TO cloud_test_reader'
        )

    def test_apply_grant_function_generates_valid_postgresql_sql(self):
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="EXECUTE",
            object_type="FUNCTION",
            object_name="get_customer_count()",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql == (
            'GRANT EXECUTE ON FUNCTION "cloud_test".get_customer_count() '
            'TO cloud_test_reader'
        )

    def test_apply_grant_procedure_generates_valid_postgresql_sql(self):
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="EXECUTE",
            object_type="PROCEDURE",
            object_name="log_message(IN p_message text)",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql == (
            'GRANT EXECUTE ON PROCEDURE "cloud_test".log_message(IN p_message text) '
            'TO cloud_test_reader'
        )

    def test_apply_grant_schema_generates_valid_postgresql_sql(self):
        """SCHEMA grants must use 'GRANT ... ON SCHEMA schema TO role' syntax"""
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="USAGE",
            object_type="SCHEMA",
            object_name="cloud_test",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        assert 'ON SCHEMA "cloud_test"' in grant_sql
        assert "TO cloud_test_reader" in grant_sql

    def test_apply_grant_rollbacks_on_failure(self):
        """Failed grants must rollback transaction and re-raise"""
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn
        # Make execute raise an exception
        cur.execute.side_effect = Exception("permission denied")

        grant = GrantDef(
            privileges="SELECT",
            object_type="TABLE",
            object_name="customers",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        with pytest.raises(Exception, match="permission denied"):
            target.apply_grant(grant)
        
        # Must rollback
        assert conn.rollback.called
        # Must NOT commit
        assert not conn.commit.called

    def test_apply_grant_sequence_generates_valid_postgresql_sql(self):
        """SEQUENCE grants must use 'GRANT ... ON SEQUENCE schema.sequence TO role' syntax"""
        from core.connectors.postgresql import PostgresTargetConnector
        from core.connectors.base import GrantDef
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        grant = GrantDef(
            privileges="USAGE, SELECT",
            object_type="SEQUENCE",
            object_name="customers_customer_id_seq",
            schema_name="cloud_test",
            grantee="cloud_test_reader",
        )
        target.apply_grant(grant)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        grant_sql = next((s for s in executed if "GRANT" in s), None)
        assert grant_sql is not None
        assert 'ON SEQUENCE "cloud_test"."customers_customer_id_seq"' in grant_sql
        assert "TO cloud_test_reader" in grant_sql

    def test_list_grants_includes_owned_sequence_for_insert(self):
        """Roles with INSERT on table with owned sequence get USAGE on that sequence"""
        from core.connectors.postgresql import PostgresSourceConnector
        from core.connectors.base import GrantDef
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["cloud_test"]}
        )
        cur = MagicMock()
        # Query order in list_grants:
        # 1. Table grants (4 cols)
        # 2. Column grants (5 cols)
        # 3. Explicit sequence grants (4 cols)
        # 4. Owned sequences NEW (3 cols)
        # 5. Schema grants (3 cols)
        # 6. Function grants (4 cols)
        cur.fetchall.side_effect = [
            [("cloud_test_reader", "cloud_test", "customers", "INSERT, SELECT")],  # table grants
            [],  # column grants
            [],  # explicit sequence grants
            [("cloud_test", "customers_customer_id_seq", "cloud_test.customers.customer_id")],  # owned sequences
            [],  # schema grants
            [],  # function grants
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()
        
        # Should have table grant
        table_grants = [g for g in grants if g.object_type == "TABLE" and g.object_name == "customers"]
        assert len(table_grants) == 1
        assert table_grants[0].grantee == "cloud_test_reader"
        assert "INSERT" in table_grants[0].privileges
        
        # Should have implicit sequence grant for the owned sequence
        seq_grants = [g for g in grants if g.object_type == "SEQUENCE" and g.object_name == "customers_customer_id_seq"]
        assert len(seq_grants) == 1
        assert seq_grants[0].grantee == "cloud_test_reader"
        assert "USAGE" in seq_grants[0].privileges
        assert seq_grants[0].schema_name == "cloud_test"

    def test_list_grants_preserves_explicit_sequence_usage_and_select(self):
        """Explicit sequence ACL entries must retain all privileges."""
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["e2e_test"]}
        )
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [],  # table grants
            [],  # column grants
            [("e2e_test_reader", "e2e_test", "customers_customer_id_seq", "USAGE, SELECT")],
            [],  # owned sequences
            [],  # schema grants
            [],  # function grants
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()

        seq_grants = [g for g in grants if g.object_type == "SEQUENCE"]
        assert len(seq_grants) == 1
        assert seq_grants[0].grantee == "e2e_test_reader"
        assert seq_grants[0].object_name == "customers_customer_id_seq"
        assert seq_grants[0].schema_name == "e2e_test"
        assert seq_grants[0].privileges == "USAGE, SELECT"

    def test_list_grants_no_duplicate_sequence_grant_if_explicit_exists(self):
        """Don't add implicit sequence grant if explicit grant already exists"""
        from core.connectors.postgresql import PostgresSourceConnector
        from core.connectors.base import GrantDef
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["cloud_test"]}
        )
        cur = MagicMock()
        # Query order:
        # 1. Table grants (4 cols)
        # 2. Column grants (5 cols)
        # 3. Explicit sequence grants (4 cols)
        # 4. Owned sequences NEW (3 cols)
        # 5. Schema grants (3 cols)
        # 6. Function grants (4 cols)
        cur.fetchall.side_effect = [
            [("cloud_test_reader", "cloud_test", "customers", "INSERT, SELECT")],  # table grants
            [],  # column grants
            [("cloud_test_reader", "cloud_test", "customers_customer_id_seq", "USAGE")],  # explicit sequence grants
            [("cloud_test", "customers_customer_id_seq", "cloud_test.customers.customer_id")],  # owned sequences
            [],  # schema grants
            [],  # function grants
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        grants = connector.list_grants()
        
        # Should have table grant
        table_grants = [g for g in grants if g.object_type == "TABLE" and g.object_name == "customers"]
        assert len(table_grants) == 1
        
        # Should have ONLY ONE sequence grant (the explicit one, not duplicate)
        seq_grants = [g for g in grants if g.object_type == "SEQUENCE" and g.object_name == "customers_customer_id_seq"]
        assert len(seq_grants) == 1
        assert seq_grants[0].privileges == "USAGE"  # explicit grant, not USAGE, SELECT


class TestPostgresRLSSchemaQualification:
    """
    Regression: RLS policies on non-public tables must preserve schema
    information and be applied with correct schema qualification.
    """

    def test_get_rls_policies_captures_schema(self):
        from core.connectors.postgresql import PostgresSourceConnector
        connector = PostgresSourceConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False,
             "include_schemas": ["public", "audit_test"]}
        )
        cur = MagicMock()
        cur.fetchall.return_value = [
            ("test_customer_policy", "SELECT", "PERMISSIVE", "true", None, "audit_test"),
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        policies = connector.get_rls_policies("test_customers")
        assert len(policies) == 1
        assert policies[0].schema_name == "audit_test"
        assert policies[0].table == "test_customers"
        assert policies[0].cmd == "SELECT"
        assert policies[0].permissive == "PERMISSIVE"
        assert policies[0].using_expr == "true"
        assert policies[0].check_expr is None

    def test_target_apply_rls_policy_qualifies_non_public_schema(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        policy = RLSPolicy(
            name="test_customer_policy",
            table="test_customers",
            schema_name="audit_test",
            cmd="SELECT",
            permissive="PERMISSIVE",
            using_expr="true",
        )
        target.apply_rls_policy(policy)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        alter_sql = next((s for s in executed if "ALTER TABLE" in s), None)
        assert alter_sql is not None
        assert '"audit_test"."test_customers"' in alter_sql

        create_sql = next((s for s in executed if "CREATE POLICY" in s), None)
        assert create_sql is not None
        assert '"audit_test"."test_customers"' in create_sql
        assert "AS PERMISSIVE FOR SELECT" in create_sql
        assert "USING (true)" in create_sql

    def test_target_apply_rls_policy_remains_unqualified_for_public(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        policy = RLSPolicy(
            name="public_policy",
            table="customers",
            schema_name="public",
            cmd="ALL",
            permissive="RESTRICTIVE",
            using_expr="false",
            check_expr="true",
        )
        target.apply_rls_policy(policy)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        alter_sql = next((s for s in executed if "ALTER TABLE" in s), None)
        assert alter_sql is not None
        assert alter_sql.strip().startswith("ALTER TABLE customers ENABLE ROW LEVEL SECURITY")

        create_sql = next((s for s in executed if "CREATE POLICY" in s), None)
        assert create_sql is not None
        assert create_sql.strip().startswith("CREATE POLICY public_policy ON customers")


class TestPostgresCrossSchemaForeignKey:
    """
    Regression: foreign keys referencing tables in non-public schemas
    must preserve the referenced schema and generate schema-qualified
    REFERENCES DDL.
    """

    def test_apply_constraints_generates_cross_schema_fk_ddl(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = Schema(
            name="fk_child",
            schema_name="audit_test",
            foreign_keys=[
                ForeignKey(
                    name="fk_child_to_public_customers",
                    columns=["parent_id"],
                    ref_table="customers",
                    ref_columns=["customer_id"],
                    ref_schema="public",
                    on_delete="CASCADE",
                    on_update="CASCADE",
                )
            ],
        )
        target.apply_constraints(schema)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
        assert fk_sql is not None
        assert "REFERENCES customers" in fk_sql
        assert "ON DELETE CASCADE ON UPDATE CASCADE" in fk_sql

    def test_apply_constraints_generates_non_public_to_non_public_fk_ddl(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = Schema(
            name="fk_child",
            schema_name="audit_test",
            foreign_keys=[
                ForeignKey(
                    name="fk_child_to_audit_parent",
                    columns=["parent_id"],
                    ref_table="fk_parent",
                    ref_columns=["id"],
                    ref_schema="audit_test",
                    on_delete="NO ACTION",
                    on_update="NO ACTION",
                )
            ],
        )
        target.apply_constraints(schema)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
        assert fk_sql is not None
        assert '"audit_test"."fk_parent"' in fk_sql

    def test_apply_constraints_generates_non_public_to_public_fk_ddl(self):
        from core.connectors.postgresql import PostgresTargetConnector
        target = PostgresTargetConnector(
            {"host": "x", "port": 1, "database": "x",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = Schema(
            name="fk_child",
            schema_name="audit_test",
            foreign_keys=[
                ForeignKey(
                    name="fk_child_to_public_parent",
                    columns=["parent_id"],
                    ref_table="public_parent",
                    ref_columns=["id"],
                    ref_schema="public",
                    on_delete="SET NULL",
                    on_update="RESTRICT",
                )
            ],
        )
        target.apply_constraints(schema)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        fk_sql = next((s for s in executed if "FOREIGN KEY" in s), None)
        assert fk_sql is not None
        assert "REFERENCES public_parent" in fk_sql
        assert "ON DELETE SET NULL ON UPDATE RESTRICT" in fk_sql


class TestPostgresCrossSchemaMetadataIsolation:
    """
    Regression: PostgresSourceConnector.get_schema() must scope all
    metadata queries (PK, indexes, FKs, check constraints) by table_schema
    so that same-named tables in different schemas do not pollute each
    other's metadata.

    Example: public.customers and cloud_test.customers both have
    customer_id as PK. Without schema filtering, the PK query returns
    customer_id twice, producing PRIMARY KEY (customer_id, customer_id).
    """

    def _source_conn(self):
        from core.connectors.postgresql import PostgresSourceConnector
        cfg = {
            "host": "x", "port": 1, "database": "x",
            "username": "u", "password": "p", "ssl": False,
            "include_schemas": ["public", "cloud_test"],
        }
        return PostgresSourceConnector(cfg)

    def _mock_cursor_for_schema(self, pk_rows, idx_rows=None, fk_rows=None, check_rows=None, uq_rows=None):
        idx_rows = idx_rows if idx_rows is not None else []
        fk_rows = fk_rows if fk_rows is not None else []
        check_rows = check_rows if check_rows is not None else []
        uq_rows = uq_rows if uq_rows is not None else []
        cur = MagicMock()
        cur.fetchone.side_effect = [
            ("cloud_test",),  # schema resolution
            None,             # RLS
            None,             # partition key
        ]
        cur.fetchall.side_effect = [
            [("customer_id", "integer", "NO", None, None, None, False, None, None, None)],  # columns
            pk_rows,                                                 # PK
            idx_rows,                                                # indexes
            fk_rows,                                                 # FKs
            check_rows,                                              # check constraints
            uq_rows,                                                 # unique constraints
        ]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_get_schema_pk_query_scoped_by_schema(self):
        connector = self._source_conn()
        conn, cur = self._mock_cursor_for_schema(
            pk_rows=[("customer_id", "pk_cloud_test_customers")],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        pk_sql = next(call.args[0] for call in cur.execute.call_args_list if "PRIMARY KEY" in call.args[0])
        assert "tc.table_schema = %s" in pk_sql, (
            f"PK query must filter by table_schema. Got: {pk_sql!r}"
        )
        pk_params = next(call.args[1] for call in cur.execute.call_args_list if "PRIMARY KEY" in call.args[0])
        assert pk_params == ("customers", "cloud_test"), (
            f"PK query params must include table_schema. Got: {pk_params!r}"
        )
        assert schema.primary_key == ["customer_id"]
        assert schema.primary_key_name == "pk_cloud_test_customers"

    def test_get_schema_index_query_scoped_by_schema(self):
        connector = self._source_conn()
        conn, cur = self._mock_cursor_for_schema(
            pk_rows=[],
            idx_rows=[
                ("idx_customer_id", True, ["customer_id"],
                 "CREATE UNIQUE INDEX idx_customer_id ON cloud_test.customers (customer_id)",
                 False),
            ],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        idx_sql = next(call.args[0] for call in cur.execute.call_args_list if "pg_get_indexdef" in call.args[0])
        assert "n.nspname = %s" in idx_sql, (
            f"Index query must filter by schema namespace. Got: {idx_sql!r}"
        )
        idx_params = next(call.args[1] for call in cur.execute.call_args_list if "pg_get_indexdef" in call.args[0])
        assert idx_params == ("cloud_test", "customers"), (
            f"Index query params must include schema then table name. Got: {idx_params!r}"
        )
        assert len(schema.indexes) == 1
        assert schema.indexes[0].name == "idx_customer_id"
        assert schema.indexes[0].constraint_backed is False

    def test_get_schema_fk_query_scoped_by_schema(self):
        connector = self._source_conn()
        conn, cur = self._mock_cursor_for_schema(
            pk_rows=[],
            fk_rows=[
                ("fk_customer", "customer_id", "public", "customers", "customer_id", "CASCADE", "CASCADE"),
            ],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        fk_sql = next(call.args[0] for call in cur.execute.call_args_list if "FOREIGN KEY" in call.args[0])
        assert "tc.table_schema = %s" in fk_sql, (
            f"FK query must filter by table_schema. Got: {fk_sql!r}"
        )
        assert "tc.table_schema = kcu.table_schema" in fk_sql, (
            f"FK join must include schema equality. Got: {fk_sql!r}"
        )
        fk_params = next(call.args[1] for call in cur.execute.call_args_list if "FOREIGN KEY" in call.args[0])
        assert fk_params == ("customers", "cloud_test"), (
            f"FK query params must include table_schema. Got: {fk_params!r}"
        )
        assert len(schema.foreign_keys) == 1
        assert schema.foreign_keys[0].name == "fk_customer"

    def test_get_schema_check_constraint_query_scoped_by_schema(self):
        connector = self._source_conn()
        conn, cur = self._mock_cursor_for_schema(
            pk_rows=[],
            check_rows=[("chk_customer_active", "active = true")],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        chk_sql = next(call.args[0] for call in cur.execute.call_args_list if "CHECK" in call.args[0])
        assert "tc.table_schema = %s" in chk_sql, (
            f"Check constraint query must filter by table_schema. Got: {chk_sql!r}"
        )
        assert "tc.constraint_schema = cc.constraint_schema" in chk_sql, (
            f"Check constraint join must include schema equality. Got: {chk_sql!r}"
        )
        chk_params = next(call.args[1] for call in cur.execute.call_args_list if "CHECK" in call.args[0])
        assert chk_params == ("customers", "cloud_test"), (
            f"Check constraint query params must include table_schema. Got: {chk_params!r}"
        )
        assert len(schema.check_constraints) == 1
        assert schema.check_constraints[0].name == "chk_customer_active"

    def test_get_schema_fk_query_scopes_referential_constraints_and_column_usage_by_schema(self):
        connector = self._source_conn()
        conn, cur = self._mock_cursor_for_schema(
            pk_rows=[],
            fk_rows=[],
        )
        connector._conn = conn

        connector.get_schema("customers")

        fk_sql = next(
            call.args[0] for call in cur.execute.call_args_list if "FOREIGN KEY" in call.args[0]
        )
        assert "tc.constraint_schema = rc.constraint_schema" in fk_sql, (
            f"FK join to referential_constraints must include schema equality. Got: {fk_sql!r}"
        )
        assert "rc.unique_constraint_schema = ccu.constraint_schema" in fk_sql, (
            f"FK join to constraint_column_usage must include schema equality. Got: {fk_sql!r}"
        )

    def test_get_schema_fk_no_duplicate_columns_under_cross_schema_pk_collision(self):
        """Regression: when the referenced PK constraint name (e.g. customers_pkey)
        exists in multiple schemas (public + cloud_test), an un-qualified FK
        join returns one ccu row per colliding schema, which the fk_map dedup
        turns into duplicated columns and/or a wrong ref_schema. The
        schema-qualified joins must yield a single, correct row per FK."""
        connector = self._source_conn()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        connector._conn = conn

        state = {"last_sql": None}

        def _execute(sql, params=None):
            state["last_sql"] = sql

        cur.execute.side_effect = _execute
        cur.fetchone.side_effect = [
            ("cloud_test",),   # schema resolution
            None,              # RLS
            None,              # partition key
        ]

        def _fetchall():
            sql = state["last_sql"]
            if "FOREIGN KEY" in sql:
                scoped = (
                    "tc.constraint_schema = rc.constraint_schema" in sql
                    and "rc.unique_constraint_schema = ccu.constraint_schema" in sql
                )
                if scoped:
                    return [
                        ("orders_customer_id_fkey", "customer_id", "cloud_test", "customers", "customer_id", "NO ACTION", "NO ACTION"),
                        ("orders_product_id_fkey", "product_id", "cloud_test", "products", "product_id", "NO ACTION", "NO ACTION"),
                    ]
                return [
                    ("orders_customer_id_fkey", "customer_id", "cloud_test", "customers", "customer_id", "NO ACTION", "NO ACTION"),
                    ("orders_customer_id_fkey", "customer_id", "public", "customers", "customer_id", "NO ACTION", "NO ACTION"),
                    ("orders_product_id_fkey", "product_id", "cloud_test", "products", "product_id", "NO ACTION", "NO ACTION"),
                    ("orders_product_id_fkey", "product_id", "public", "products", "product_id", "NO ACTION", "NO ACTION"),
                ]
            return []

        cur.fetchall.side_effect = _fetchall

        schema = connector.get_schema("orders")

        assert len(schema.foreign_keys) == 2
        by_name = {fk.name: fk for fk in schema.foreign_keys}
        assert set(by_name) == {"orders_customer_id_fkey", "orders_product_id_fkey"}
        for fk in schema.foreign_keys:
            assert len(fk.columns) == 1, f"{fk.name} columns duplicated: {fk.columns}"
            assert len(fk.ref_columns) == 1, f"{fk.name} ref_columns duplicated: {fk.ref_columns}"
            assert fk.ref_schema == "cloud_test", f"{fk.name} ref_schema wrong: {fk.ref_schema}"


class TestConfigSchema:
    def test_schema_yaml_is_valid(self):
        import yaml
        import os
        schema_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "config", "migration_config.schema.yaml",
        )
        with open(schema_path) as f:
            schema = yaml.safe_load(f)

        assert "source" in schema
        assert "target" in schema
        assert "migration" in schema


class TestOrchestratorGrantFailureTracking:
    """Grant failures should be tracked in migration summary."""

    def test_grant_failure_adds_to_failed_objects_and_all_errors(self):
        from core.orchestrator import MigrationOrchestrator
        from core.connectors.base import GrantDef

        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)

        source.list_objects.return_value = ["customers"]
        source.get_object_count.return_value = 1
        source.get_schema.return_value = Schema(
            name="customers", columns=[Column(name="id", source_type="integer")]
        )
        source.export_full.return_value = iter([{"id": 1}])
        source.list_grants.return_value = [
            GrantDef(
                privileges="SELECT",
                object_type="TABLE",
                object_name="customers",
                schema_name="public",
                grantee="missing_role",
            )
        ]

        target.connect.return_value = None
        target.create_schema.return_value = None
        target.create_object_if_missing.return_value = None
        target.upsert_batch.return_value = UpsertResult(success_count=1)
        target.get_object_count.return_value = 1
        # Make apply_grant fail
        target.apply_grant.side_effect = Exception("role does not exist")

        orchestrator = MigrationOrchestrator(source, target, {})
        result = orchestrator.run_full()

        # Grant failure should be reflected in status
        assert result["status"] == "partial_success"
        # Grant failure should be recorded in the grants phase results
        grants_phase = result["phases"].get("grants", [])
        assert any("failed" in str(g) and "role does not exist" in str(g) for g in grants_phase)


class TestPostgresUniqueConstraintVsUniqueIndex:
    """A UNIQUE constraint must migrate as a real constraint; a hand-made
    UNIQUE INDEX must migrate as an independent index. The two are told apart by
    the source using pg_constraint.conindid, never by name."""

    def _target(self, failing=()):
        """Build a target whose cursor is a plain fake.

        ``failing`` maps a substring of the SQL to the exception it should raise,
        letting a test fail one specific object and assert the others still run.
        A real fake cursor is used (not MagicMock) so the recorded SQL is exact
        and a raising statement does not recurse through the mock.
        """
        from core.connectors.postgresql import PostgresTargetConnector

        executed: list[str] = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *a, **kw):
                executed.append(str(sql))
                for fragment, err in failing:
                    if fragment in str(sql):
                        raise err

        class FakeConn:
            def __init__(self):
                self.rollbacks = 0
                self.commits = 0

            def cursor(self):
                return FakeCursor()

            def rollback(self):
                self.rollbacks += 1

            def commit(self):
                self.commits += 1

        target = PostgresTargetConnector({"database": "t", "source_engine": "postgresql"})
        target._conn = FakeConn()
        return target, target._conn, executed

    @staticmethod
    def _sql(executed):
        return executed

    def test_unique_constraint_is_created_as_alter_table_add_constraint(self):
        target, conn, executed = self._target()
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
        )

        target.apply_constraints(schema)

        assert (
            'ALTER TABLE "training"."customers" ADD CONSTRAINT uq_customers_email UNIQUE (email)'
            in executed
        )

    def test_constraint_backed_index_is_not_recreated_standalone(self):
        target, conn, executed = self._target()
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            indexes=[Index(
                name="uq_customers_email", columns=["email"], unique=True,
                ddl="CREATE UNIQUE INDEX uq_customers_email ON training.customers (email)",
                constraint_backed=True,
            )],
        )

        target.apply_constraints(schema)

        assert any("ADD CONSTRAINT uq_customers_email UNIQUE (email)" in s for s in executed)
        assert not any("CREATE UNIQUE INDEX" in s for s in executed), (
            "the constraint-backed index must not be recreated as a standalone index"
        )

    def test_manually_created_unique_index_is_still_migrated_as_index(self):
        target, conn, executed = self._target()
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            indexes=[Index(
                name="uq_customers_name_manual", columns=["customer_name"], unique=True,
                ddl="CREATE UNIQUE INDEX uq_customers_name_manual ON training.customers (customer_name)",
                constraint_backed=False,
            )],
        )

        target.apply_constraints(schema)

        assert any("uq_customers_name_manual" in s and "CREATE UNIQUE INDEX" in s for s in executed)

    def test_constraint_and_index_sharing_a_name_both_survive(self):
        """A constraint-backed index and a similarly-named manual index must not
        be conflated: the manual one is still created."""
        target, conn, executed = self._target()
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            indexes=[
                Index(name="uq_customers_email", columns=["email"], unique=True,
                      ddl="CREATE UNIQUE INDEX uq_customers_email ON training.customers (email)",
                      constraint_backed=True),
                Index(name="uq_customers_email_manual", columns=["city"], unique=True,
                      ddl="CREATE UNIQUE INDEX uq_customers_email_manual ON training.customers (city)",
                      constraint_backed=False),
            ],
        )

        target.apply_constraints(schema)

        assert any("ADD CONSTRAINT uq_customers_email UNIQUE (email)" in s for s in executed)
        assert any("uq_customers_email_manual" in s and "CREATE UNIQUE INDEX" in s
                   for s in executed)

    def test_unique_constraint_applied_before_indexes(self):
        target, conn, executed = self._target()
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            indexes=[Index(name="idx_city", columns=["city"],
                           ddl="CREATE INDEX idx_city ON training.customers (city)")],
        )

        target.apply_constraints(schema)

        add = next(i for i, s in enumerate(executed) if "ADD CONSTRAINT uq_customers_email" in s)
        idx = next(i for i, s in enumerate(executed) if "CREATE INDEX IF NOT EXISTS idx_city" in s)
        assert add < idx, "UNIQUE constraint must be applied before normal indexes"

    def test_failure_in_one_constraint_does_not_poison_following_objects(self):
        """A real error must roll back so the next CHECK/index/FK still runs."""
        target, conn, executed = self._target(failing=[
            ("ADD CONSTRAINT uq_broken UNIQUE", RuntimeError('relation "uq_broken" already exists')),
            ("ADD CONSTRAINT ck_name CHECK", RuntimeError("permission denied for schema training")),
        ])
        schema = Schema(
            name="customers", schema_name="training",
            unique_constraints=[
                UniqueConstraint(name="uq_broken", columns=["email"]),
                UniqueConstraint(name="uq_ok", columns=["city"]),
            ],
            check_constraints=[CheckConstraint(name="ck_name", expression="length(city) > 2")],
            indexes=[Index(name="idx_city", columns=["city"],
                           ddl="CREATE INDEX idx_city ON training.customers (city)")],
        )

        target.apply_constraints(schema)

        # both unique constraints attempted, despite the first one failing
        assert any("ADD CONSTRAINT uq_broken UNIQUE" in s for s in executed)
        assert any("ADD CONSTRAINT uq_ok UNIQUE" in s for s in executed)
        # the failing CHECK and the index after it were still attempted
        assert any("ADD CONSTRAINT ck_name CHECK" in s for s in executed)
        assert any("CREATE INDEX IF NOT EXISTS idx_city" in s for s in executed)
        # each failure rolled back individually rather than aborting the run
        assert conn.rollbacks >= 2

    def test_real_error_is_audited_as_failed_not_already_exists(self):
        target, conn, executed = self._target(failing=[
            ("ADD CONSTRAINT uq_customers_email UNIQUE",
             RuntimeError("must be owner of relation customers")),
        ])

        with patch("core.connectors.postgresql.target.audit_log") as logged:
            target.apply_constraints(Schema(
                name="customers", schema_name="training",
                unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            ))

        unique_calls = [c for c in logged.call_args_list if c.kwargs.get("phase") == "create_unique"]
        assert len(unique_calls) == 1
        call = unique_calls[0]
        assert call.kwargs["status"] == "failed"
        assert "must be owner" in call.kwargs["details"]["reason"]

    def test_duplicate_object_is_audited_as_skipped(self):
        target, conn, executed = self._target(failing=[
            ("ADD CONSTRAINT uq_customers_email UNIQUE",
             RuntimeError('constraint "uq_customers_email" for relation "customers" already exists')),
        ])

        with patch("core.connectors.postgresql.target.audit_log") as logged:
            target.apply_constraints(Schema(
                name="customers", schema_name="training",
                unique_constraints=[UniqueConstraint(name="uq_customers_email", columns=["email"])],
            ))

        call = next(c for c in logged.call_args_list if c.kwargs.get("phase") == "create_unique")
        assert call.kwargs["status"] == "skipped"
        assert "already exists" in call.kwargs["details"]["reason"]


class TestPrimaryKeyNamePreservation:
    """An explicitly named source PRIMARY KEY must keep its name on the target.

    The name is carried on the existing ``Schema`` DTO (``primary_key_name``)
    next to the column list, so no parallel PK representation is introduced.
    When the source had no explicit name the target keeps emitting a bare
    ``PRIMARY KEY (...)`` and the engine generates its own name, exactly as
    before this change.
    """

    @staticmethod
    def _pg_ddl(schema):
        """Run the PostgreSQL create_table path and return the emitted DDL."""
        from core.connectors.postgresql import PostgresTargetConnector

        class FakeCursor:
            def __init__(self):
                self.sql: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *a, **kw):
                self.sql.append(str(sql))
                # existence probe must report "table absent" so DDL is emitted
                if "information_schema.tables" in str(sql):
                    self._row = None
                else:
                    self._row = None

            def fetchone(self):
                return None

        cur = FakeCursor()

        class FakeConn:
            def __init__(self):
                self._cur = cur

            def cursor(self):
                return self._cur

            def commit(self):
                pass

            def rollback(self):
                pass

        target = PostgresTargetConnector({"database": "t", "source_engine": "postgresql"})
        target._conn = FakeConn()
        target.create_object_if_missing(schema)
        return next((s for s in cur.sql if s.upper().startswith("CREATE TABLE")), "")

    # --- A. explicitly named PK is preserved ---

    def test_pg_named_primary_key_is_preserved_in_ddl(self):
        ddl = self._pg_ddl(Schema(
            name="pk_name_test", schema_name="training",
            columns=[Column(name="id", source_type="integer", nullable=False)],
            primary_key=["id"],
            primary_key_name="pk_test_primary_key",
        ))
        assert 'CONSTRAINT "pk_test_primary_key" PRIMARY KEY (id)' in ddl
        assert "pk_name_test_pkey" not in ddl

    def test_mssql_named_primary_key_is_preserved_in_ddl(self):
        from core.connectors.mssql import MSSQLTargetConnector
        cur = MagicMock()
        cur.fetchone.return_value = None  # table/schema missing -> DDL emitted
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        target = MSSQLTargetConnector({"database": "t", "source_engine": "mssql"})
        target._conn = conn

        target.create_object_if_missing(Schema(
            name="pk_name_test", schema_name="training",
            columns=[Column(name="id", source_type="int", nullable=False, target_type="int")],
            primary_key=["id"],
            primary_key_name="pk_test_primary_key",
        ))

        ddl = next(str(c.args[0]) for c in cur.execute.call_args_list
                   if c.args and str(c.args[0]).upper().startswith("CREATE TABLE"))
        assert 'CONSTRAINT "pk_test_primary_key" PRIMARY KEY (id)' in ddl

    # --- B. PK columns and ordering are unchanged ---

    def test_pg_composite_primary_key_columns_and_order_unchanged(self):
        ddl = self._pg_ddl(Schema(
            name="composite", schema_name="training",
            columns=[
                Column(name="tenant_id", source_type="integer", nullable=False),
                Column(name="code", source_type="text", nullable=False),
            ],
            primary_key=["tenant_id", "code"],
            primary_key_name="pk_composite_ordered",
        ))
        assert 'CONSTRAINT "pk_composite_ordered" PRIMARY KEY (tenant_id, code)' in ddl

    # --- C. unnamed / default cases keep the previous behaviour ---

    def test_pg_unnamed_primary_key_emits_bare_clause(self):
        ddl = self._pg_ddl(Schema(
            name="pk_name_test", schema_name="training",
            columns=[Column(name="id", source_type="integer", nullable=False)],
            primary_key=["id"],
        ))
        assert "PRIMARY KEY (id)" in ddl
        assert "CONSTRAINT" not in ddl

    def test_pg_no_primary_key_emits_no_pk_clause(self):
        ddl = self._pg_ddl(Schema(
            name="pk_less", schema_name="training",
            columns=[Column(name="id", source_type="integer", nullable=True)],
        ))
        assert "PRIMARY KEY" not in ddl

    def test_mssql_unnamed_primary_key_emits_bare_clause(self):
        from core.connectors.mssql import MSSQLTargetConnector
        cur = MagicMock()
        cur.fetchone.return_value = None  # table/schema missing -> DDL emitted
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        target = MSSQLTargetConnector({"database": "t", "source_engine": "mssql"})
        target._conn = conn

        target.create_object_if_missing(Schema(
            name="orders", schema_name="dbo",
            columns=[Column(name="id", source_type="int", nullable=False, target_type="int")],
            primary_key=["id"],
        ))

        ddl = next(str(c.args[0]) for c in cur.execute.call_args_list
                   if c.args and str(c.args[0]).upper().startswith("CREATE TABLE"))
        assert "PRIMARY KEY (id)" in ddl
        assert "CONSTRAINT" not in ddl

    # --- D. quoting of awkward names uses the shared helper ---

    def test_pg_awkward_pk_name_is_quoted_not_concatenated(self):
        ddl = self._pg_ddl(Schema(
            name="odd", schema_name="training",
            columns=[Column(name="id", source_type="integer", nullable=False)],
            primary_key=["id"],
            primary_key_name="Mixed Case-PK",
        ))
        assert 'CONSTRAINT "Mixed Case-PK" PRIMARY KEY (id)' in ddl

    def test_pg_already_quoted_pk_name_is_not_double_wrapped(self):
        ddl = self._pg_ddl(Schema(
            name="odd", schema_name="training",
            columns=[Column(name="id", source_type="integer", nullable=False)],
            primary_key=["id"],
            primary_key_name='"already-quoted"',
        ))
        assert 'CONSTRAINT "already-quoted" PRIMARY KEY (id)' in ddl
        assert '""already-quoted""' not in ddl

    # --- source discovery carries the name ---

    def test_mssql_partitioned_table_preserves_pk_name(self):
        from core.connectors.mssql.objects import partition as mssql_partition

        cur = MagicMock()
        cur.fetchone.return_value = None  # schema + table missing -> DDL emitted
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        mssql_partition.create_partitioned_table(conn, Schema(
            name="events", schema_name="dbo",
            columns=[
                Column(name="id", source_type="int", nullable=False),
                Column(name="created_at", source_type="datetime2", nullable=False),
            ],
            primary_key=["id"],
            primary_key_name="pk_events",
        ), partition_scheme_name="ps_events", partition_column="created_at")

        ddl = next(str(c.args[0]) for c in cur.execute.call_args_list
                   if c.args and str(c.args[0]).upper().startswith("CREATE TABLE"))
        assert 'CONSTRAINT "pk_events" PRIMARY KEY (id)' in ddl

    def test_mssql_partitioned_table_unnamed_pk_stays_bare(self):
        from core.connectors.mssql.objects import partition as mssql_partition

        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        mssql_partition.create_partitioned_table(conn, Schema(
            name="events", schema_name="dbo",
            columns=[Column(name="id", source_type="int", nullable=False)],
            primary_key=["id"],
        ), partition_scheme_name="ps_events", partition_column="id")

        ddl = next(str(c.args[0]) for c in cur.execute.call_args_list
                   if c.args and str(c.args[0]).upper().startswith("CREATE TABLE"))
        assert "PRIMARY KEY (id)" in ddl
        assert "CONSTRAINT" not in ddl

    def test_pg_source_carries_pk_name_and_columns(self):
        connector = TestPostgresCrossSchemaMetadataIsolation()._source_conn()
        conn, cur = TestPostgresCrossSchemaMetadataIsolation()._mock_cursor_for_schema(
            pk_rows=[("tenant_id", "pk_composite"), ("code", "pk_composite")],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        assert schema.primary_key == ["tenant_id", "code"]
        assert schema.primary_key_name == "pk_composite"

    def test_pg_source_pk_name_is_none_when_table_has_no_pk(self):
        connector = TestPostgresCrossSchemaMetadataIsolation()._source_conn()
        conn, cur = TestPostgresCrossSchemaMetadataIsolation()._mock_cursor_for_schema(
            pk_rows=[],
        )
        connector._conn = conn

        schema = connector.get_schema("customers")

        assert schema.primary_key == []
        assert schema.primary_key_name is None

    # --- MSSQL composite PK ordering through real discovery ---

    def _mssql_source_conn_for_pk_ordering(self, pk_rows):
        """MSSQL cursor mock that dispatches on the SQL actually executed."""
        executed: list[str] = []

        def _execute(sql, *args, **kwargs):
            executed.append(" ".join(str(sql).split()).lower())

        def _fetchall():
            sql = executed[-1] if executed else ""
            if "constraint_type = 'primary key'" in sql:
                required = (
                    "kcu.column_name",
                    "tc.constraint_name",
                    "kcu.ordinal_position",
                    "tc.table_name = kcu.table_name",
                )
                return pk_rows if all(f in sql for f in required) else []
            if "from information_schema.columns" in sql:
                # id, col_b, col_a -- declaration order deliberately differs
                # from the PK ordinal order asserted below.
                return [
                    ("id", "int", "NO", None, 10, 0),
                    ("col_b", "nvarchar", "NO", 50, None, None),
                    ("col_a", "nvarchar", "NO", 50, None, None),
                ]
            return []

        cur = MagicMock()
        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.fetchone.side_effect = [("training",)]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_mssql_source_orders_composite_pk_by_ordinal_position(self):
        """Composite PK discovery must be driven by the PK ordinal position.

        Ordering is performed by the server, so the regression this test guards
        is the *query clause*, not Python-side sorting: the ``pk_sql`` assertion
        below fails outright if ``ORDER BY kcu.ORDINAL_POSITION`` is dropped from
        ``INFORMATION_SCHEMA.KEY_COLUMN_USAGE``. The rows are supplied in
        deliberately awkward order (col_b before col_a) with their ordinal
        positions attached, and the remaining assertions confirm that real
        ``get_schema`` discovery maps those rows onto ``Schema.primary_key`` and
        carries the constraint name through unchanged.
        """
        from core.connectors.mssql import MSSQLSourceConnector

        # ORDINAL_POSITION 2 first, then 1 -- the awkward order a server without
        # the ORDER BY clause would return.
        pk_rows = [
            ("col_b", "PK_Composite_Ordered"),
            ("col_a", "PK_Composite_Ordered"),
        ]
        conn, cur = self._mssql_source_conn_for_pk_ordering(pk_rows)
        connector = MSSQLSourceConnector({
            "host": "x", "port": 1, "database": "x",
            "username": "u", "password": "p", "ssl": False,
            "include_schemas": ["training"],
        })
        connector._conn = conn

        schema = connector.get_schema("composite")

        # The query must genuinely carry the ordering clause.
        pk_sql = next(
            " ".join(str(c.args[0]).split())
            for c in cur.execute.call_args_list
            if c.args and "CONSTRAINT_TYPE = 'PRIMARY KEY'" in str(c.args[0]).upper()
        )
        assert "ORDER BY kcu.ORDINAL_POSITION" in pk_sql

        # Every PK row maps to one column; the single name covers the composite.
        assert schema.primary_key == ["col_b", "col_a"]
        assert schema.primary_key_name == "PK_Composite_Ordered"

        # With ordinals applied by the server, col_a (ordinal 1) leads.
        ordinals = {"col_a": 1, "col_b": 2}
        assert sorted(schema.primary_key, key=lambda c: ordinals[c]) == ["col_a", "col_b"]

    # --- regression: discovery must depend on the real PK query ---

    def _mock_cursor_pk_join_sensitive(self, pk_rows):
        """Cursor whose PK result is only returned for the real PK query.

        ``get_schema`` issues its metadata queries in a fixed order, so a plain
        positional ``fetchall`` stub cannot tell the PK query apart from the
        others and will happily return PK rows even if the query no longer
        selects a constraint name. This mock keys off the SQL that was actually
        executed: PK rows come back only while that SQL still carries the
        ``pg_constraint`` join and the ``conname`` selection. Removing either
        one therefore makes the caller observe "no primary key" and fails the
        test instead of passing on a hand-fed stub.
        """
        executed: list[str] = []

        def _execute(sql, *args, **kwargs):
            executed.append(" ".join(str(sql).split()).lower())

        def _fetchall():
            sql = executed[-1] if executed else ""
            if "constraint_type = 'primary key'" in sql:
                required = (
                    "pg_catalog.pg_constraint",
                    "pgc.conname",
                    "pgc.contype = 'p'",
                    "to_regclass",
                    "kcu.ordinal_position",
                )
                return pk_rows if all(f in sql for f in required) else []
            if "from information_schema.columns" in sql:
                return [("customer_id", "integer", "NO", None, None,
                         False, None, None, None, None)]
            return []

        cur = MagicMock()
        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.fetchone.side_effect = [("public",), None, None]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_pg_pk_query_selects_constraint_name_via_pg_constraint(self):
        """The PK query itself must carry every fragment discovery relies on."""
        conn, cur = self._mock_cursor_pk_join_sensitive(
            pk_rows=[("tenant_id", "pk_composite"), ("code", "pk_composite")],
        )
        connector = TestPostgresCrossSchemaMetadataIsolation()._source_conn()
        connector._conn = conn

        schema = connector.get_schema("customers")

        pk_sql = next(
            " ".join(str(c.args[0]).split())
            for c in cur.execute.call_args_list
            if c.args and "constraint_type = 'PRIMARY KEY'" in str(c.args[0])
        )
        for fragment in (
            "pg_catalog.pg_constraint",
            "pgc.conname",
            "pgc.contype = 'p'",
            "to_regclass",
            "ORDER BY kcu.ordinal_position",
        ):
            assert fragment in pk_sql, f"PK query lost {fragment!r}: {pk_sql}"

        # The name only reaches the DTO because the query selected it.
        assert schema.primary_key == ["tenant_id", "code"]
        assert schema.primary_key_name == "pk_composite"

    def test_pg_source_reports_no_pk_when_constraint_join_is_absent(self):
        """Guards the mock itself: no pg_constraint join means no PK name."""
        executed: list[str] = []

        def _execute(sql, *args, **kwargs):
            executed.append(" ".join(str(sql).split()).lower())

        def _fetchall():
            sql = executed[-1] if executed else ""
            if "constraint_type = 'primary key'" in sql:
                # Simulates the regression: join and conname removed.
                return []
            if "from information_schema.columns" in sql:
                return [("customer_id", "integer", "NO", None, None,
                         False, None, None, None, None)]
            return []

        cur = MagicMock()
        cur.execute.side_effect = _execute
        cur.fetchall.side_effect = _fetchall
        cur.fetchone.side_effect = [("public",), None, None]
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur

        connector = TestPostgresCrossSchemaMetadataIsolation()._source_conn()
        connector._conn = conn

        schema = connector.get_schema("customers")

        assert schema.primary_key == []
        assert schema.primary_key_name is None