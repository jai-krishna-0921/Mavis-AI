# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
# Portions adapted from Hermes Agent (https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous
# Research, MIT License.
"""A tool's JSON input schema as a server sends it, made one predictable shape; its arguments checked.

Adapted from Hermes Agent: `tools/mcp_tool_schema.py` (`_normalize_mcp_input_schema`, `_rewrite_local_refs`,
`_repair_object_shape`) and `tools/schema_sanitizer.py` (`strip_nullable_unions`, `collapse_const_unions`).
The schema is never given to the model as a function schema (the model calls `mcp_read` / `mcp_act` with an
`arguments` object); it is stored, summarised for `mcp_tools` and used to validate arguments before a call.

Pure functions. Every function returns new data and never mutates its input."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from mavis.mcp.text import clean_description

EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_UNION_KEYS = ("anyOf", "oneOf")
_UNION_META_KEYS = ("title", "description", "default", "examples")  # copied onto replacements
# Keywords whose value maps entry NAMES to schemas: the map itself is never a schema node, so a property
# literally named `properties`, `required` or `definitions` is a name, not a keyword (Hermes #110530).
_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")


def _rewrite(schema: Any, fn: Callable[[dict], Any]) -> Any:
    """Bottom-up map over a schema tree: lists and dicts recurse, then `fn` sees each dict."""
    if isinstance(schema, list):
        return [_rewrite(item, fn) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return fn({k: _rewrite(v, fn) for k, v in schema.items()})


def rewrite_local_refs(node: Any) -> Any:
    """Promote legacy `definitions` to `$defs`, only where it is a JSON Schema keyword and never as a
    property NAME inside `properties` or `patternProperties`; `#/definitions/x` refs follow."""
    if isinstance(node, list):
        return [rewrite_local_refs(item) for item in node]
    if not isinstance(node, dict):
        return node
    normalized: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "patternProperties") and isinstance(value, dict):
            normalized[key] = {name: rewrite_local_refs(schema) for name, schema in value.items()}
        else:
            normalized["$defs" if key == "definitions" else key] = rewrite_local_refs(value)
    ref = normalized.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
    return normalized


def repair_object_shape(node: Any, *, object_root: bool = False) -> Any:
    """Fill a missing object `type`, make sure `properties` exists and prune `required` to names that are
    present. A dict that carries `required` but no `properties` and no `type` is a constraint fragment ("at
    least one of these keys", inside anyOf/oneOf/allOf), not an object declaration: it is left alone, because
    repairing it destroys it. Only the schema root is always repaired."""
    if isinstance(node, list):
        return [repair_object_shape(item) for item in node]
    if not isinstance(node, dict):
        return node
    repaired: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            repaired[key] = {name: repair_object_shape(schema) for name, schema in value.items()}
        else:
            repaired[key] = repair_object_shape(value)
    if object_root or "properties" in repaired or "type" in node:
        if not repaired.get("type") and ("properties" in repaired or "required" in repaired):
            repaired["type"] = "object"
        if repaired.get("type") == "object":
            if not isinstance(repaired.get("properties"), dict):
                repaired["properties"] = {}
            required = repaired.get("required")
            props = repaired["properties"]
            repaired["required"] = ([r for r in required if isinstance(r, str) and r in props]
                                    if isinstance(required, list) else [])
    return repaired


def _is_null_branch(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "null"


def _carry_union_meta(outer: dict, replacement: dict, *, skip_default_on_ref: bool) -> None:
    for meta_key in _UNION_META_KEYS:
        if meta_key in outer and meta_key not in replacement and not (
                skip_default_on_ref and meta_key == "default" and "$ref" in replacement):
            replacement[meta_key] = outer[meta_key]


def strip_nullable_unions(schema: Any, *, keep_nullable_hint: bool = True) -> Any:
    """Collapse `anyOf`/`oneOf` nullable unions (how optional fields arrive from Pydantic and Zod servers) to
    the single non-null branch. Optionality already lives in the parent's `required`. Only when a null branch
    was dropped AND exactly one non-null branch survives. `nullable: true` is kept as a hint."""
    def collapse(stripped: dict) -> Any:
        for key in _UNION_KEYS:
            variants = stripped.get(key)
            if not isinstance(variants, list):
                continue
            non_null = [item for item in variants if not _is_null_branch(item)]
            if len(non_null) == 1 and len(non_null) != len(variants):
                replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
                if keep_nullable_hint:
                    replacement.setdefault("nullable", True)
                _carry_union_meta(stripped, replacement, skip_default_on_ref=True)
                return _rewrite(replacement, collapse)  # the survivor may itself be a union
        return stripped
    return _rewrite(schema, collapse)


_CONST_PRIMITIVE_TYPES: dict[type, str] = {bool: "boolean", int: "integer", float: "number", str: "string"}


def _const_branch_type(branch: Any) -> str | None:
    """Primitive JSON Schema type of a pure `const` branch (a declared `type` must match; only title and
    description may accompany it), else None."""
    if not isinstance(branch, dict) or "const" not in branch \
            or set(branch) - {"const", "type", "title", "description"}:
        return None
    json_type = _CONST_PRIMITIVE_TYPES.get(type(branch["const"]))  # type(): bool is a subclass of int
    return json_type if branch.get("type") in (None, json_type) else None


def collapse_const_unions(schema: Any) -> Any:
    """Collapse `anyOf`/`oneOf` unions of same-typed consts (Rust and TypeScript servers write enums that
    way) to an `enum`. Only when EVERY non-null branch is a pure `const` of one primitive type; one null
    branch is tolerated as `nullable: true`."""
    def collapse(out: dict) -> Any:
        for key in _UNION_KEYS:
            variants = out.get(key)
            if not isinstance(variants, list) or not variants:
                continue
            null_branches = [i for i in variants if _is_null_branch(i) and "const" not in i]
            const_branches = [item for item in variants if item not in null_branches]
            if len(null_branches) > 1 or not const_branches:
                continue
            branch_types = {_const_branch_type(item) for item in const_branches}
            if len(branch_types) != 1 or None in branch_types:
                continue
            replacement: dict = {"type": branch_types.pop(),
                                 "enum": [item["const"] for item in const_branches]}
            if null_branches:
                replacement["nullable"] = True
            _carry_union_meta(out, replacement, skip_default_on_ref=False)
            return replacement
        return out
    return _rewrite(schema, collapse)


def normalize_input_schema(schema: Any) -> dict[str, Any]:
    """One predictable form of a server's `inputSchema`. Order matters: `definitions` becomes `$defs`;
    nullable unions collapse to the non-null branch; same-typed const unions become enums (after the nullable
    strip); then object shapes are repaired (a missing or null `type` on an object-shaped node becomes
    "object", `properties` exists, `required` names only existing properties). Empty or malformed input
    gives an empty object schema."""
    if not schema or not isinstance(schema, dict):
        return copy.deepcopy(EMPTY_OBJECT_SCHEMA)
    normalized = rewrite_local_refs(schema)
    normalized = strip_nullable_unions(normalized, keep_nullable_hint=True)
    normalized = collapse_const_unions(normalized)
    normalized = repair_object_shape(normalized, object_root=True)
    if not isinstance(normalized, dict):
        return copy.deepcopy(EMPTY_OBJECT_SCHEMA)
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}
    return normalized


def json_size(value: Any) -> int:
    """Bytes of the compact JSON form."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode())


def _type_label(prop: Any) -> str:
    if not isinstance(prop, dict):
        return "any"
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        shown = "|".join(str(v) for v in prop["enum"][:6])
        return shown + ("|..." if len(prop["enum"]) > 6 else "")
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = "|".join(str(k) for k in kind if k != "null")
    if kind == "array":
        items = prop.get("items")
        return f"array of {_type_label(items)}" if isinstance(items, dict) else "array"
    if isinstance(kind, str):
        return kind
    for key in _UNION_KEYS:
        if isinstance(prop.get(key), list):
            return "|".join(_type_label(b) for b in prop[key][:4])
    return "any"


def argument_summary(schema: dict[str, Any], *, max_chars: int = 400) -> str:
    """A compact one-line argument list for `mcp_tools`: `name* (type): short description; ...` where `*`
    marks a required argument. The text is server-written, so descriptions are cleaned and short."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return "no arguments"
    required = set(schema.get("required") or ())
    parts = []
    for name, prop in props.items():
        label = f"{clean_description(name, 40)}{'*' if name in required else ''} ({_type_label(prop)})"
        desc = clean_description(prop.get("description") if isinstance(prop, dict) else "", 80)
        parts.append(f"{label}: {desc}" if desc else label)
    out = "; ".join(parts)
    return out if len(out) <= max_chars else out[: max_chars - 3].rstrip() + "..."


def drop_null_optionals(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Models write `null` for an argument they mean to leave out. A null for a property that is not
    required is dropped (Hermes coerces the same way); a null for a required one is left for validation to
    report."""
    required = set(schema.get("required") or ())
    return {k: v for k, v in arguments.items() if not (v is None and k not in required)}


def _without_regex(node: Any) -> Any:
    """The schema without `pattern`/`patternProperties`/`format`: a server-written regular expression must
    not run against model text (catastrophic backtracking)."""
    if isinstance(node, list):
        return [_without_regex(i) for i in node]
    if not isinstance(node, dict):
        return node
    out = {}
    dropped_pattern_properties = False
    for key, value in node.items():
        if key in ("pattern", "format") and not isinstance(value, dict):
            continue
        if key == "patternProperties" and isinstance(value, dict):
            dropped_pattern_properties = True
            continue
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {name: _without_regex(sub) for name, sub in value.items()}
        else:
            out[key] = _without_regex(value)
    if dropped_pattern_properties:
        # Keys the dropped pattern would have matched must not trip the closed-object rules.
        out.pop("additionalProperties", None)
        out.pop("unevaluatedProperties", None)
    if out.get("nullable") is True:
        out = _allow_null(out)
    return out


def _allow_null(node: dict[str, Any]) -> dict[str, Any]:
    """Validation copy of a node normalization marked `nullable: true` (a hint Draft 2020-12 ignores): null
    must pass wherever the server's original schema allowed it."""
    node = {k: v for k, v in node.items() if k != "nullable"}
    kind = node.get("type")
    if isinstance(kind, str) or (isinstance(kind, list) and "$ref" not in node):
        node["type"] = [*([kind] if isinstance(kind, str) else kind), "null"]
        if isinstance(node.get("enum"), list):
            node["enum"] = [*node["enum"], None]
        return node
    return {"anyOf": [node, {"type": "null"}]}


def check_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """None when `arguments` satisfy `schema`; otherwise one short sentence the model can act on. A schema
    that is itself unusable never blocks a call (the server will judge the arguments)."""
    try:
        validator = Draft202012Validator(_without_regex(schema))
        errors = sorted(validator.iter_errors(arguments), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # noqa: BLE001 - unresolvable refs and the like: let the server decide
        return None
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.absolute_path)
    message = clean_description(first.message, 200)
    more = len(errors) - 1
    extra = f" (and {more} more problem{'s' if more > 1 else ''})" if more else ""
    return f"Invalid arguments{f' at {where}' if where else ''}: {message}{extra}"
