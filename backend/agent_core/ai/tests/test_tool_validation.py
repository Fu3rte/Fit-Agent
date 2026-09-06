"""Tests for tool_validation.py — port of pi-package/ai/src/utils/validation.ts.

Covers the plan §11.2 contracts: null vs absent, optional non-nullable null
deletion, required-null judged per schema/coercion, anyOf/oneOf member order
and failure fallback, bool/int/string conversion boundaries, and "no JSON
Schema default application".
"""

from agent_core.ai.tool_validation import (
    ToolNotFoundError,
    ToolArgumentsValidationError,
    coerce_primitive_by_type,
    format_validation_path,
    get_validator,
    normalize_optional_nulls,
    validate_tool_arguments,
    validate_tool_call,
)
from agent_core.ai.types import Tool, ToolCall


def make_tool(parameters, name="echo"):
    return Tool(name=name, description="test tool", parameters=parameters)


def make_call(arguments, name="echo"):
    return ToolCall(id="call-1", name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# coerce_primitive_by_type boundaries (§11.2: bool/int/string 边界)
# ---------------------------------------------------------------------------


class TestCoerceNumber:
    def test_numeric_string(self):
        assert coerce_primitive_by_type("42", "number") == 42

    def test_numeric_string_with_whitespace(self):
        assert coerce_primitive_by_type("  42  ", "number") == 42

    def test_exponent_string(self):
        assert coerce_primitive_by_type("1e3", "number") == 1000.0

    def test_hex_string(self):
        # JS Number("0x10") === 16
        assert coerce_primitive_by_type("0x10", "number") == 16

    def test_float_string(self):
        assert coerce_primitive_by_type("4.5", "number") == 4.5

    def test_non_numeric_string_unchanged(self):
        assert coerce_primitive_by_type("abc", "number") == "abc"

    def test_empty_string_unchanged(self):
        # TS guards value.trim() !== "" before parsing
        assert coerce_primitive_by_type("", "number") == ""
        assert coerce_primitive_by_type("   ", "number") == "   "

    def test_partial_numeric_string_unchanged(self):
        assert coerce_primitive_by_type("12abc", "number") == "12abc"

    def test_underscore_separated_string_unchanged(self):
        # JS Number("1_000") is NaN
        assert coerce_primitive_by_type("1_000", "number") == "1_000"

    def test_infinity_string_unchanged(self):
        # Number.isFinite rejects Infinity
        assert coerce_primitive_by_type("Infinity", "number") == "Infinity"

    def test_boolean_true_becomes_one(self):
        assert coerce_primitive_by_type(True, "number") == 1

    def test_boolean_false_becomes_zero(self):
        assert coerce_primitive_by_type(False, "number") == 0

    def test_null_becomes_zero(self):
        assert coerce_primitive_by_type(None, "number") == 0

    def test_number_unchanged(self):
        assert coerce_primitive_by_type(3.5, "number") == 3.5


class TestCoerceInteger:
    def test_integer_string(self):
        assert coerce_primitive_by_type("42", "integer") == 42

    def test_fractional_string_unchanged(self):
        assert coerce_primitive_by_type("4.5", "integer") == "4.5"

    def test_hex_string(self):
        assert coerce_primitive_by_type("0x1A", "integer") == 26

    def test_null_becomes_zero(self):
        assert coerce_primitive_by_type(None, "integer") == 0

    def test_boolean_becomes_one_or_zero(self):
        assert coerce_primitive_by_type(True, "integer") == 1
        assert coerce_primitive_by_type(False, "integer") == 0

    def test_integer_number_unchanged(self):
        assert coerce_primitive_by_type(7, "integer") == 7


class TestCoerceBoolean:
    def test_true_string(self):
        assert coerce_primitive_by_type("true", "boolean") is True

    def test_false_string(self):
        assert coerce_primitive_by_type("false", "boolean") is False

    def test_other_strings_unchanged(self):
        # Only exact "true"/"false" convert; "1" must NOT become true.
        assert coerce_primitive_by_type("1", "boolean") == "1"
        assert coerce_primitive_by_type("yes", "boolean") == "yes"

    def test_number_one_becomes_true(self):
        assert coerce_primitive_by_type(1, "boolean") is True

    def test_float_one_becomes_true(self):
        # JS 1.0 === 1
        assert coerce_primitive_by_type(1.0, "boolean") is True

    def test_number_zero_becomes_false(self):
        assert coerce_primitive_by_type(0, "boolean") is False

    def test_other_numbers_unchanged(self):
        assert coerce_primitive_by_type(2, "boolean") == 2

    def test_null_becomes_false(self):
        assert coerce_primitive_by_type(None, "boolean") is False

    def test_bool_unchanged(self):
        assert coerce_primitive_by_type(True, "boolean") is True


class TestCoerceString:
    def test_integer_becomes_string(self):
        assert coerce_primitive_by_type(42, "string") == "42"

    def test_float_becomes_string(self):
        assert coerce_primitive_by_type(3.5, "string") == "3.5"

    def test_boolean_becomes_lowercase_string(self):
        assert coerce_primitive_by_type(True, "string") == "true"
        assert coerce_primitive_by_type(False, "string") == "false"

    def test_null_becomes_empty_string(self):
        assert coerce_primitive_by_type(None, "string") == ""

    def test_string_unchanged(self):
        assert coerce_primitive_by_type("x", "string") == "x"


class TestCoerceNull:
    def test_empty_string_becomes_null(self):
        assert coerce_primitive_by_type("", "null") is None

    def test_zero_becomes_null(self):
        assert coerce_primitive_by_type(0, "null") is None
        assert coerce_primitive_by_type(0.0, "null") is None

    def test_false_becomes_null(self):
        assert coerce_primitive_by_type(False, "null") is None

    def test_string_zero_unchanged(self):
        # "0" is not === 0 in TS
        assert coerce_primitive_by_type("0", "null") == "0"

    def test_true_unchanged(self):
        assert coerce_primitive_by_type(True, "null") is True

    def test_none_unchanged(self):
        assert coerce_primitive_by_type(None, "null") is None


# ---------------------------------------------------------------------------
# normalizeOptionalNulls (§11.2: optional 非 nullable 字段的 null 删除)
# ---------------------------------------------------------------------------


class TestNormalizeOptionalNulls:
    def test_optional_null_deleted(self):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        value = {"a": None, "b": 1}
        normalize_optional_nulls(value, schema)
        assert value == {"b": 1}

    def test_required_null_kept(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
        }
        value = {"a": None}
        normalize_optional_nulls(value, schema)
        assert value == {"a": None}

    def test_nullable_subschema_kept(self):
        schema = {
            "type": "object",
            "properties": {"a": {"anyOf": [{"type": "null"}, {"type": "integer"}]}},
        }
        value = {"a": None}
        normalize_optional_nulls(value, schema)
        assert value == {"a": None}

    def test_type_list_with_null_kept(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": ["null", "integer"]}},
        }
        value = {"a": None}
        normalize_optional_nulls(value, schema)
        assert value == {"a": None}

    def test_ref_property_never_deleted(self):
        # TS explicitly skips deletion when propertySchema.$ref is a string.
        schema = {"type": "object", "properties": {"x": {"$ref": "#/defs/x"}}}
        value = {"x": None}
        normalize_optional_nulls(value, schema)
        assert value == {"x": None}

    def test_recurses_into_nested_objects(self):
        schema = {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {"deep": {"type": "string"}},
                }
            },
        }
        value = {"inner": {"deep": None}}
        normalize_optional_nulls(value, schema)
        assert value == {"inner": {}}

    def test_recurses_into_array_items(self):
        schema = {
            "type": "object",
            "properties": {
                "list": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}}
            },
        }
        value = {"list": [{"n": None}, {"n": 1}]}
        normalize_optional_nulls(value, schema)
        assert value == {"list": [{}, {"n": 1}]}


# ---------------------------------------------------------------------------
# Union coercion (§11.2: anyOf/oneOf 成员顺序与失败回退)
# ---------------------------------------------------------------------------


class TestUnionCoercion:
    def test_anyof_member_order_first_match_wins(self):
        # First-pass match: "42" matches the string member, so it must stay a
        # string even though the integer member could also coerce it. The
        # union sits at the property level, where value coercion applies.
        schema = {
            "type": "object",
            "properties": {
                "v": {"anyOf": [{"type": "string"}, {"type": "integer"}]}
            },
        }
        args = {"v": "42"}
        assert validate_tool_arguments(make_tool(schema), make_call(args)) == {"v": "42"}

    def test_top_level_anyof_never_matches_object_arguments(self):
        # TS parity: a bare top-level anyOf of scalar types applies to the
        # whole arguments object; a dict matches neither member and fails.
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        try:
            validate_tool_arguments(make_tool(schema), make_call({"v": "42"}))
        except ToolArgumentsValidationError as exc:
            assert "must match a schema in anyOf" in str(exc)
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_anyof_failure_fallback_coerces_clone(self):
        # No member matches as-is; the first member (integer) succeeds after
        # coercion of a clone, and the result is merged back into args.
        schema = {
            "type": "object",
            "anyOf": [
                {"type": "object", "properties": {"v": {"type": "integer"}}},
                {"type": "object", "properties": {"v": {"type": "integer", "minimum": 5}}},
            ],
        }
        original = {"v": "3"}
        result = validate_tool_arguments(make_tool(schema), make_call(original))
        assert result == {"v": 3}
        # original arguments must be untouched (args is a structuredClone)
        assert original == {"v": "3"}

    def test_anyof_fallback_tries_later_member_when_earlier_fails_check(self):
        schema = {
            "type": "object",
            "properties": {
                "v": {
                    "anyOf": [
                        {"type": "integer", "minimum": 5},
                        {"type": "string"},
                    ]
                }
            },
        }
        args = {"v": "3"}
        # First pass: "3" matches neither. Second pass: integer member coerces
        # to 3 but fails minimum=5; string member accepts as-is.
        assert validate_tool_arguments(make_tool(schema), make_call(args)) == {"v": "3"}

    def test_checker_oneof_requires_exactly_one_match(self):
        validator = get_validator({"oneOf": [{"type": "integer"}, {"type": "number"}]})
        # 3 matches both members -> oneOf fails
        assert validator.check(3) is False
        assert validator.check("x") is False
        # only... both members accept numbers, so nothing satisfies exactly one here;
        # use a disjoint pair for the success case
        disjoint = get_validator({"oneOf": [{"type": "string"}, {"type": "integer"}]})
        assert disjoint.check(3) is True
        assert disjoint.check("x") is True

    def test_checker_anyof_requires_at_least_one_match(self):
        validator = get_validator({"anyOf": [{"type": "string"}, {"type": "integer"}]})
        assert validator.check("x") is True
        assert validator.check(3) is True
        assert validator.check(True) is False

    def test_checker_does_not_coerce(self):
        # Check performs no conversion: "42" is not an integer to the checker.
        validator = get_validator({"type": "integer"})
        assert validator.check("42") is False
        assert validator.check(True) is False
        assert validator.check(2.0) is True  # Number.isInteger(2.0)
        assert validator.check(2.5) is False


# ---------------------------------------------------------------------------
# Full pipeline (clone -> normalize -> coerce -> check)
# ---------------------------------------------------------------------------


class TestValidateToolArguments:
    def test_coerces_and_returns_arguments(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "s": {"type": "string"}},
                "required": ["n", "s"],
            }
        )
        original = {"n": "42", "s": 7}
        result = validate_tool_arguments(tool, make_call(original))
        assert result == {"n": 42, "s": "7"}
        assert original == {"n": "42", "s": 7}  # input not mutated

    def test_in_place_property_coercion_preserves_args_object_semantics(self):
        # Coercion inside properties mutates args in place; coerced is args.
        tool = make_tool({"type": "object", "properties": {"a": {"type": "integer"}}})
        result = validate_tool_arguments(tool, make_call({"a": "1"}))
        assert result == {"a": 1}

    def test_nested_object_coercion(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {
                    "inner": {"type": "object", "properties": {"n": {"type": "number"}}}
                },
            }
        )
        assert validate_tool_arguments(tool, make_call({"inner": {"n": "2.5"}})) == {
            "inner": {"n": 2.5}
        }

    def test_array_items_coercion(self):
        tool = make_tool(
            {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "integer"}}}}
        )
        assert validate_tool_arguments(tool, make_call({"xs": ["1", 2]})) == {"xs": [1, 2]}

    def test_tuple_items_coercion(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {
                    "pair": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}]}
                },
            }
        )
        assert validate_tool_arguments(tool, make_call({"pair": ["1", 2]})) == {"pair": [1, "2"]}

    def test_allof_folds_coercion(self):
        tool = make_tool(
            {
                "allOf": [
                    {"type": "object", "properties": {"a": {"type": "integer"}}},
                    {"type": "object", "properties": {"b": {"type": "string"}}},
                ]
            }
        )
        assert validate_tool_arguments(tool, make_call({"a": "1", "b": 2})) == {"a": 1, "b": "2"}

    def test_type_list_coercion_uses_first_convertible_member(self):
        # schemaTypes order decides: "string" is tried before "number".
        tool = make_tool(
            {"type": "object", "properties": {"v": {"type": ["string", "number"]}}}
        )
        assert validate_tool_arguments(tool, make_call({"v": True})) == {"v": "true"}

    def test_json_schema_default_never_applied(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"x": {"type": "integer", "default": 7}},
            }
        )
        assert validate_tool_arguments(tool, make_call({})) == {}

    def test_required_null_number_coerced_to_zero(self):
        # Required null is judged by the schema coercion result (not rejected
        # outright, and not blindly zero for every type).
        tool = make_tool(
            {
                "type": "object",
                "properties": {"n": {"type": "number"}},
                "required": ["n"],
            }
        )
        assert validate_tool_arguments(tool, make_call({"n": None})) == {"n": 0}

    def test_required_null_object_type_fails(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"o": {"type": "object"}},
                "required": ["o"],
            }
        )
        try:
            validate_tool_arguments(tool, make_call({"o": None}))
        except ToolArgumentsValidationError as exc:
            assert "must be object" in str(exc)
            assert 'Validation failed for tool "echo"' in str(exc)
            assert '"o": null' in str(exc)  # original arguments echoed
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_optional_null_removed_before_check(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["b"],
            }
        )
        assert validate_tool_arguments(tool, make_call({"a": None, "b": 1})) == {"b": 1}

    def test_nullable_optional_null_survives(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"anyOf": [{"type": "null"}, {"type": "integer"}]}},
            }
        )
        assert validate_tool_arguments(tool, make_call({"a": None})) == {"a": None}

    def test_validation_error_message_shape(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["a", "b"],
            }
        )
        try:
            validate_tool_arguments(tool, make_call({"a": "x"}))
        except ToolArgumentsValidationError as exc:
            text = str(exc)
            # en_US template + requiredProperties + dot path formatting
            assert "- b: must have required properties b" in text
            assert "- a: must be integer" in text
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_nested_path_formatting(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"type": "object", "properties": {"b": {"type": "integer"}}}},
            }
        )
        try:
            validate_tool_arguments(tool, make_call({"a": {"b": "z"}}))
        except ToolArgumentsValidationError as exc:
            assert "- a.b: must be integer" in str(exc)
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_additional_properties_false_rejects_extra_key(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "additionalProperties": False,
            }
        )
        try:
            validate_tool_arguments(tool, make_call({"a": 1, "extra": 2}))
        except ToolArgumentsValidationError as exc:
            assert "must not have additional properties" in str(exc)
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_additional_properties_schema_coerces_extra_values(self):
        tool = make_tool(
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "additionalProperties": {"type": "number"},
            }
        )
        assert validate_tool_arguments(tool, make_call({"a": 1, "extra": "3.5"})) == {
            "a": 1,
            "extra": 3.5,
        }

    def test_string_length_uses_utf16_units(self):
        # "🚀" is 1 code point but 2 UTF-16 code units (JS .length === 2).
        tool = make_tool({"type": "object", "properties": {"s": {"type": "string", "minLength": 2}}})
        assert validate_tool_arguments(tool, make_call({"s": "🚀"})) == {"s": "🚀"}
        try:
            validate_tool_arguments(tool, make_call({"s": "a"}))
        except ToolArgumentsValidationError as exc:
            assert "must not have fewer than 2 characters" in str(exc)
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_enum_rejects_missing_value(self):
        tool = make_tool(
            {"type": "object", "properties": {"op": {"type": "string", "enum": ["add", "sub"]}}}
        )
        assert validate_tool_arguments(tool, make_call({"op": "add"})) == {"op": "add"}
        try:
            validate_tool_arguments(tool, make_call({"op": "mul"}))
        except ToolArgumentsValidationError as exc:
            assert "must be equal to one of the allowed values" in str(exc)
        else:
            raise AssertionError("expected ToolArgumentsValidationError")

    def test_numeric_bounds(self):
        validator = get_validator({"type": "number", "minimum": 1, "maximum": 10})
        assert validator.check(1) is True
        assert validator.check(10) is True
        assert validator.check(0.5) is False
        assert validator.check(10.5) is False
        exclusive = get_validator({"type": "number", "exclusiveMinimum": 1})
        assert exclusive.check(1) is False
        assert exclusive.check(1.5) is True
        assert validator.check(True) is False  # bool is not a number

    def test_pattern(self):
        validator = get_validator({"type": "string", "pattern": "^[a-z]+$"})
        assert validator.check("abc") is True
        assert validator.check("ab1") is False


# ---------------------------------------------------------------------------
# Tool lookup (§4.4: 名称查找失败必须生成明确错误，不猜)
# ---------------------------------------------------------------------------


class TestValidateToolCall:
    def test_finds_tool_by_name(self):
        tool = make_tool({"type": "object", "properties": {}}, name="echo")
        other = make_tool({"type": "object", "properties": {}}, name="other")
        result = validate_tool_call([other, tool], make_call({}, name="echo"))
        assert result == {}

    def test_unknown_tool_raises_exact_message(self):
        try:
            validate_tool_call([], make_call({}, name="nope"))
        except ToolNotFoundError as exc:
            assert str(exc) == 'Tool "nope" not found'
        else:
            raise AssertionError("expected ToolNotFoundError")


# ---------------------------------------------------------------------------
# format_validation_path
# ---------------------------------------------------------------------------


class TestFormatValidationPath:
    def test_root(self):
        from agent_core.ai.tool_validation import SchemaError

        assert format_validation_path(SchemaError("type", "", "m")) == "root"

    def test_nested(self):
        from agent_core.ai.tool_validation import SchemaError

        assert format_validation_path(SchemaError("type", "/a/b", "m")) == "a.b"

    def test_required_appends_first_missing_property(self):
        from agent_core.ai.tool_validation import SchemaError

        error = SchemaError(
            "required", "/a", "m", {"requiredProperties": ["x", "y"]}
        )
        assert format_validation_path(error) == "a.x"

    def test_required_at_root(self):
        from agent_core.ai.tool_validation import SchemaError

        error = SchemaError("required", "", "m", {"requiredProperties": ["x"]})
        assert format_validation_path(error) == "x"
