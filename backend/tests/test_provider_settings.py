# provider.json 五字段读写与 resolve 合并：api／structured_output 缺省回落 openai_compatible／json_schema，
# 组合非法在写入与 resolve 边界就地崩溃，旧 structured_protocol 只做一次确定性映射。

import json
from pathlib import Path

import pytest

from provider_settings import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_COMPATIBLE,
    DEFAULT_API,
    DEFAULT_STRUCTURED_OUTPUT,
    LEGACY_STRUCTURED_PROTOCOLS,
    STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    STRUCTURED_OUTPUT_JSON_SCHEMA,
    ModelConfigurationError,
    ProviderConfig,
    read_provider_config,
    resolve_provider_config,
    validated_protocol_pair,
    write_provider_config,
)

_LEGAL_PAIRS = (
    (API_OPENAI_COMPATIBLE, STRUCTURED_OUTPUT_JSON_SCHEMA),
    (API_OPENAI_COMPATIBLE, STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT),
    (API_ANTHROPIC_MESSAGES, STRUCTURED_OUTPUT_JSON_SCHEMA),
)


def test_write_read_roundtrip_keeps_five_fields(tmp_path: Path) -> None:
    """五字段写入后原样读回；api 与 structured_output 都不丢失。"""
    config = ProviderConfig(
        api_key="k",
        base_url="https://example.test/v1",
        model="m",
        api=API_ANTHROPIC_MESSAGES,
        structured_output=STRUCTURED_OUTPUT_JSON_SCHEMA,
    )

    write_provider_config(tmp_path, config)

    assert read_provider_config(tmp_path) == config
    stored = json.loads((tmp_path / "provider.json").read_text(encoding="utf-8"))
    assert set(stored) == {"api_key", "base_url", "model", "api", "structured_output"}


def test_read_defaults_protocol_pair(tmp_path: Path) -> None:
    """缺省（无文件）与两个字段都缺省的已存文件都回落 openai_compatible／json_schema。"""
    assert read_provider_config(tmp_path) == ProviderConfig()

    (tmp_path / "provider.json").write_text(
        '{"api_key": "k", "base_url": "u", "model": "m"}', encoding="utf-8"
    )
    config = read_provider_config(tmp_path)
    assert (config.api, config.structured_output) == (
        DEFAULT_API,
        DEFAULT_STRUCTURED_OUTPUT,
    )


@pytest.mark.parametrize("legacy, expected", sorted(LEGACY_STRUCTURED_PROTOCOLS.items()))
def test_read_migrates_legacy_structured_protocol(
    tmp_path: Path, legacy: str, expected: tuple[str, str]
) -> None:
    """旧 structured_protocol 三值各映射到唯一的 (api, structured_output)。"""
    (tmp_path / "provider.json").write_text(
        json.dumps(
            {
                "api_key": "k",
                "base_url": "https://example.test/v1",
                "model": "m",
                "structured_protocol": legacy,
            }
        ),
        encoding="utf-8",
    )

    config = read_provider_config(tmp_path)

    assert (config.api, config.structured_output) == expected


def test_read_rejects_unknown_legacy_protocol(tmp_path: Path) -> None:
    """旧字段值不在映射表内即就地拒绝，不猜 transport。"""
    (tmp_path / "provider.json").write_text(
        '{"structured_protocol": "not_a_protocol"}', encoding="utf-8"
    )

    with pytest.raises(ModelConfigurationError):
        read_provider_config(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        {"api": API_OPENAI_COMPATIBLE},
        {"structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA},
        {"api": "", "structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA},
        {"api": API_OPENAI_COMPATIBLE, "structured_output": ""},
        {"api": "bogus_api", "structured_output": STRUCTURED_OUTPUT_JSON_SCHEMA},
        {"api": API_OPENAI_COMPATIBLE, "structured_output": "bogus_output"},
        {
            "api": API_ANTHROPIC_MESSAGES,
            "structured_output": STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
        },
    ],
)
def test_read_rejects_incomplete_or_illegal_new_fields(
    tmp_path: Path, raw: dict[str, str]
) -> None:
    """新字段存在时必须完整合法：缺一、空串、未知值、非法组合一律就地拒绝，不静默回落。"""
    (tmp_path / "provider.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ModelConfigurationError):
        read_provider_config(tmp_path)


def test_write_rejects_illegal_combination(tmp_path: Path) -> None:
    """非法组合拒绝落盘：磁盘上不留需要靠 resolve 才暴露的坏配置。"""
    with pytest.raises(ModelConfigurationError):
        write_provider_config(
            tmp_path,
            ProviderConfig(
                api=API_ANTHROPIC_MESSAGES,
                structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            ),
        )

    assert not (tmp_path / "provider.json").exists()


@pytest.mark.parametrize("api, structured_output", _LEGAL_PAIRS)
def test_validated_protocol_pair_accepts_legal_combinations(
    api: str, structured_output: str
) -> None:
    """三个合法组合原样通过校验。"""
    assert validated_protocol_pair(api, structured_output) == (api, structured_output)


def test_resolve_returns_file_credentials_and_protocol_pair(tmp_path: Path) -> None:
    """凭据与协议都只取 provider.json。"""
    write_provider_config(
        tmp_path,
        ProviderConfig(
            api_key="k",
            base_url="https://file.test/v1",
            model="m",
            api=API_OPENAI_COMPATIBLE,
            structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
        ),
    )

    assert resolve_provider_config(tmp_path) == ProviderConfig(
        api_key="k",
        base_url="https://file.test/v1",
        model="m",
        api=API_OPENAI_COMPATIBLE,
        structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
    )


def test_resolve_override_merges_nonempty_fields(tmp_path: Path) -> None:
    """override 的非空字段覆盖 provider.json，空字段回落文件值。"""
    write_provider_config(
        tmp_path,
        ProviderConfig(
            api_key="file-key",
            base_url="https://file.test/v1",
            model="file-model",
            api=API_ANTHROPIC_MESSAGES,
            structured_output=STRUCTURED_OUTPUT_JSON_SCHEMA,
        ),
    )

    config = resolve_provider_config(
        tmp_path,
        ProviderConfig(
            api_key="",
            base_url="",
            model="",
            api=API_OPENAI_COMPATIBLE,
            structured_output="",
        ),
    )

    assert config == ProviderConfig(
        api_key="file-key",
        base_url="https://file.test/v1",
        model="file-model",
        api=API_OPENAI_COMPATIBLE,
        structured_output=STRUCTURED_OUTPUT_JSON_SCHEMA,
    )


def test_resolve_override_illegal_combination_fails(tmp_path: Path) -> None:
    """override 拼出的非法组合在 resolve 第二道校验处失败。"""
    write_provider_config(
        tmp_path,
        ProviderConfig(api_key="k", base_url="u", model="m"),
    )

    with pytest.raises(ModelConfigurationError):
        resolve_provider_config(
            tmp_path,
            ProviderConfig(
                api=API_ANTHROPIC_MESSAGES,
                structured_output=STRUCTURED_OUTPUT_FUNCTION_CALLING_STRICT,
            ),
        )


def test_resolve_fails_when_credentials_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider.json 无凭据即就地抛 ModelConfigurationError。"""
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    monkeypatch.setenv("MODEL_BASE_URL", "https://env.test/v1")
    monkeypatch.setenv("MODEL_MODEL", "env-model")

    with pytest.raises(ModelConfigurationError):
        resolve_provider_config(tmp_path)


def test_resolve_fails_on_unknown_protocol(tmp_path: Path) -> None:
    """非法 api／structured_output 在 resolve 就地抛 ModelConfigurationError。"""
    write_provider_config(
        tmp_path,
        ProviderConfig(api_key="k", base_url="u", model="m"),
    )
    raw = json.loads((tmp_path / "provider.json").read_text(encoding="utf-8"))
    raw["api"] = "not_an_api"
    (tmp_path / "provider.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ModelConfigurationError):
        resolve_provider_config(tmp_path)
