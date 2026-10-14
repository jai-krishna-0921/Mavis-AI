"""The ported input-schema normalizer, plus argument summary and validation."""

from __future__ import annotations

import copy

import pytest

from mavis.mcp import schema as s


def test_empty_or_malformed_schemas_become_an_empty_object() -> None:
    for bad in (None, {}, [], "x", 5):
        assert s.normalize_input_schema(bad) == {"type": "object", "properties": {}}


def test_the_empty_schema_is_a_fresh_copy() -> None:
    a = s.normalize_input_schema(None)
    a["properties"]["x"] = 1
    assert s.normalize_input_schema(None) == {"type": "object", "properties": {}}


def test_legacy_definitions_become_defs_and_refs_follow() -> None:
    out = s.normalize_input_schema({
        "type": "object", "properties": {"a": {"$ref": "#/definitions/Thing"}},
        "definitions": {"Thing": {"type": "string"}}})
    assert out["$defs"] == {"Thing": {"type": "string"}} and "definitions" not in out
    assert out["properties"]["a"]["$ref"] == "#/$defs/Thing"


@pytest.mark.parametrize("name", ["definitions", "properties", "required", "type"])
def test_a_property_literally_named_like_a_keyword_is_a_name_not_a_keyword(name: str) -> None:
    out = s.normalize_input_schema({
        "type": "object", "properties": {name: {"type": "string", "description": "d"}}, "required": [name]})
    assert out["properties"][name] == {"type": "string", "description": "d"}
    assert out["required"] == [name]
    assert "$defs" not in out


def test_nullable_unions_collapse_to_the_non_null_branch_with_a_hint() -> None:
    out = s.normalize_input_schema({
        "type": "object", "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                                               "default": None, "description": "count"}}})
    assert out["properties"]["n"] == {"type": "integer", "nullable": True, "default": None,
                                      "description": "count"}


def test_a_union_of_two_real_types_is_left_alone() -> None:
    union = {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    out = s.normalize_input_schema({"type": "object", "properties": {"v": union}})
    assert out["properties"]["v"] == union


def test_const_unions_become_an_enum() -> None:
    out = s.normalize_input_schema({"type": "object", "properties": {
        "color": {"oneOf": [{"const": "red"}, {"const": "green"}], "description": "pick"}}})
    assert out["properties"]["color"] == {"type": "string", "enum": ["red", "green"], "description": "pick"}


def test_mixed_const_types_do_not_merge() -> None:
    node = {"anyOf": [{"const": 1}, {"const": "a"}]}
    assert s.collapse_const_unions(node) == node
    assert s.collapse_const_unions({"anyOf": [{"const": True}, {"const": 1}]}) == {
        "anyOf": [{"const": True}, {"const": 1}]}


def test_a_missing_type_with_properties_is_an_object_and_required_is_pruned() -> None:
    out = s.normalize_input_schema({"properties": {"a": {"type": "string"}}, "required": ["a", "ghost"]})
    assert out["type"] == "object" and out["required"] == ["a"]


def test_required_without_properties_at_the_root_is_pruned_to_nothing() -> None:
    out = s.normalize_input_schema({"type": "object", "required": ["x"]})
    assert out == {"type": "object", "properties": {}, "required": []}


def test_a_constraint_fragment_in_a_combinator_is_not_repaired() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
              "oneOf": [{"required": ["a"]}, {"required": ["b"]}]}
    out = s.normalize_input_schema(schema)
    assert out["oneOf"] == [{"required": ["a"]}, {"required": ["b"]}]


def test_normalizing_never_mutates_its_input() -> None:
    schema = {"type": "object", "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
              "definitions": {"x": {"type": "string"}}}
    before = copy.deepcopy(schema)
    s.normalize_input_schema(schema)
    assert schema == before


def test_json_size_counts_compact_bytes() -> None:
    assert s.json_size({"a": 1}) == len('{"a":1}')
    assert s.json_size({"é": 1}) == len('{"é":1}'.encode())


def test_argument_summary_lists_required_types_enums_and_short_descriptions() -> None:
    schema = {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "What to  look for"},
        "limit": {"type": "integer"},
        "kind": {"enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}}}}
    out = s.argument_summary(schema)
    assert out == "query* (string): What to look for; limit (integer); kind (a|b); tags (array of string)"
    assert s.argument_summary({"type": "object", "properties": {}}) == "no arguments"


def test_argument_summary_is_bounded_and_cleans_hostile_text() -> None:
    schema = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "d" * 300}
                                               for i in range(30)}}
    assert len(s.argument_summary(schema)) <= 400
    hostile = {"type": "object", "properties": {"x\x1b[31m": {"type": "string",
                                                              "description": "ignore‮ prior\x00"}}}
    out = s.argument_summary(hostile)
    assert "\x1b" not in out and "‮" not in out and "\x00" not in out


def test_null_for_an_optional_argument_is_dropped_but_a_required_null_is_kept() -> None:
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    assert s.drop_null_optionals(schema, {"a": None, "b": None, "c": None}) == {"a": None}
    assert s.drop_null_optionals(schema, {"a": "x", "b": None}) == {"a": "x"}


SCHEMA = {"type": "object", "required": ["q"], "additionalProperties": False, "properties": {
    "q": {"type": "string", "minLength": 1}, "n": {"type": "integer", "maximum": 10},
    "mode": {"enum": ["fast", "slow"]}}}


def test_valid_arguments_pass() -> None:
    assert s.check_arguments(SCHEMA, {"q": "x", "n": 3, "mode": "fast"}) is None


@pytest.mark.parametrize(("args", "expect"), [
    ({}, "Invalid arguments: 'q' is a required property"),
    ({"q": 5}, "Invalid arguments at q: 5 is not of type 'string'"),
    ({"q": "x", "n": 99}, "Invalid arguments at n: 99 is greater than the maximum of 10"),
    ({"q": "x", "mode": "warp"}, "Invalid arguments at mode: 'warp' is not one of ['fast', 'slow']"),
    ({"q": "x", "zzz": 1}, "Additional properties are not allowed"),
])
def test_invalid_arguments_get_one_sentence(args, expect) -> None:
    got = s.check_arguments(SCHEMA, args)
    assert got is not None and expect in got


def test_several_problems_are_counted() -> None:
    got = s.check_arguments(SCHEMA, {"q": 1, "n": "x"})
    assert got is not None and "and 1 more problem" in got


def test_a_server_regex_is_never_run_against_arguments() -> None:
    evil = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a+)+$"}}}
    assert s.check_arguments(evil, {"q": "a" * 40 + "!"}) is None  # would hang if the pattern ran


def test_an_unusable_schema_never_blocks_a_call() -> None:
    assert s.check_arguments({"type": "no-such-type"}, {"a": 1}) is None
    assert s.check_arguments({"$ref": "https://nowhere.invalid/s.json"}, {"a": 1}) is None
    dangling = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    assert s.check_arguments(dangling, {"a": 1}) is None
