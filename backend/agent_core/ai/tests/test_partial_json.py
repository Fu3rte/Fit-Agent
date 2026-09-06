"""Tests for agent_core.ai.partial_json (TS json-parse.ts behavior).

No pytest dependency (house style): plain asserts + a tiny raises helper.
"""


def _raises_value_error(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


from agent_core.ai.partial_json import (
    parse_json_with_repair,
    parse_streaming_json,
    partial_parse,
    repair_json,
)


class TestRepairJson:
    def test_valid_json_unchanged(self):
        assert repair_json('{"a": 1}') == '{"a": 1}'

    def test_raw_control_character_escaped(self):
        assert repair_json('"a\nb"') == '"a\\nb"'
        assert repair_json('"a\tb"') == '"a\\tb"'

    def test_other_control_characters_become_unicode_escape(self):
        assert repair_json('"a\x01b"') == '"a\\u0001b"'

    def test_invalid_escape_doubles_backslash(self):
        # \x is not a valid JSON escape -> the backslash is doubled.
        assert repair_json('"a\\xb"') == '"a\\\\xb"'

    def test_trailing_backslash_doubled(self):
        assert repair_json('"a\\') == '"a\\\\'

    def test_valid_escapes_preserved(self):
        assert repair_json('"a\\nb\\tc\\\\d\\u0041"') == '"a\\nb\\tc\\\\d\\u0041"'

    def test_incomplete_unicode_escape_doubles_backslash(self):
        assert repair_json('"a\\u12"') == '"a\\\\u12"'

    def test_control_char_outside_string_untouched(self):
        assert repair_json("{\n \"a\": 1\n}") == "{\n \"a\": 1\n}"


class TestParseJsonWithRepair:
    def test_valid_json_direct(self):
        assert parse_json_with_repair('{"a": [1, 2]}') == {"a": [1, 2]}

    def test_repairs_control_characters(self):
        assert parse_json_with_repair('{"a": "line1\nline2"}') == {"a": "line1\nline2"}

    def test_repairs_invalid_escapes(self):
        # \x and \q are invalid JSON escapes; repair doubles the backslashes
        # so the parsed value keeps them as literal backslash characters.
        assert parse_json_with_repair('{"path": "C:\\xyz\\q2"}') == {"path": "C:\\xyz\\q2"}

    def test_unchanged_invalid_json_raises(self):
        _raises_value_error(parse_json_with_repair, "{not json")


class TestPartialParse:
    def test_complete_object(self):
        assert partial_parse('{"a": 1}') == {"a": 1}

    def test_incomplete_string_value(self):
        assert partial_parse('{"a": 1, "b": "two') == {"a": 1, "b": "two"}

    def test_incomplete_array(self):
        assert partial_parse('[1, 2, 3, "fo') == [1, 2, 3, "fo"]

    def test_nested_incomplete(self):
        assert partial_parse('{"a": {"b": [1, "x') == {"a": {"b": [1, "x"]}}

    def test_incomplete_literal_completes(self):
        assert partial_parse('{"a": tru') == {"a": True}
        assert partial_parse('{"a": fals') == {"a": False}
        assert partial_parse('{"a": nul') == {"a": None}

    def test_partial_number_keeps_valid_prefix(self):
        assert partial_parse('{"n": 12.') == {"n": 12}
        assert partial_parse('{"n": 3.14') == {"n": 3.14}
        assert partial_parse('{"n": 5e') == {"n": 5}
        assert partial_parse('{"n": 5e+') == {"n": 5}

    def test_key_without_value_omitted(self):
        assert partial_parse('{"a"') == {}
        assert partial_parse('{"a":') == {}

    def test_trailing_comma_tolerated(self):
        assert partial_parse('[1, 2,') == [1, 2]
        assert partial_parse('{"a": 1,') == {"a": 1}

    def test_incomplete_key_string(self):
        assert partial_parse('{"ab') == {}

    def test_content_after_first_value_ignored(self):
        assert partial_parse('{"a": 1} trailing') == {"a": 1}

    def test_top_level_scalar_prefixes(self):
        assert partial_parse("tru") is True
        assert partial_parse("42") == 42
        assert partial_parse('"unterminated') == "unterminated"

    def test_garbage_raises(self):
        _raises_value_error(partial_parse, "{zzz")
        _raises_value_error(partial_parse, "")

    def test_unicode_escape(self):
        assert partial_parse('{"a": "\\u4e2d\\u6587') == {"a": "中文"}


class TestParseStreamingJson:
    def test_empty_and_none_return_empty_object(self):
        assert parse_streaming_json(None) == {}
        assert parse_streaming_json("") == {}
        assert parse_streaming_json("   ") == {}

    def test_valid_json_passthrough(self):
        assert parse_streaming_json('{"a": {"b": 2}}') == {"a": {"b": 2}}

    def test_repairable_json_repaired(self):
        assert parse_streaming_json('{"text": "multi\nline"}') == {"text": "multi\nline"}

    def test_partial_json_best_effort(self):
        assert parse_streaming_json('{"a": 1, "b": "tw') == {"a": 1, "b": "tw"}

    def test_null_result_becomes_empty_object(self):
        # "nul" completes to null; TS `result ?? {}` yields {}.
        assert parse_streaming_json("nul") == {}

    def test_total_garbage_returns_empty_object(self):
        assert parse_streaming_json("zzzz") == {}
        assert parse_streaming_json("{zzz") == {}

    def test_toolcall_arguments_partial_stream(self):
        chunks = ['{"comma', 'nd": "ls -', 'la", "dir": "/tm']
        accumulated = ""
        for chunk in chunks:
            accumulated += chunk
            parsed = parse_streaming_json(accumulated)
            assert isinstance(parsed, dict)
        assert parse_streaming_json(accumulated) == {"command": "ls -la", "dir": "/tm"}

    def test_never_returns_none(self):
        for sample in ["{", "[", '"', "tru", "-", '{"a": ']:
            assert parse_streaming_json(sample) is not None
