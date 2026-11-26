from __future__ import annotations

from typing import Any

from core.schema_mapping.type_map import check_size_limit, map_type


class TypeMappingRegistry:
    def __init__(self) -> None:
        self._custom_mappings: dict[str, str] = {}

    def register(self, source_engine: str, target_engine: str, source_type: str, target_type: str) -> None:
        key = f"{source_engine}:{target_engine}:{source_type}"
        self._custom_mappings[key] = target_type

    def map_type(self, source_engine: str, target_engine: str, source_type: str) -> str | None:
        key = f"{source_engine}:{target_engine}:{source_type}"
        if key in self._custom_mappings:
            return self._custom_mappings[key]
        return map_type(source_engine, target_engine, source_type)

    def check_size_limit(self, target_engine: str, size_bytes: int) -> bool:
        return check_size_limit(target_engine, size_bytes)

    def get_supported_engines(self) -> list[str]:
        return ["postgresql", "mysql", "mongodb", "mssql", "oracle", "cosmos_mongo"]

    def validate_compatibility(self, source_engine: str, target_engine: str, source_type: str) -> dict[str, Any]:
        target_type = self.map_type(source_engine, target_engine, source_type)
        compatible = target_type is not None
        warning = None
        if not compatible and source_type not in ("bytea", "geometry", "geography"):
            warning = f"Unsupported type mapping: {source_engine}:{source_type} -> {target_engine}"
        return {
            "source_engine": source_engine,
            "target_engine": target_engine,
            "source_type": source_type,
            "target_type": target_type,
            "compatible": compatible,
            "warning": warning,
        }


class TypeMapper:
    def __init__(self) -> None:
        self._registry = TypeMappingRegistry()

    def map_type(
        self, source_engine: str, target_engine: str, source_type: str
    ) -> str | None:
        return self._registry.map_type(source_engine, target_engine, source_type)

    def check_size_limit(self, target_engine: str, size_bytes: int) -> bool:
        return self._registry.check_size_limit(target_engine, size_bytes)

    def validate_compatibility(
        self, source_engine: str, target_engine: str, source_type: str
    ) -> dict[str, Any]:
        return self._registry.validate_compatibility(source_engine, target_engine, source_type)
