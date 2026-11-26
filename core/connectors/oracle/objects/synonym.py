"""Oracle private synonym discovery and dependency ordering."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.connectors.base import SynonymDef
from core.connectors.oracle._models import validate_oracle_identifier


def discover_synonyms(conn: Any, owner: str) -> list[SynonymDef]:
    """Discover private synonyms, rejecting unresolved and external targets."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT synonym_name, table_owner, table_name, db_link "
            "FROM user_synonyms ORDER BY synonym_name"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT synonym_name, table_owner, table_name, db_link "
            "FROM all_synonyms WHERE owner = 'PUBLIC' AND table_owner = :1 "
            "ORDER BY synonym_name",
            (owner,),
        )
        relevant_public = cur.fetchall()
        cur.execute("SELECT object_name, object_type FROM user_objects")
        object_rows = cur.fetchall()
    if relevant_public:
        names = ", ".join(str(row[0]) for row in relevant_public)
        raise RuntimeError(
            "Oracle FULL cannot safely preserve PUBLIC synonym(s) that reference "
            f"managed source-owner objects: {names}; PUBLIC synonyms are never modified"
        )
    object_types: dict[str, set[str]] = defaultdict(set)
    for name, kind in object_rows:
        object_types.setdefault(str(name).casefold(), set()).add(str(kind).upper())
    names = {str(row[0]).casefold() for row in rows}
    dependencies: dict[str, str] = {}
    definitions: dict[str, SynonymDef] = {}
    for name, target_owner, target_name, db_link in rows:
        synonym_name = str(name)
        validate_oracle_identifier(synonym_name, "synonym")
        if db_link:
            raise RuntimeError(
                f"Oracle FULL does not support database-link synonym {owner}.{synonym_name}"
            )
        if not target_owner or not target_name:
            raise RuntimeError(
                f"Oracle FULL cannot resolve synonym {owner}.{synonym_name}: target owner/name is missing"
            )
        if str(target_owner).casefold() != owner.casefold():
            raise RuntimeError(
                f"Oracle FULL cannot migrate cross-owner synonym {owner}.{synonym_name} "
                f"to {target_owner}.{target_name}"
            )
        validate_oracle_identifier(str(target_name), "synonym target")
        target_types = object_types.get(str(target_name).casefold(), set())
        if not target_types:
            raise RuntimeError(
                f"Oracle FULL cannot resolve synonym {owner}.{synonym_name}: "
                f"target {target_owner}.{target_name} does not exist"
            )
        supported_target_types = {
            "TABLE",
            "VIEW",
            "MATERIALIZED VIEW",
            "SEQUENCE",
            "FUNCTION",
            "PROCEDURE",
            "PACKAGE",
            "SYNONYM",
        }
        if not target_types.intersection(supported_target_types):
            raise RuntimeError(
                f"Oracle FULL cannot preserve synonym {owner}.{synonym_name}: "
                f"target object type(s) {', '.join(sorted(target_types))} are unsupported"
            )
        if "SYNONYM" in target_types:
            target_key = str(target_name).casefold()
            if target_key not in names:
                raise RuntimeError(
                    f"Oracle FULL cannot resolve synonym chain {owner}.{synonym_name} "
                    f"through unsupported target {target_name}"
                )
            dependencies[synonym_name.casefold()] = target_key
        definitions[synonym_name.casefold()] = SynonymDef(
            name=synonym_name,
            schema_name=owner,
            base_object=f"{target_owner}.{target_name}",
            base_owner=str(target_owner),
            base_name=str(target_name),
        )
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise RuntimeError(f"Oracle synonym dependency cycle detected at {owner}.{name}")
        if name in visited:
            return
        visiting.add(name)
        if name in dependencies:
            visit(dependencies[name])
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in sorted(definitions):
        visit(name)
    return [definitions[name] for name in ordered]
