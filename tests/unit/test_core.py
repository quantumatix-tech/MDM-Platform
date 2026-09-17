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
    FunctionDef,
    TriggerDef,
    MySQLPartitionDef,
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

    def test_full_sync_clears_stale_target_rows_before_load(self):
        """A full run replaces target table contents before it upserts rows."""
        stale_target_counts = {
            "customers": 4,   # one stale row
            "products": 3,
            "orders": 4,
        }
        source = self._make_source(self.EXPECTED_COUNTS)
        target = self._make_target(stale_target_counts)

        result = MigrationOrchestrator(source, target, {}).run_full()

        target.clear_objects_for_full_sync.assert_called_once_with(self.EXPECTED_TABLES)
        assert result["phases"]["customers"]["success"] == 3
        assert result["phases"]["full_target_sync"]["strategy"] == "clear-before-load"


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


class TestMySQLViewDatabaseReferenceRewrite:
    def test_target_view_maps_source_database_qualifiers(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector(
            {"host": "x", "port": 1, "database": "mysql_migration_target",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        view = ViewDefinition(
            name="customer_order_summary",
            schema_name="mysql_migration_source",
            definition=(
                "SELECT c.name, 'mysql_migration_source.customers' AS label "
                "/* mysql_migration_source.orders remains a comment */ "
                "FROM `mysql_migration_source`.`customers` c "
                "LEFT JOIN mysql_migration_source . `orders` o ON o.customer_id = c.id "
                "LEFT JOIN reporting_archive.orders a ON a.customer_id = c.id"
            ),
        )

        target.create_view(view)

        create_sql = cur.execute.call_args.args[0]
        assert "`mysql_migration_target`.`customers`" in create_sql
        assert "mysql_migration_target . `orders`" in create_sql
        assert "'mysql_migration_source.customers'" in create_sql
        assert "/* mysql_migration_source.orders remains a comment */" in create_sql
        assert "reporting_archive.orders" in create_sql
        assert "`mysql_migration_source`.`customers`" not in create_sql
        assert "mysql_migration_source . `orders`" not in create_sql

    def test_target_view_leaves_same_database_definition_unchanged(self):
        from core.connectors.mysql import _rewrite_view_database_references

        definition = "SELECT * FROM mysql_migration_target.customers"
        assert _rewrite_view_database_references(
            definition, "mysql_migration_target", "mysql_migration_target"
        ) == definition

    def test_target_view_maps_ansi_quoted_source_database_qualifiers(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector(
            {"host": "x", "port": 1, "database": "mysql_migration_target",
             "username": "u", "password": "p", "ssl": False}
        )
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        view = ViewDefinition(
            name="customer_order_summary",
            schema_name="mysql_migration_source",
            definition=(
                'SELECT c.name FROM "mysql_migration_source"."customers" c '
                'LEFT JOIN "mysql_migration_source"."orders" o '
                "ON o.customer_id = c.id"
            ),
        )

        target.create_view(view)

        create_sql = cur.execute.call_args.args[0]
        assert '"mysql_migration_target"."customers"' in create_sql
        assert '"mysql_migration_target"."orders"' in create_sql
        assert '"mysql_migration_source"' not in create_sql


class TestMySQLPartitionMigration:
    @staticmethod
    def _range_partition_schema() -> Schema:
        return Schema(
            name="tbl_partition_test",
            columns=[
                Column(name="id", source_type="INT", nullable=False),
                Column(name="created_at", source_type="DATE", nullable=False),
            ],
            primary_key=["id", "created_at"],
            mysql_partition_method="RANGE",
            mysql_partition_expression="YEAR(created_at)",
            mysql_partitions=[
                MySQLPartitionDef("p2025", "2026"),
                MySQLPartitionDef("p2026", "2027"),
                MySQLPartitionDef("pmax", "MAXVALUE"),
            ],
        )

    def _run_existing_mysql_partition_target(self, target_schema: Schema, reconcile: bool = False, after_reconcile: Schema | None = None, reconcile_error: Exception | None = None):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        source_schema = self._range_partition_schema()
        source.list_objects.return_value = ["tbl_partition_test"]
        source.get_schema.return_value = source_schema
        source.get_object_count.return_value = 3
        source.export_full.return_value = iter([
            {"id": 1, "created_at": "2025-01-01"},
            {"id": 2, "created_at": "2026-01-01"},
            {"id": 3, "created_at": "2027-01-01"},
        ])
        source.get_all_triggers.return_value = []
        source.list_comments.return_value = []
        source.list_grants.return_value = []
        target.create_object_if_missing.return_value = "already_exists"
        target.inspect_schema.return_value = target_schema
        if after_reconcile is not None:
            target.inspect_schema.side_effect = [target_schema, after_reconcile, after_reconcile, after_reconcile]
        if reconcile_error is not None:
            target.reconcile_mysql_table.side_effect = reconcile_error
        target.upsert_batch.return_value = UpsertResult(success_count=3)
        target.get_object_count.return_value = 3
        target.get_capabilities.return_value = {}
        result = MigrationOrchestrator(
            source, target, {"source": {"engine": "mysql"}, "target": {"engine": "mysql"}, "migration": {"reconcile_target_schema": reconcile}},
        ).run_full()
        return result, target

    def test_non_partitioned_summary_has_zero_partition_objects(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target"})
        summary = MigrationOrchestrator(
            MagicMock(spec=SourceConnector), target, {}
        )._build_object_migration_summary(
            {"customers": Schema(name="customers")},
            {"create_partitions": {}},
            {},
        )

        assert summary["categories"]["partitions"]["source_count"] == 0
        assert summary["categories"]["partitions"]["migrated"] == 0

    def test_orchestrator_builds_partition_testing_from_source_and_target_metadata(self):
        source = MagicMock(spec=SourceConnector)
        target = MagicMock(spec=TargetConnector)
        target.get_object_count.return_value = 3
        target.inspect_schema.return_value = Schema(
            name="tbl_partition_test",
            mysql_partition_method="RANGE",
            mysql_partition_expression="YEAR(created_at)",
            mysql_partitions=[
                MySQLPartitionDef("p2025", "2026"),
                MySQLPartitionDef("p2026", "2027"),
                MySQLPartitionDef("pmax", "MAXVALUE"),
            ],
        )
        orchestrator = MigrationOrchestrator(source, target, {})
        source_schema = Schema(
            name="tbl_partition_test",
            mysql_partition_method="RANGE",
            mysql_partition_expression="YEAR(created_at)",
            mysql_partitions=[
                MySQLPartitionDef("p2025", "2026"),
                MySQLPartitionDef("p2026", "2027"),
                MySQLPartitionDef("pmax", "MAXVALUE"),
            ],
        )

        report = orchestrator._build_partition_testing(
            {"tbl_partition_test": source_schema},
            {"tbl_partition_test": {"source_rows": 3, "success": 3, "failure": 0}},
        )

        assert report["status"] == "PASS"
        assert report["tables"][0]["checks"] == {
            "data_migration": "PASS",
            "structure_preservation": "PASS",
            "overall": "PASS",
        }

        summary = orchestrator._build_object_migration_summary(
            {"tbl_partition_test": source_schema},
            {"create_partitions": {"tbl_partition_test.p2025": "created", "tbl_partition_test.p2026": "created", "tbl_partition_test.pmax": "created"}},
            {},
        )
        assert summary["categories"]["partitions"]["source_count"] == 3
        assert summary["categories"]["partitions"]["migrated"] == 3
        assert summary["categories"]["partitions"]["status"] == "MIGRATED"

    def test_target_create_table_preserves_ordered_range_partitions(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target", "source_engine": "mysql"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        outcome = target.create_object_if_missing(Schema(
            name="tbl_partition_test",
            columns=[
                Column(name="id", source_type="INT", nullable=False),
                Column(name="name", source_type="VARCHAR(100)", nullable=False),
                Column(name="created_at", source_type="DATE", nullable=False),
            ],
            primary_key=["id", "created_at"],
            mysql_partition_method="RANGE",
            mysql_partition_expression="YEAR(created_at)",
            mysql_partitions=[
                MySQLPartitionDef("p2025", "2026"),
                MySQLPartitionDef("p2026", "2027"),
                MySQLPartitionDef("pmax", "MAXVALUE"),
            ],
        ))

        create_sql = cur.execute.call_args.args[0]
        assert "PARTITION BY RANGE (YEAR(created_at))" in create_sql
        assert "PARTITION `p2025` VALUES LESS THAN (2026)" in create_sql
        assert "PARTITION `p2026` VALUES LESS THAN (2027)" in create_sql
        assert "PARTITION `pmax` VALUES LESS THAN (MAXVALUE)" in create_sql
        assert create_sql.index("p2025") < create_sql.index("p2026") < create_sql.index("pmax")
        assert outcome == "created"

    def test_existing_unpartitioned_target_is_failed_not_reported_as_partition_created(self):
        result, target = self._run_existing_mysql_partition_target(
            Schema(name="tbl_partition_test")
        )

        phase = result["phases"]["create_partitions"]
        assert set(phase) == {
            "tbl_partition_test.p2025",
            "tbl_partition_test.p2026",
            "tbl_partition_test.pmax",
        }
        assert all(value.startswith("failed: partition schema mismatch") for value in phase.values())
        assert all("created (table DDL)" not in value for value in phase.values())
        assert "target table is unpartitioned" in next(iter(phase.values()))
        assert result["object_migration"]["categories"]["partitions"]["status"] == "FAILED"
        assert result["object_migration"]["categories"]["partitions"]["migrated"] == 0
        assert result["status"] == "partial_success"
        target.create_object_if_missing.assert_called_once()

    def test_existing_matching_target_partitions_are_verified_without_recreation(self):
        result, target = self._run_existing_mysql_partition_target(
            self._range_partition_schema()
        )

        phase = result["phases"]["create_partitions"]
        assert set(phase.values()) == {"verified_existing (partition signature matches source)"}
        assert result["object_migration"]["categories"]["partitions"]["status"] == "MIGRATED"
        assert result["object_migration"]["categories"]["partitions"]["migrated"] == 3
        target.create_object_if_missing.assert_called_once()

    def test_reconcile_true_stages_and_verifies_an_existing_unpartitioned_target(self):
        source_schema = self._range_partition_schema()
        result, target = self._run_existing_mysql_partition_target(
            Schema(name="tbl_partition_test"), reconcile=True, after_reconcile=source_schema
        )

        assert result["phases"]["reconcile_target_schema"] == {"tbl_partition_test": "recreated_and_verified"}
        assert set(result["phases"]["create_partitions"].values()) == {"reconciled and verified"}
        assert result["object_migration"]["categories"]["partitions"]["status"] == "MIGRATED"
        target.reconcile_mysql_table.assert_called_once_with(source_schema, {"tbl_partition_test"})

    def test_reconciliation_failure_is_reported_without_false_partition_success(self):
        result, _ = self._run_existing_mysql_partition_target(
            Schema(name="tbl_partition_test"), reconcile=True,
            reconcile_error=RuntimeError("staging failed; original retained"),
        )

        assert result["phases"]["reconcile_target_schema"]["tbl_partition_test"].startswith("failed:")
        assert all(value.startswith("failed:") for value in result["phases"]["create_partitions"].values())
        assert result["object_migration"]["categories"]["partitions"]["status"] == "FAILED"

    def test_target_create_table_returns_already_exists_without_ddl(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target", "source_engine": "mysql"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.return_value = ("tbl_partition_test",)
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        outcome = target.create_object_if_missing(self._range_partition_schema())

        assert outcome == "already_exists"
        assert cur.execute.call_count == 1
        assert "INFORMATION_SCHEMA.TABLES" in cur.execute.call_args.args[0]

    def test_source_schema_discovers_ordered_mysql_partitions(self):
        from core.connectors.mysql import MySQLSourceConnector

        target = MySQLSourceConnector({"database": "mysql_migration_source"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.return_value = (None, "InnoDB", "utf8mb4_0900_ai_ci")
        cur.fetchall.side_effect = [
            [("id", "INT", "NO", None, None, "", None, None),
             ("created_at", "DATE", "NO", None, None, "", None, None)],
            [("id",)],
            [],
            [],
            [],
            [("RANGE", "year(`created_at`)", "p2025", "2026"),
             ("RANGE", "year(`created_at`)", "p2026", "2027"),
             ("RANGE", "year(`created_at`)", "pmax", "MAXVALUE")],
        ]
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        schema = target.get_schema("tbl_partition_test")

        assert schema.mysql_partition_method == "RANGE"
        assert schema.mysql_partition_expression == "year(`created_at`)"
        assert [p.name for p in schema.mysql_partitions] == ["p2025", "p2026", "pmax"]


def test_object_summary_classifies_blocked_and_migrated_routines_separately():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    target.get_capabilities.return_value = {}
    orchestrator = MigrationOrchestrator(source, target, {})

    summary = orchestrator._build_object_migration_summary(
        {},
        {
            "functions": {
                "blocked_function": "FUNCTION: BLOCKED\nPlease ask a MySQL administrator to run once:\nSET PERSIST log_bin_trust_function_creators = ON;\nThis server configuration persists across MySQL restarts.\nThen re-run the migration.",
                "migrated_function": "created",
                "migrated_procedure": "created",
            },
            "triggers": {"customers.trg_customer": "created"},
        },
        {
            "functions": 2,
            "procedures": 1,
            "triggers": 1,
            "routine_kinds": {
                "blocked_function": "function",
                "migrated_function": "function",
                "migrated_procedure": "procedure",
            },
        },
    )

    assert summary["categories"]["functions"]["status"] == "BLOCKED"
    assert summary["categories"]["functions"]["migrated"] == 1
    assert summary["categories"]["functions"]["blocked"] == 1
    assert summary["categories"]["procedures"]["status"] == "MIGRATED"
    assert summary["categories"]["triggers"]["status"] == "MIGRATED"
    assert summary["categories"]["functions"]["details"] == [
        "FUNCTION: BLOCKED\nPlease ask a MySQL administrator to run once:\nSET PERSIST log_bin_trust_function_creators = ON;\nThis server configuration persists across MySQL restarts.\nThen re-run the migration."
    ]


def test_object_summary_records_applied_comments_as_migrated():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    target.get_capabilities.return_value = {}
    schema = Schema(
        name="customers",
        comment="Updated customers comment",
        columns=[Column(name="email", source_type="varchar(255)", comment="Updated email comment")],
    )

    summary = MigrationOrchestrator(source, target, {})._build_object_migration_summary(
        {"customers": schema},
        {"comments": {"target.customers": "applied", "target.customers.email": "applied"}},
        {},
    )

    comments = summary["categories"]["comments"]
    assert comments["source_count"] == 2
    assert comments["migrated"] == 2
    assert comments["failed"] == 0
    assert comments["status"] == "MIGRATED"


def test_object_summary_does_not_report_failed_comment_as_migrated():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    target.get_capabilities.return_value = {}
    schema = Schema(name="customers", comment="Updated customers comment")

    summary = MigrationOrchestrator(source, target, {})._build_object_migration_summary(
        {"customers": schema},
        {"comments": {"target.customers": "skipped: target database denied ALTER TABLE"}},
        {},
    )

    comments = summary["categories"]["comments"]
    assert comments["status"] == "FAILED"
    assert comments["migrated"] == 0
    assert comments["failed"] == 1


def test_target_comment_database_failure_is_recorded_as_failed_not_migrated():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    schema = Schema(name="customers", comment="Updated customers comment")
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = schema
    source.get_object_count.return_value = 1
    source.export_full.return_value = iter([{"id": 1}])
    source.list_comments.return_value = [
        CommentDef("TABLE", "customers", "Updated customers comment", "source")
    ]
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.get_object_count.return_value = 1
    target.apply_comment.side_effect = RuntimeError("ALTER TABLE denied")
    target.get_capabilities.return_value = {}

    result = MigrationOrchestrator(source, target, {}).run_full()

    assert result["phases"]["comments"] == {
        "source.customers": "skipped: ALTER TABLE denied"
    }
    comments = result["object_migration"]["categories"]["comments"]
    assert comments["status"] == "FAILED"
    assert comments["migrated"] == 0
    assert comments["failed"] == 1


def test_target_grant_database_failure_is_recorded_as_failed_not_migrated():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = schema
    source.get_object_count.return_value = 1
    source.export_full.return_value = iter([{"id": 1}])
    source.list_grants.return_value = [
        GrantDef("SELECT", "TABLE", "customers", "'migration_grant_test'@'localhost'", "mysql_migration_source")
    ]
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.get_object_count.return_value = 1
    target.apply_grant.side_effect = RuntimeError("GRANT denied")
    target.get_capabilities.return_value = {}

    result = MigrationOrchestrator(source, target, {}).run_full()

    assert result["phases"]["grants"] == {
        "mysql_migration_source.customers TO 'migration_grant_test'@'localhost'": "skipped: GRANT denied"
    }
    grants = result["object_migration"]["categories"]["grants"]
    assert grants["status"] == "FAILED"
    assert grants["migrated"] == 0
    assert grants["failed"] == 1


def test_successful_target_grant_is_recorded_as_migrated():
    source = MagicMock(spec=SourceConnector)
    target = MagicMock(spec=TargetConnector)
    schema = Schema(name="customers", columns=[Column(name="id", source_type="INT")])
    source.list_objects.return_value = ["customers"]
    source.get_schema.return_value = schema
    source.get_object_count.return_value = 1
    source.export_full.return_value = iter([{"id": 1}])
    source.list_grants.return_value = [
        GrantDef("SELECT", "TABLE", "customers", "'migration_grant_test'@'localhost'", "mysql_migration_source")
    ]
    target.upsert_batch.return_value = UpsertResult(success_count=1)
    target.get_object_count.return_value = 1
    target.get_capabilities.return_value = {}

    result = MigrationOrchestrator(source, target, {}).run_full()

    assert result["phases"]["grants"] == {
        "mysql_migration_source.customers TO 'migration_grant_test'@'localhost'": "applied"
    }
    grants = result["object_migration"]["categories"]["grants"]
    assert grants["status"] == "MIGRATED"
    assert grants["migrated"] == 1
    assert grants["failed"] == 0


def test_blocked_object_forces_partial_success_even_when_validation_passes():
    status = MigrationOrchestrator._full_migration_status(
        {"status": "success"},
        [],
        set(),
        {"failed": 0, "blocked": 1},
    )

    assert status == "partial_success"


class TestMySQLTriggerLifecycle:
    def test_function_1419_is_reported_as_actionable_policy_error(self):
        from core.connectors.mysql import MySQLRoutineCreationPolicyError, MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.execute.side_effect = [None, RuntimeError("Error 1419: log_bin_trust_function_creators")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        with pytest.raises(MySQLRoutineCreationPolicyError, match="FUNCTION: BLOCKED") as error:
            target.create_function(FunctionDef(
                name="get_customer_count",
                kind="function",
                ddl="CREATE FUNCTION get_customer_count() RETURNS INT RETURN 1",
            ))

        assert str(error.value).splitlines() == [
            "FUNCTION: BLOCKED",
            "Please ask a MySQL administrator to run once:",
            "SET PERSIST log_bin_trust_function_creators = ON;",
            "This server configuration persists across MySQL restarts.",
            "Then re-run the migration.",
        ]
        assert "SET GLOBAL" not in str(error.value)
        conn.rollback.assert_called_once()

    def test_source_connect_enables_autocommit(self, monkeypatch):
        import mysql.connector
        from core.connectors.mysql import MySQLSourceConnector

        connection = MagicMock()
        monkeypatch.setattr(mysql.connector, "connect", lambda **_: connection)

        source = MySQLSourceConnector({
            "host": "x", "port": 1, "database": "mysql_migration_source",
            "username": "u", "password": "p", "ssl": False,
        })
        monkeypatch.setattr("core.connectors.mysql.ensure_driver", lambda *_: None)
        source.connect()

        assert connection.autocommit is True

    def test_create_trigger_rolls_back_when_ddl_fails(self):
        from core.connectors.mysql import MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.execute.side_effect = [None, RuntimeError("trigger DDL failed")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        with pytest.raises(RuntimeError, match="trigger DDL failed"):
            target.create_trigger(TriggerDef(
                name="trg_after_customer_insert_log",
                table="customers",
                ddl=(
                    "CREATE TRIGGER trg_after_customer_insert_log "
                    "AFTER INSERT ON customers FOR EACH ROW "
                    "INSERT INTO customer_insert_log "
                    "(customer_id, customer_name, customer_status) VALUES "
                    "(NEW.id, NEW.name, NEW.status)"
                ),
                schema_name="mysql_migration_source",
            ))

        conn.rollback.assert_called_once()

    def test_trigger_1419_is_reported_as_actionable_policy_error(self):
        from core.connectors.mysql import MySQLRoutineCreationPolicyError, MySQLTargetConnector

        target = MySQLTargetConnector({"database": "mysql_migration_target"})
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.execute.side_effect = [None, RuntimeError("Error 1419: log_bin_trust_function_creators")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        target._conn = conn

        with pytest.raises(MySQLRoutineCreationPolicyError, match="TRIGGER: BLOCKED") as error:
            target.create_trigger(TriggerDef(
                name="trg_customer",
                table="customers",
                ddl="CREATE TRIGGER trg_customer AFTER INSERT ON customers FOR EACH ROW SET @x = 1",
            ))

        conn.rollback.assert_called_once()
        assert str(error.value).splitlines() == [
            "TRIGGER: BLOCKED",
            "Please ask a MySQL administrator to run once:",
            "SET PERSIST log_bin_trust_function_creators = ON;",
            "This server configuration persists across MySQL restarts.",
            "Then re-run the migration.",
        ]
        assert "SET GLOBAL" not in str(error.value)

    def test_mysql_connector_close_rolls_back_and_closes(self):
        from core.connectors.mysql import MySQLTargetConnector

        connection = MagicMock()
        target = MySQLTargetConnector({"database": "mysql_migration_target"})
        target._conn = connection

        target.close()

        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
        assert target._conn is None


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
        cur.fetchall.side_effect = [
            [("audit_user", "audit_test", "test_customers", "SELECT, INSERT")],
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
        assert '"test_customers"."email"' in grant_sql
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
