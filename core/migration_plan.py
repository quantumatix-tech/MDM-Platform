from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from core.connectors.base import Schema


CREATE_AND_MIGRATE = "CREATE_AND_MIGRATE"
MIGRATE = "MIGRATE"
WARNING = "WARNING"
BLOCK = "BLOCK"


@dataclass
class PreflightIssue:
    code: str
    message: str
    severity: str = "blocker"


@dataclass
class SchemaComparison:
    status: str
    differences: list[str] = field(default_factory=list)


@dataclass
class ObjectMigrationPlan:
    object_name: str
    schema_name: str
    source_exists: bool = True
    target_exists: bool = False
    source_schema: dict[str, Any] | None = None
    target_schema: dict[str, Any] | None = None
    schema_comparison: SchemaComparison | None = None
    source_row_count: int | None = None
    target_row_count: int | None = None
    action: str = BLOCK
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    readiness: str = "BLOCKED"


@dataclass
class MigrationPlan:
    source_engine: str = "postgresql"
    target_engine: str = "postgresql"
    objects: list[ObjectMigrationPlan] = field(default_factory=list)
    blockers: list[PreflightIssue] = field(default_factory=list)
    warnings: list[PreflightIssue] = field(default_factory=list)
    source_connected: bool = False
    target_connected: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers and all(not obj.blockers for obj in self.objects)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["ready"] = self.ready
        return result


def validate_postgresql_full_config(config: dict[str, Any]) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for role in ("source", "target"):
        role_config = config.get(role)
        if not isinstance(role_config, dict):
            issues.append(PreflightIssue("CONFIG_MISSING", f"Missing {role} configuration."))
            continue
        if role_config.get("engine") != "postgresql":
            issues.append(
                PreflightIssue(
                    "ENGINE_UNSUPPORTED",
                    f"Phase 1 preflight supports PostgreSQL {role}; configure {role}.engine as 'postgresql'.",
                )
            )
        connection = role_config.get("connection")
        if not isinstance(connection, dict):
            issues.append(PreflightIssue("CONNECTION_MISSING", f"Missing {role}.connection configuration."))
            continue
        for key in ("host", "database", "username"):
            if not isinstance(connection.get(key), str) or not connection[key].strip():
                issues.append(PreflightIssue("CONFIG_REQUIRED", f"Missing required {role}.connection.{key}."))
        port = connection.get("port", 5432)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            issues.append(PreflightIssue("CONFIG_INVALID", f"{role}.connection.port must be an integer between 1 and 65535."))
        if not connection.get("password_secret") and not connection.get("password"):
            issues.append(
                PreflightIssue(
                    "SECRET_REFERENCE_MISSING",
                    f"Configure {role}.connection.password_secret or provide a password through the configured secret provider.",
                )
            )
    return issues


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _schema_summary(schema: Schema) -> dict[str, Any]:
    return {
        "columns": [
            {
                "name": column.name,
                "type": column.source_type,
                "nullable": column.nullable,
                "default": column.default,
                "generated": column.generated,
            }
            for column in schema.columns
        ],
        "primary_key": schema.primary_key,
    }


def compare_schemas(source: Schema, target: Schema) -> SchemaComparison:
    differences: list[str] = []
    source_columns = {column.name: column for column in source.columns}
    target_columns = {column.name: column for column in target.columns}

    for name in sorted(source_columns.keys() - target_columns.keys()):
        differences.append(f"Target is missing column {name!r}.")
    for name in sorted(target_columns.keys() - source_columns.keys()):
        differences.append(f"Target has unexpected column {name!r}.")
    for name in sorted(source_columns.keys() & target_columns.keys()):
        source_column = source_columns[name]
        target_column = target_columns[name]
        if _normalize(source_column.source_type) != _normalize(target_column.source_type):
            differences.append(
                f"Column {name!r} type differs: source={source_column.source_type!r}, "
                f"target={target_column.source_type!r}."
            )
        if source_column.nullable != target_column.nullable:
            differences.append(f"Column {name!r} nullability differs.")
        if _normalize(source_column.default) != _normalize(target_column.default):
            differences.append(f"Column {name!r} default differs.")
        if _normalize(source_column.generated) != _normalize(target_column.generated):
            differences.append(f"Column {name!r} generated expression differs.")

    if source.primary_key != target.primary_key:
        differences.append(
            f"Primary key differs: source={source.primary_key!r}, target={target.primary_key!r}."
        )
    return SchemaComparison("COMPATIBLE" if not differences else "MISMATCH", differences)


def decide_action(
    target_exists: bool,
    comparison: SchemaComparison | None,
    blockers: list[str],
) -> str:
    if blockers or (comparison is not None and comparison.status == "MISMATCH"):
        return BLOCK
    if not target_exists:
        return CREATE_AND_MIGRATE
    return MIGRATE


class PostgresMigrationPlanner:
    """Builds a read-only PostgreSQL-to-PostgreSQL migration plan."""

    def __init__(self, source: Any, target: Any, config: dict[str, Any]) -> None:
        self._source = source
        self._target = target
        self._config = config

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error)
        return re.sub(r"(?i)(password\s*=\s*)[^\s;]+", r"\1[redacted]", message)

    def build(self) -> MigrationPlan:
        plan = MigrationPlan()
        plan.blockers.extend(validate_postgresql_full_config(self._config))
        if plan.blockers:
            return plan

        try:
            self._source.connect()
            plan.source_connected = True
        except Exception as exc:
            plan.blockers.append(
                PreflightIssue("SOURCE_CONNECT_FAILED", f"Unable to connect to PostgreSQL source: {self._safe_error(exc)}")
            )
            return plan

        try:
            self._target.connect()
            plan.target_connected = True
        except Exception as exc:
            plan.blockers.append(
                PreflightIssue("TARGET_CONNECT_FAILED", f"Unable to connect to PostgreSQL target: {self._safe_error(exc)}")
            )
            return plan

        try:
            object_names = self._source.list_objects()
        except Exception as exc:
            plan.blockers.append(
                PreflightIssue("SOURCE_DISCOVERY_FAILED", f"Unable to discover source objects: {self._safe_error(exc)}")
            )
            return plan

        source_schemas: list[Schema] = []
        for object_name in object_names:
            try:
                source_schemas.append(self._source.get_schema(object_name))
            except Exception as exc:
                plan.objects.append(
                    ObjectMigrationPlan(
                        object_name=object_name,
                        schema_name="public",
                        blockers=[f"Unable to read source schema: {self._safe_error(exc)}"],
                    )
                )

        schema_names = sorted({schema.schema_name or "public" for schema in source_schemas})
        try:
            target_objects = set(self._target.list_objects(schema_names))
        except Exception as exc:
            plan.blockers.append(
                PreflightIssue("TARGET_DISCOVERY_FAILED", f"Unable to discover target objects: {self._safe_error(exc)}")
            )
            return plan

        for source_schema in source_schemas:
            schema_name = source_schema.schema_name or "public"
            key = (schema_name, source_schema.name)
            target_exists = key in target_objects
            object_plan = ObjectMigrationPlan(
                object_name=source_schema.name,
                schema_name=schema_name,
                target_exists=target_exists,
                source_schema=_schema_summary(source_schema),
                dependencies=sorted({fk.ref_table for fk in source_schema.foreign_keys}),
            )
            try:
                object_plan.source_row_count = self._source.get_object_count(source_schema.name)
            except Exception as exc:
                object_plan.blockers.append(f"Unable to count source rows: {self._safe_error(exc)}")

            if target_exists:
                try:
                    target_schema = self._target.inspect_schema(source_schema.name, schema_name)
                    if target_schema is None:
                        object_plan.blockers.append("Target object disappeared during preflight.")
                    else:
                        object_plan.target_schema = _schema_summary(target_schema)
                        object_plan.schema_comparison = compare_schemas(source_schema, target_schema)
                        object_plan.blockers.extend(object_plan.schema_comparison.differences)
                        object_plan.target_row_count = self._target.get_object_count(source_schema.name)
                        if object_plan.target_row_count:
                            object_plan.warnings.append(
                                "Target already contains rows; migration uses the existing upsert behavior."
                            )
                except Exception as exc:
                    object_plan.blockers.append(f"Unable to inspect target object: {self._safe_error(exc)}")
            else:
                object_plan.schema_comparison = SchemaComparison("TARGET_MISSING")

            object_plan.action = decide_action(
                object_plan.target_exists, object_plan.schema_comparison, object_plan.blockers
            )
            object_plan.readiness = "READY" if object_plan.action != BLOCK else "BLOCKED"
            plan.objects.append(object_plan)

        return plan
