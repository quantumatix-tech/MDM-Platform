from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    SourceConnector,
    TargetConnector,
    CDCEngine,
    Schema,
    Column,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


class MongoSourceConnector(SourceConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._db: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pymongo")
        from pymongo import MongoClient

        host = self._config["host"]
        port = self._config.get("port", 27017)
        username = self._config.get("username")
        password = self._config.get("password")
        database = self._config["database"]

        validate_identifier(database, "database")

        if username and password:
            self._client = MongoClient(
                host=host,
                port=port,
                username=username,
                password=password,
                tls=self._config.get("ssl", True),
                tlsAllowInvalidCertificates=False,
            )
        else:
            self._client = MongoClient(host=host, port=port)

        self._db = self._client[database]
        audit_log(phase="connect", status="success", details={"engine": "mongodb", "role": "source"})

    def list_objects(self) -> list[str]:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        collections = self._db.list_collection_names()
        for c in collections:
            validate_identifier(c, "collection")
        return collections

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "collection")
        return self._db[object_name].count_documents({})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "collection")
        cursor = self._db[object_name].find().batch_size(100)
        for doc in cursor:
            yield doc

    def get_schema(self, object_name: str) -> Schema:
        validate_identifier(object_name, "collection")
        sample = self._db[object_name].find_one()
        columns: list[Column] = []
        primary_key: list[str] = ["_id"]

        if sample:
            for key, value in sample.items():
                py_type = type(value).__name__
                mongo_type = {
                    "str": "string",
                    "int": "int32",
                    "float": "double",
                    "bool": "boolean",
                    "dict": "object",
                    "list": "array",
                    "ObjectId": "objectId",
                }.get(py_type, "string")
                columns.append(Column(name=key, source_type=mongo_type, target_type=None))

        return Schema(name=object_name, columns=columns, primary_key=primary_key)


class MongoTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._db: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pymongo")
        from pymongo import MongoClient

        host = self._config["host"]
        port = self._config.get("port", 27017)
        username = self._config.get("username")
        password = self._config.get("password")
        database = self._config["database"]

        validate_identifier(database, "database")

        if username and password:
            self._client = MongoClient(
                host=host,
                port=port,
                username=username,
                password=password,
                tls=self._config.get("ssl", True),
                tlsAllowInvalidCertificates=False,
            )
        else:
            self._client = MongoClient(host=host, port=port)

        self._db = self._client[database]
        audit_log(phase="connect", status="success", details={"engine": "mongodb", "role": "target"})

    def ensure_database_exists(self) -> None:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        self._client[db_name].command("ping")

    def create_object_if_missing(self, schema: Schema) -> None:
        validate_identifier(schema.name, "collection")
        existing = self._db.list_collection_names()
        if schema.name in existing:
            return
        self._db.create_collection(schema.name)
        audit_log(phase="create_collection", status="created", details={"collection": schema.name})

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        validate_identifier(object_name, "collection")
        result = UpsertResult()
        batch = list(rows)

        if not batch:
            return result

        from pymongo import ReplaceOne

        operations = []
        for row in batch:
            doc_id = row.pop("_id", None)
            if doc_id is not None:
                filter_doc = {"_id": doc_id}
            else:
                filter_doc = row
            operations.append(ReplaceOne(filter_doc, row, upsert=True))

        try:
            write_result = self._db[object_name].bulk_write(operations, ordered=False)
            result.success_count = write_result.upserted_count + write_result.modified_count
            audit_log(phase="upsert_batch", status="success", details={"collection": object_name, "count": result.success_count})
        except Exception as exc:
            result.failure_count = len(batch)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
            audit_log(phase="upsert_batch", status="failure", details={"collection": object_name, "error": str(exc)})

        return result

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "collection")
        return self._db[object_name].count_documents({})

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        validate_identifier(object_name, "collection")
        doc_id = document.get("_id")
        if doc_id is not None:
            self._db[object_name].delete_one({"_id": doc_id})
        elif schema and schema.primary_key:
            filter_doc = {pk: document.get(pk) for pk in schema.primary_key}
            self._db[object_name].delete_one(filter_doc)
        audit_log(phase="cdc_delete", status="deleted", details={"collection": object_name})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "collection")
        cursor = self._db[object_name].find().batch_size(100)
        for doc in cursor:
            yield doc


class MongoCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._db: Any = None
        self._last_oplog_time: Any = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        ensure_driver("pymongo")
        from pymongo import MongoClient

        host = self._config["host"]
        port = self._config.get("port", 27017)
        username = self._config.get("username")
        password = self._config.get("password")
        database = self._config["database"]

        if username and password:
            self._client = MongoClient(
                host=host,
                port=port,
                username=username,
                password=password,
                tls=self._config.get("ssl", True),
                tlsAllowInvalidCertificates=False,
            )
        else:
            self._client = MongoClient(host=host, port=port)

        self._db = self._client[database]

    def start(self) -> None:
        admin_db = self._client["admin"]
        is_replica = admin_db.command("isMaster").get("ismaster", False)
        if not is_replica:
            raise RuntimeError("MongoDB CDC requires replica set mode")

        oplog = self._client["local"]["oplog.rs"]
        cursor = oplog.find().sort("$natural", -1).limit(1)
        entry = cursor.next()
        self._last_oplog_time = entry["ts"]
        audit_log(phase="cdc_start", status="success", details={"engine": "mongodb"})

    def poll_changes(self) -> list[ChangeEvent]:

        oplog = self._client["local"]["oplog.rs"]
        query = {}
        if self._last_oplog_time is not None:
            query["ts"] = {"$gt": self._last_oplog_time}

        cursor = oplog.find(query).sort("$natural", 1)
        events = []
        for entry in cursor:
            operation = {
                "i": "insert",
                "u": "update",
                "d": "delete",
            }.get(entry.get("op", "i"), "insert")

            document = entry.get("o", {})
            if operation == "update" and "o2" in entry:
                document["_id"] = entry["o2"].get("_id")

            ns = entry.get("ns", "")
            object_name = ns.split(".", 1)[-1] if "." in ns else ns

            events.append(
                ChangeEvent(
                    operation=operation,
                    document=document,
                    object_name=object_name,
                    watermark=entry.get("ts"),
                )
            )

        return events

    def apply(self, events: list[ChangeEvent], target: TargetConnector) -> ApplyResult:
        result = ApplyResult()
        if not events:
            return result

        for event in events:
            collection_name = event.object_name or "unknown"
            try:
                if event.operation == "insert":
                    target.upsert_batch(collection_name, iter([event.document]), event.schema)
                elif event.operation == "update":
                    target.upsert_batch(collection_name, iter([event.document]), event.schema)
                elif event.operation == "delete":
                    target.delete(collection_name, event.document, event.schema)
                result.success_count += 1
            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0:
            result.last_checkpoint = events[-1].watermark
            audit_log(phase="cdc_apply", status="success", details={"applied": result.success_count})
        else:
            audit_log(phase="cdc_apply", status="partial_failure", details={"success": result.success_count, "failure": result.failure_count})

        return result

    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        self._last_oplog_time = result.last_checkpoint
        audit_log(phase="cdc_checkpoint", status="advanced", details={"watermark": str(result.last_checkpoint)})