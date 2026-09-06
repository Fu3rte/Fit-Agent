"""Ported from pi-package/ai/src/utils/json-parse.ts @ pi snapshot 9841914c71a74d81abe07f751aefd271fd924e63

Best-effort JSON parsing for streaming UI progress.

⚠ CONTRACT (plan §4.4): results of ``parse_streaming_json`` /
``partial_parse`` are progress approximations for UI display and frame
encoding only. They are NEVER validated tool arguments and must never be
executed. Only complete, terminal tool calls go through schema validation.

The TS source delegates partial parsing to the npm ``partial-json`` package;
that behavior is re-implemented here with the standard library only:
- unterminated strings/arrays/objects return what was parsed so far,
- incomplete literals that are a strict prefix of true/false/null complete
  to that literal,
- partial numbers keep their longest valid prefix,
- trailing commas and content after the first complete value are ignored.
"""

from __future__ import annotations

import json
from typing import Any

_VALID_JSON_ESCAPES = frozenset('"\\/bfnrt')
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_STRING_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _is_control_character(char: str) -> bool:
    return 0x00 <= ord(char) <= 0x1F


def _escape_control_character(char: str) -> str:
    if char == "\b":
        return "\\b"
    if char == "\f":
        return "\\f"
    if char == "\n":
        return "\\n"
    if char == "\r":
        return "\\r"
    if char == "\t":
        return "\\t"
    return "\\u" + format(ord(char), "04x")


def repair_json(json_text: str) -> str:
    """Repairs malformed JSON string literals by:
    - escaping raw control characters inside strings
    - doubling backslashes before invalid escape characters"""
    repaired: list[str] = []
    in_string = False
    i = 0
    n = len(json_text)

    while i < n:
        char = json_text[i]

        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            i += 1
            continue

        if char == "\\":
            next_char = json_text[i + 1] if i + 1 < n else None
            if next_char is None:
                repaired.append("\\\\")
                i += 1
                continue

            if next_char == "u":
                unicode_digits = json_text[i + 2 : i + 6]
                if len(unicode_digits) == 4 and all(c in _HEX_DIGITS for c in unicode_digits):
                    repaired.append("\\u" + unicode_digits)
                    i += 6
                    continue

            if next_char in _VALID_JSON_ESCAPES:
                repaired.append("\\" + next_char)
                i += 2
                continue

            repaired.append("\\\\")
            i += 1
            continue

        repaired.append(_escape_control_character(char) if _is_control_character(char) else char)
        i += 1

    return "".join(repaired)


def parse_json_with_repair(json_text: str) -> Any:
    """Strict parse; on failure retry once with ``repair_json``."""
    try:
        return json.loads(json_text)
    except ValueError:
        repaired_json = repair_json(json_text)
        if repaired_json != json_text:
            return json.loads(repaired_json)
        raise


# ---------------------------------------------------------------------------
# Partial (incomplete JSON) parser — stdlib replacement for npm partial-json
# ---------------------------------------------------------------------------


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    return i


def _parse_string(text: str, i: int) -> tuple[str, int]:
    """text[i] == '"'. Returns (content, next_index). At end of input the
    content parsed so far is returned (unterminated string)."""
    i += 1
    out: list[str] = []
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            return "".join(out), i + 1
        if char == "\\":
            if i + 1 >= n:
                # Trailing lone backslash: drop it, return partial content.
                return "".join(out), n
            escape = text[i + 1]
            if escape == "u":
                hexpart = text[i + 2 : i + 6]
                if len(hexpart) == 4 and all(c in _HEX_DIGITS for c in hexpart):
                    out.append(chr(int(hexpart, 16)))
                    i += 6
                    continue
                if i + 2 >= n:
                    return "".join(out), n
                if all(c in _HEX_DIGITS for c in hexpart):
                    # Incomplete \u escape at end of input: keep digits literally.
                    out.append("u" + hexpart)
                    return "".join(out), n
                raise ValueError(f"invalid \\u escape at {i}")
            if escape in _STRING_ESCAPES:
                out.append(_STRING_ESCAPES[escape])
                i += 2
                continue
            raise ValueError(f"invalid escape at {i}")
        # Best-effort leniency: raw control characters are kept as-is here;
        # the strict path (json.loads + repair_json) handles them upstream.
        out.append(char)
        i += 1
    return "".join(out), n


def _parse_number(text: str, i: int) -> tuple[int | float, int]:
    start = i
    n = len(text)
    if i < n and text[i] in "+-":
        i += 1
    digits_start = i
    while i < n and text[i].isdigit():
        i += 1
    if i == digits_start:
        if i >= n:
            raise ValueError(f"incomplete number at {start}")
        raise ValueError(f"invalid number at {start}")
    is_float = False
    value_end = i
    if i < n and text[i] == ".":
        j = i + 1
        while j < n and text[j].isdigit():
            j += 1
        if j > i + 1:
            is_float = True
            i = j
            value_end = j
        elif j >= n:
            # Incomplete fraction at end of input: consume the dot so the
            # caller's loop sees end-of-input instead of a stray ".".
            i = j
        # "." followed by junk is not part of the number.
    if i < n and text[i] in "eE":
        j = i + 1
        if j < n and text[j] in "+-":
            j += 1
        exp_digits = j
        while j < n and text[j].isdigit():
            j += 1
        if j > exp_digits:
            is_float = True
            i = j
            value_end = j
        elif j >= n:
            # Incomplete exponent at end of input: consume the marker.
            i = j
        # Invalid exponent is dropped.
    token = text[start:value_end]
    return (float(token) if is_float else int(token)), i


def _parse_literal(text: str, i: int) -> tuple[Any, int]:
    for literal, value in (("true", True), ("false", False), ("null", None)):
        if text.startswith(literal, i):
            return value, i + len(literal)
        if literal.startswith(text[i:]):
            # Rest of input is a strict prefix of the literal: complete it.
            return value, len(text)
    raise ValueError(f"invalid literal at {i}")


def _parse_object(text: str, i: int) -> tuple[dict[str, Any], int]:
    obj: dict[str, Any] = {}
    i += 1  # consume '{'
    n = len(text)
    while True:
        i = _skip_ws(text, i)
        if i >= n:
            return obj, i  # incomplete object
        if text[i] == "}":
            return obj, i + 1
        if text[i] == ",":
            i += 1  # trailing comma / separator before end
            continue
        if text[i] != '"':
            raise ValueError(f"expected object key at {i}")
        key, i = _parse_string(text, i)
        i = _skip_ws(text, i)
        if i >= n:
            return obj, i  # key without colon/value yet: omit the key
        if text[i] != ":":
            raise ValueError(f"expected ':' at {i}")
        i = _skip_ws(text, i + 1)
        if i >= n:
            return obj, i  # key without value yet: omit the key
        value, i = _parse_value(text, i)
        obj[key] = value


def _parse_array(text: str, i: int) -> tuple[list[Any], int]:
    arr: list[Any] = []
    i += 1  # consume '['
    n = len(text)
    while True:
        i = _skip_ws(text, i)
        if i >= n:
            return arr, i  # incomplete array
        if text[i] == "]":
            return arr, i + 1
        if text[i] == ",":
            i += 1
            continue
        value, i = _parse_value(text, i)
        arr.append(value)


def _parse_value(text: str, i: int) -> tuple[Any, int]:
    char = text[i]
    if char == "{":
        return _parse_object(text, i)
    if char == "[":
        return _parse_array(text, i)
    if char == '"':
        return _parse_string(text, i)
    if char in "tfn":
        return _parse_literal(text, i)
    if char == "-" or char.isdigit():
        return _parse_number(text, i)
    raise ValueError(f"unexpected character {char!r} at {i}")


def partial_parse(text: str) -> Any:
    """Parse potentially incomplete JSON, returning the first value found.
    Raises ValueError when nothing parseable exists (caller decides fallback)."""
    i = _skip_ws(text, 0)
    if i >= len(text):
        raise ValueError("empty input")
    value, _ = _parse_value(text, i)
    return value


def parse_streaming_json(partial_json: str | None) -> Any:
    """Attempts to parse potentially incomplete JSON during streaming.
    Always returns a valid value, even if the JSON is incomplete.

    ⚠ UI/frame progress only — never execute the result (see module docstring).
    """
    if not partial_json or partial_json.strip() == "":
        return {}

    try:
        return parse_json_with_repair(partial_json)
    except (ValueError, RecursionError):
        try:
            result = partial_parse(partial_json)
            return {} if result is None else result
        except (ValueError, RecursionError, IndexError):
            try:
                result = partial_parse(repair_json(partial_json))
                return {} if result is None else result
            except (ValueError, RecursionError, IndexError):
                return {}
