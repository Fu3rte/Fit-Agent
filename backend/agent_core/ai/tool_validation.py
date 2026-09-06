"""Tool call argument validation ported from pi.

Ported from pi-package/ai/src/utils/validation.ts @ pi snapshot
9841914c71a74d81abe07f751aefd271fd924e63 (typebox 1.1.38 semantics verified
against the local node_modules build).

Pipeline contract (plan §4.4): clone -> normalize optional nulls -> convert ->
coerce -> check. In TS there are two regimes:

1. TypeBox-kind schemas: ``Value.Convert`` performs conversion, and pi's JSON
   Schema coercion (``coerceWithJsonSchema``) is skipped (TYPEBOX_KIND check).
2. Plain JSON Schema dicts: ``Value.Convert`` is a no-op (verified: typebox
   ``FromType`` falls through every ``Type.Is*`` check and returns the value
   unchanged), so pi's JSON Schema coercion + ``Compile``-based ``Check`` runs.

Python tools register plain JSON Schema dicts (see ``Tool.parameters``), so
this port always follows regime 2; the ``Value.Convert`` step is therefore a
documented no-op here. All behavior contracts come from ``coerceWithJsonSchema``
and its helpers, which are ported line-by-line.

Checker subset (mirrors the measured typebox ``Compile`` behavior for plain
JSON Schema): type (string|list), const, enum, allOf, anyOf, oneOf, required,
properties, additionalProperties, items (schema|tuple), numeric bounds,
multipleOf, minLength/maxLength (UTF-16 code units, matching JS ``.length``),
pattern, minItems/maxItems, uniqueItems, minProperties/maxProperties.
Unresolvable ``$ref`` compiles (measured) to an always-false check with
keyword ``boolean`` / message ``schema is false``; ported as such.
JSON Schema ``default`` is never applied (plan §11.2).

Error objects carry ``keyword`` / ``instance_path`` (JS ``instancePath``,
"/"-joined) / ``message`` (typebox en_US templates) / ``params`` so that
``format_validation_path`` reproduces the TS formatting exactly.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ToolValidationError",
    "ToolNotFoundError",
    "ToolArgumentsValidationError",
    "SchemaError",
    "JsonSchemaValidator",
    "get_validator",
    "get_schema_types",
    "matches_json_type",
    "coerce_primitive_by_type",
    "coerce_with_json_schema",
    "coerce_with_union_schema",
    "normalize_optional_nulls",
    "format_validation_path",
    "validate_tool_call",
    "validate_tool_arguments",
]


class ToolValidationError(Exception):
    """Raised when a tool call cannot be resolved or validated."""


class ToolNotFoundError(ToolValidationError):
    """Tool name lookup failed; callers must surface an error tool result,
    never guess a tool (plan §4.4)."""


class ToolArgumentsValidationError(ToolValidationError):
    """Arguments failed schema validation after the full coercion pipeline."""


@dataclass
class SchemaError:
    """TypeBox-shaped validation error (keyword/instancePath/params/message)."""

    keyword: str
    instance_path: str
    message: str
    params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema type helpers (port of getSchemaTypes / matchesJsonType)
# ---------------------------------------------------------------------------


def get_schema_types(schema: dict[str, Any]) -> list[str]:
    """Port of ``getSchemaTypes``: string form, filtered list form, or []."""
    if not isinstance(schema, dict):
        return []
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [entry for entry in declared if isinstance(entry, str)]
    return []


def _is_json_number(value: Any) -> bool:
    # TS ``typeof value === "number"``: booleans are not numbers in Python either.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_json_integer(value: Any) -> bool:
    # TS ``Number.isInteger``: 2.0 counts, 1.5 does not, true does not.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return True
    return value.is_integer()


def matches_json_type(value: Any, type_name: str) -> bool:
    """Port of ``matchesJsonType`` (object rejects arrays, like IsObjectNotArray)."""
    if type_name == "number":
        return _is_json_number(value)
    if type_name == "integer":
        return _is_json_integer(value)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "null":
        return value is None
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


# ---------------------------------------------------------------------------
# TS Number()/String() semantics
# ---------------------------------------------------------------------------


def _ts_number(text: str) -> int | float | None:
    """Approximate JS ``Number(string)``: trims whitespace, supports hex,
    rejects NaN/Infinity (callers apply isFinite/isInteger themselves)."""
    stripped = text.strip()
    if not stripped or "_" in stripped:
        return None
    body = stripped[1:] if stripped[0] in "+-" else stripped
    if body[:2].lower() == "0x":
        try:
            return int(stripped, 16)
        except ValueError:
            return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _ts_number_to_string(value: int | float) -> str:
    """Approximate JS ``String(number)`` for already-numeric values."""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        # Python pads exponents to two digits ("1e-07"); JS does not ("1e-7").
        text = repr(value)
        return re.sub(r"e([+-])0*(\d)", r"e\1\2", text)
    return str(value)


# ---------------------------------------------------------------------------
# Coercion (port of coercePrimitiveByType and friends)
# ---------------------------------------------------------------------------


def coerce_primitive_by_type(value: Any, type_name: str) -> Any:
    """Port of ``coercePrimitiveByType``; returns value unchanged when no
    conversion applies (identity is significant for the callers)."""
    if type_name == "number":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            parsed = _ts_number(value)
            if parsed is not None and math.isfinite(parsed):
                return parsed
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if type_name == "integer":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            parsed = _ts_number(value)
            if parsed is not None and math.isfinite(parsed) and float(parsed).is_integer():
                return int(parsed)
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if type_name == "boolean":
        if value is None:
            return False
        if isinstance(value, str):
            if value == "true":
                return True
            if value == "false":
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value == 1:
                return True
            if value == 0:
                return False
        return value
    if type_name == "string":
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return _ts_number_to_string(value)
        return value
    if type_name == "null":
        # TS: value === "" || value === 0 || value === false -> null
        if isinstance(value, str) and value == "":
            return None
        if isinstance(value, bool):
            return None if value is False else value
        if _is_json_number(value) and value == 0:
            return None
        return value
    return value


def _strict_not_equal(candidate: Any, value: Any) -> bool:
    """Port of the TS ``candidate !== nextValue`` identity/value comparison."""
    if candidate is value:
        return False
    if isinstance(candidate, bool) != isinstance(value, bool):
        return True
    return not (candidate == value)  # NaN != NaN -> changed, like TS


def apply_schema_object_coercion(value: dict[str, Any], schema: dict[str, Any]) -> None:
    """Port of ``applySchemaObjectCoercion`` (mutates ``value`` in place)."""
    properties = schema.get("properties")
    defined_keys = set(properties.keys()) if isinstance(properties, dict) else set()

    if isinstance(properties, dict):
        for key, property_schema in properties.items():
            if key not in value:
                continue
            value[key] = coerce_with_json_schema(value[key], property_schema)

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        for key in list(value.keys()):
            if key in defined_keys:
                continue
            value[key] = coerce_with_json_schema(value[key], additional)


def apply_schema_array_coercion(value: list[Any], schema: dict[str, Any]) -> None:
    """Port of ``applySchemaArrayCoercion`` (mutates ``value`` in place)."""
    items = schema.get("items")
    if isinstance(items, list):
        for index in range(len(value)):
            item_schema = items[index] if index < len(items) else None
            if not item_schema:
                continue
            value[index] = coerce_with_json_schema(value[index], item_schema)
        return
    if isinstance(items, dict):
        for index in range(len(value)):
            value[index] = coerce_with_json_schema(value[index], items)


def coerce_with_union_schema(value: Any, schemas: list[Any]) -> Any:
    """Port of ``coerceWithUnionSchema``: member order is significant. First
    pass accepts an as-is match; second pass tries clone+coerce+check per
    member (each candidate cloned from the original value) and falls back."""
    for schema in schemas:
        validator = get_sub_schema_validator(schema)
        if validator is not None and validator.check(value):
            return value

    for schema in schemas:
        candidate = copy.deepcopy(value)
        coerced = coerce_with_json_schema(candidate, schema)
        validator = get_sub_schema_validator(schema)
        if validator is not None and validator.check(coerced):
            return coerced
    return value


def coerce_with_json_schema(value: Any, schema: Any) -> Any:
    """Port of ``coerceWithJsonSchema``; object/array coercion mutates in
    place, so the returned value may be the same object as the input."""
    if not isinstance(schema, dict):
        return value

    next_value = value

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for nested in all_of:
            next_value = coerce_with_json_schema(next_value, nested)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        next_value = coerce_with_union_schema(next_value, any_of)

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        next_value = coerce_with_union_schema(next_value, one_of)

    schema_types = get_schema_types(schema)
    matches_union_member = len(schema_types) > 1 and any(
        matches_json_type(next_value, declared) for declared in schema_types
    )
    if len(schema_types) > 0 and not matches_union_member:
        for declared in schema_types:
            candidate = coerce_primitive_by_type(next_value, declared)
            if _strict_not_equal(candidate, next_value):
                next_value = candidate
                break

    if "object" in schema_types and isinstance(next_value, dict):
        apply_schema_object_coercion(next_value, schema)

    if "array" in schema_types and isinstance(next_value, list):
        apply_schema_array_coercion(next_value, schema)

    return next_value


def normalize_optional_nulls(value: Any, schema: Any) -> None:
    """Port of ``normalizeOptionalNulls`` (mutates ``value`` in place).

    Optional (non-required) keys whose value is ``null`` are deleted unless the
    subschema accepts null (anyOf/oneOf [.., null], type lists containing
    "null") or the subschema is referenced via ``$ref`` (TS explicitly skips
    deletion for ``$ref`` schemas because the target cannot be resolved here).
    """
    if not isinstance(schema, dict):
        return
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, list):
            for index in range(len(value)):
                item_schema = items[index] if index < len(items) else None
                if item_schema is not None:
                    normalize_optional_nulls(value[index], item_schema)
        elif items is not None:
            for item in value:
                normalize_optional_nulls(item, items)
        return
    if not isinstance(value, dict) or not schema.get("properties"):
        return

    required = set(schema.get("required") or [])
    for key, property_schema in schema["properties"].items():
        if key not in value:
            continue
        if (
            value[key] is None
            and key not in required
            and not isinstance(property_schema.get("$ref"), str)
        ):
            validator = get_sub_schema_validator(property_schema)
            if validator is not None and validator.check(None) is False:
                del value[key]
                continue
        normalize_optional_nulls(value[key], property_schema)


# ---------------------------------------------------------------------------
# JSON Schema validator (subset mirroring typebox Compile for plain schemas)
# ---------------------------------------------------------------------------


def _deep_equal(a: Any, b: Any) -> bool:
    """JSON-value equality; bools and numbers are distinct like JS ===."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, dict):
        return isinstance(b, dict) and set(a.keys()) == set(b.keys()) and all(
            _deep_equal(a[key], b[key]) for key in a
        )
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(
            _deep_equal(x, y) for x, y in zip(a, b)
        )
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _utf16_length(text: str) -> int:
    """JS ``string.length`` counts UTF-16 code units, not code points."""
    return len(text.encode("utf-16-le")) // 2


class JsonSchemaValidator:
    """Subset JSON Schema checker mirroring typebox ``Compile`` output for
    plain JSON Schema dicts. ``check``/``errors`` mirror Validator.Check/.Errors."""

    def __init__(self, schema: Any) -> None:
        if not isinstance(schema, (bool, dict)):
            raise ValueError("unsupported schema: expected a JSON Schema object")
        self.schema = schema

    def check(self, value: Any) -> bool:
        return not self.errors(value)

    def errors(self, value: Any) -> list[SchemaError]:
        return list(self._validate(value, self.schema, ""))

    def _ok(self, schema: Any, value: Any) -> bool:
        return not self._validate(value, schema, "")

    def _validate(self, value: Any, schema: Any, path: str) -> list[SchemaError]:
        if schema is True:
            return []
        if schema is False:
            return [SchemaError("boolean", path, "schema is false")]
        if not isinstance(schema, dict):
            return []
        if "$ref" in schema:
            # Measured typebox behavior: unresolvable $ref compiles to a
            # constant-false check (keyword "boolean", "schema is false").
            return [SchemaError("boolean", path, "schema is false")]

        errors: list[SchemaError] = []

        declared_types = get_schema_types(schema)
        if declared_types and not any(matches_json_type(value, t) for t in declared_types):
            if len(declared_types) == 1:
                message = f"must be {declared_types[0]}"
                params: dict[str, Any] = {"type": declared_types[0]}
            else:
                message = "must be either " + " or ".join(declared_types)
                params = {"type": declared_types}
            errors.append(SchemaError("type", path, message, params))

        if "const" in schema and not _deep_equal(value, schema["const"]):
            errors.append(SchemaError("const", path, "must be equal to constant"))

        if "enum" in schema:
            allowed = schema["enum"]
            if not isinstance(allowed, list) or not any(_deep_equal(value, entry) for entry in allowed):
                errors.append(SchemaError("enum", path, "must be equal to one of the allowed values"))

        for sub_schema in schema.get("allOf") or []:
            errors.extend(self._validate(value, sub_schema, path))

        any_of = schema.get("anyOf")
        if isinstance(any_of, list) and not any(self._ok(sub, value) for sub in any_of):
            errors.append(SchemaError("anyOf", path, "must match a schema in anyOf"))

        one_of = schema.get("oneOf")
        if isinstance(one_of, list):
            matched = sum(1 for sub in one_of if self._ok(sub, value))
            if matched != 1:
                errors.append(
                    SchemaError("oneOf", path, "must match exactly one schema in oneOf")
                )

        if isinstance(value, dict):
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            required = [key for key in required if isinstance(key, str)] if isinstance(required, list) else []
            missing = [key for key in required if key not in value]
            if missing:
                errors.append(
                    SchemaError(
                        "required",
                        path,
                        "must have required properties " + ", ".join(missing),
                        {"requiredProperties": missing},
                    )
                )
            for key, sub_schema in properties.items():
                if key in value:
                    errors.extend(self._validate(value[key], sub_schema, f"{path}/{key}"))
            additional = schema.get("additionalProperties")
            defined_keys = set(properties.keys())
            if additional is False:
                for key in value:
                    if key not in defined_keys:
                        errors.append(
                            SchemaError(
                                "additionalProperties",
                                f"{path}/{key}",
                                "must not have additional properties",
                            )
                        )
            elif isinstance(additional, dict):
                for key in value:
                    if key not in defined_keys:
                        errors.extend(self._validate(value[key], additional, f"{path}/{key}"))
            min_properties = schema.get("minProperties")
            if isinstance(min_properties, (int, float)) and len(value) < min_properties:
                errors.append(
                    SchemaError("minProperties", path, f"must not have fewer than {min_properties} properties")
                )
            max_properties = schema.get("maxProperties")
            if isinstance(max_properties, (int, float)) and len(value) > max_properties:
                errors.append(
                    SchemaError("maxProperties", path, f"must not have more than {max_properties} properties")
                )

        if isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, list):
                for index, sub_schema in enumerate(items):
                    if index < len(value):
                        errors.extend(self._validate(value[index], sub_schema, f"{path}/{index}"))
            elif isinstance(items, dict):
                for index, item in enumerate(value):
                    errors.extend(self._validate(item, items, f"{path}/{index}"))
            min_items = schema.get("minItems")
            if isinstance(min_items, (int, float)) and len(value) < min_items:
                errors.append(
                    SchemaError("minItems", path, f"must not have fewer than {min_items} items")
                )
            max_items = schema.get("maxItems")
            if isinstance(max_items, (int, float)) and len(value) > max_items:
                errors.append(
                    SchemaError("maxItems", path, f"must not have more than {max_items} items")
                )
            if schema.get("uniqueItems") is True:
                for i in range(len(value)):
                    for j in range(i + 1, len(value)):
                        if _deep_equal(value[i], value[j]):
                            errors.append(SchemaError("uniqueItems", path, "must not have duplicate items"))
                            break
                    else:
                        continue
                    break

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
                errors.append(SchemaError("minimum", path, f"must be >= {minimum}", {"comparison": ">=", "limit": minimum}))
            maximum = schema.get("maximum")
            if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
                errors.append(SchemaError("maximum", path, f"must be <= {maximum}", {"comparison": "<=", "limit": maximum}))
            exclusive_minimum = schema.get("exclusiveMinimum")
            if (
                isinstance(exclusive_minimum, (int, float))
                and not isinstance(exclusive_minimum, bool)
                and value <= exclusive_minimum
            ):
                errors.append(
                    SchemaError(
                        "exclusiveMinimum",
                        path,
                        f"must be > {exclusive_minimum}",
                        {"comparison": ">", "limit": exclusive_minimum},
                    )
                )
            exclusive_maximum = schema.get("exclusiveMaximum")
            if (
                isinstance(exclusive_maximum, (int, float))
                and not isinstance(exclusive_maximum, bool)
                and value >= exclusive_maximum
            ):
                errors.append(
                    SchemaError(
                        "exclusiveMaximum",
                        path,
                        f"must be < {exclusive_maximum}",
                        {"comparison": "<", "limit": exclusive_maximum},
                    )
                )
            multiple_of = schema.get("multipleOf")
            if isinstance(multiple_of, (int, float)) and not isinstance(multiple_of, bool) and multiple_of != 0:
                if not _is_json_integer(value / multiple_of):
                    errors.append(
                        SchemaError("multipleOf", path, f"must be multiple of {multiple_of}", {"multipleOf": multiple_of})
                    )

        if isinstance(value, str):
            min_length = schema.get("minLength")
            if isinstance(min_length, (int, float)) and _utf16_length(value) < min_length:
                errors.append(
                    SchemaError("minLength", path, f"must not have fewer than {min_length} characters")
                )
            max_length = schema.get("maxLength")
            if isinstance(max_length, (int, float)) and _utf16_length(value) > max_length:
                errors.append(
                    SchemaError("maxLength", path, f"must not have more than {max_length} characters")
                )
            pattern = schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    if re.search(pattern, value) is None:
                        errors.append(
                            SchemaError("pattern", path, f'must match pattern "{pattern}"', {"pattern": pattern})
                        )
                except re.error:
                    # JS/Python regex dialect mismatch: fail open instead of
                    # rejecting valid arguments on unsupported constructs.
                    pass

        return errors


_VALIDATOR_CACHE: dict[int, JsonSchemaValidator] = {}


def get_validator(schema: Any) -> JsonSchemaValidator:
    """Port of ``getValidator``. The TS WeakMap cache is keyed by schema
    object identity; ``dict`` is not weakref-able in Python, so schemas are
    compiled per call (validation is cheap relative to provider I/O)."""
    return JsonSchemaValidator(schema)


def get_sub_schema_validator(schema: Any) -> JsonSchemaValidator | None:
    """Port of ``getSubSchemaValidator``: compile errors yield None."""
    try:
        return get_validator(schema)
    except ValueError:
        return None


def format_validation_path(error: SchemaError) -> str:
    """Port of ``formatValidationPath`` (identical path formatting)."""

    def to_dots(instance_path: str) -> str:
        stripped = instance_path[1:] if instance_path.startswith("/") else instance_path
        return stripped.replace("/", ".")

    if error.keyword == "required":
        required_properties = error.params.get("requiredProperties")
        required_property = required_properties[0] if required_properties else None
        if required_property is not None:
            base_path = to_dots(error.instance_path)
            return f"{base_path}.{required_property}" if base_path else required_property
    return to_dots(error.instance_path) or "root"


# ---------------------------------------------------------------------------
# Public entry points (port of validateToolCall / validateToolArguments)
# ---------------------------------------------------------------------------


def validate_tool_call(tools: list[Any], tool_call: Any) -> Any:
    """Finds a tool by name and validates the tool call arguments against its
    JSON Schema. Raises ``ToolNotFoundError`` when the tool is missing (never
    guesses); otherwise delegates to ``validate_tool_arguments``."""
    tool = next((t for t in tools if t.name == tool_call.name), None)
    if tool is None:
        raise ToolNotFoundError(f'Tool "{tool_call.name}" not found')
    return validate_tool_arguments(tool, tool_call)


def validate_tool_arguments(tool: Any, tool_call: Any) -> Any:
    """Validates tool call arguments against the tool's JSON Schema.

    Order mirrors the TS source: clone -> normalizeOptionalNulls ->
    (Value.Convert: no-op for plain JSON Schema, see module docstring) ->
    coerceWithJsonSchema -> Check. Raises ``ToolArgumentsValidationError``
    with the TS error message shape when validation fails.
    """
    parameters = tool.parameters
    validator = get_validator(parameters)

    args = copy.deepcopy(tool_call.arguments)
    normalize_optional_nulls(args, parameters)
    # Value.Convert(parameters, args): for plain JSON Schema dicts typebox's
    # FromType returns the value unchanged (verified against typebox 1.1.38),
    # so the conversion layer is intentionally a no-op here.

    coerced = coerce_with_json_schema(args, parameters)
    if coerced is not args:
        if isinstance(args, dict) and isinstance(coerced, (dict, list)):
            # TS: delete all keys of args, then Object.assign(args, coerced).
            # Object.assign with an array source copies index keys as strings.
            args.clear()
            if isinstance(coerced, list):
                for index, item in enumerate(coerced):
                    args[str(index)] = item
            else:
                args.update(coerced)
        else:
            return coerced if validator.check(coerced) else args

    if validator.check(args):
        return args

    error_lines = "\n".join(
        f"  - {format_validation_path(error)}: {error.message}" for error in validator.errors(args)
    )
    errors_text = error_lines or "Unknown validation error"
    message = (
        f'Validation failed for tool "{tool_call.name}":\n'
        f"{errors_text}\n"
        f"\n"
        f"Received arguments:\n"
        f"{json.dumps(tool_call.arguments, indent=2, ensure_ascii=False)}"
    )
    raise ToolArgumentsValidationError(message)
