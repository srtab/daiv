from __future__ import annotations

import json
import math
from typing import Any

_JSON_TYPES_BY_PYTHON_TYPE: dict[type, set[str]] = {
    int: {"integer", "number"},
    float: {"number"},
    bool: {"boolean"},
    list: {"array"},
    dict: {"object"},
    type(None): {"null"},
}


def _finite_float(text: str) -> float:
    """Reject non-finite numbers: MCP clients serialize ``nan``/``inf`` as ``null``, which would turn a clear
    type rejection into a silently wrong argument."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def _allowed_types(prop: object) -> set[str] | None:
    """JSON-schema types a property accepts, or ``None`` if it (or any ``anyOf``/``oneOf`` branch) declares no
    usable ``type``."""
    if not isinstance(prop, dict):
        return None
    declared = prop.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared) if all(isinstance(t, str) for t in declared) else None
    branches = prop.get("anyOf") or prop.get("oneOf")
    if not isinstance(branches, list):
        return None
    allowed: set[str] = set()
    for branch in branches:
        branch_types = _allowed_types(branch)
        if branch_types is None:
            return None
        allowed |= branch_types
    return allowed


def decode_stringified_args(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Decode top-level string args into the non-string JSON type the schema declares for them.

    Returns only the args it decoded: a value qualifies when the schema rules out a string for that key and the
    decoded value satisfies one of its declared types.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    decoded: dict[str, Any] = {}
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        allowed = _allowed_types(properties.get(key))
        if not allowed or "string" in allowed:
            continue
        try:
            candidate = json.loads(value, parse_float=_finite_float, parse_constant=_finite_float)
        except ValueError, RecursionError:
            continue
        if _JSON_TYPES_BY_PYTHON_TYPE.get(type(candidate), set()) & allowed:
            decoded[key] = candidate
    return decoded
