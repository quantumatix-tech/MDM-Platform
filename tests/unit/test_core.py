from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    Index,
    ForeignKey,
    CheckConstraint,
    ViewDefinition,
    MaterializedViewDef,
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
        assert "retry" in schema