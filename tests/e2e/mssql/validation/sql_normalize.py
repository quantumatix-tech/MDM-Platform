"""Conservative SQL DDL normalization for catalog-definition comparisons."""
from __future__ import annotations

import re


def normalize_sql_definition(definition: str | None) -> str:
    """Normalize formatting/identifier quoting while preserving string values.

    SQL keywords and unquoted identifiers are case folded, whitespace and
    comments are removed, and bracketed identifiers are unwrapped. Quoted
    string literals are copied byte-for-byte so case-sensitive literal
    changes remain visible in definition comparisons.
    """
    if definition is None:
        return ""
    output: list[str] = []
    i = 0
    pending_space = False
    text = definition
    while i < len(text):
        if text.startswith("--", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end + 1
            pending_space = True
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            pending_space = True
            continue
        char = text[i]
        if char.isspace():
            pending_space = True
            i += 1
            continue
        if pending_space and output:
            output.append(" ")
        pending_space = False
        if char in ("'", '"'):
            quote = char
            start = i
            i += 1
            while i < len(text):
                if text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            output.append(text[start:i])
            continue
        if char == "[":
            i += 1
            identifier = []
            while i < len(text):
                if text[i] == "]":
                    if i + 1 < len(text) and text[i + 1] == "]":
                        identifier.append("]")
                        i += 2
                        continue
                    i += 1
                    break
                identifier.append(text[i])
                i += 1
            output.append("".join(identifier).lower())
            continue
        output.append(char.lower())
        i += 1
    normalized = "".join(output).strip()
    return re.sub(r"^create\s+or\s+alter\s+", "create ", normalized)
