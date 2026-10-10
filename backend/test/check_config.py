import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest
from pydantic import ValidationError

from app import model_config
from app.model_config import (
    ModelConfigError,
    ModelNotConfigured,
    clear_provider,
    effective_provider,
    load_model_config,
    load_provider_status,
    save_provider,
    saved_api_key,
)

EMPTY_STATUS = {
    "api": None,
    "base_url": None,
    "model": None,
    "provider": None,
    "api_key": None,
}


def check_effective_provider() -> None:
    assert effective_provider("openai-completions", None) == "openai-completions"
    assert effective_provider("anthropic-messages", "") == "anthropic-messages"
    assert effective_provider("openai-completions", "   ") == "openai-completions"
    assert effective_provider("openai-completions", "  stepfun ") == "stepfun"


def check_stored_schemas() -> None:
    with pytest.raises(ValidationError):
        model_config.StoredModelSettings(
            api="unknown", base_url="http://localhost/v1", model="m", provider="p"
        )
    with pytest.raises(ValidationError):
        model_config.StoredModelSettings(
            api="openai-completions", base_url="ftp://localhost/v1", model="m", provider="p"
        )
    with pytest.raises(ValidationError):
        model_config.StoredModelSettings(
            api="openai-completions", base_url="http://localhost/v1", model="  ", provider="p"
        )
    with pytest.raises(ValidationError):
        model_config.StoredModelSettings(
            api="openai-completions",
            base_url="http://localhost/v1",
            model="m",
            provider="",
        )
    with pytest.raises(ValidationError):
        model_config.StoredModelSettings(
            api="openai-completions",
            base_url="http://localhost/v1",
            model="m",
            provider="p",
            extra="x",
        )
    with pytest.raises(ValidationError):
        model_config.StoredAuth(api_key="   ")
    assert model_config.StoredAuth(api_key=None).api_key is None


def check_snapshot_consistency() -> None:
    # 13. 保存期间取快照不混用不同保存操作的端点和 Key。
    with TemporaryDirectory() as directory:
        root = Path(directory)
        original = model_config.DATA_ROOT
        model_config.DATA_ROOT = root
        pairs = (
            ("http://first.example/v1", "key-first"),
            ("http://second.example/v1", "key-second"),
        )
        try:
            save_provider(
                api="openai-completions",
                base_url=pairs[0][0],
                model="m",
                provider=None,
                api_key=pairs[0][1],
            )
            mixed: list[tuple[str, str]] = []

            def writer() -> None:
                for index in range(400):
                    base_url, api_key = pairs[index % 2]
                    save_provider(
                        api="openai-completions",
                        base_url=base_url,
                        model="m",
                        provider=None,
                        api_key=api_key,
                    )

            def reader() -> None:
                for _ in range(400):
                    config = load_model_config()
                    if (config.base_url, config.api_key) not in pairs:
                        mixed.append((config.base_url, config.api_key))

            threads = [Thread(target=writer), Thread(target=reader)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert not mixed, mixed

            # 14. 运行取得快照后，保存或清除配置不改变已有快照。
            snapshot = load_model_config()
            before = (snapshot.base_url, snapshot.model, snapshot.api_key)
            assert before[0] in {pairs[0][0], pairs[1][0]}
            save_provider(
                api="openai-completions",
                base_url="http://changed.example/v1",
                model="changed",
                provider="changed",
                api_key="key-changed",
            )
            assert (snapshot.base_url, snapshot.model, snapshot.api_key) == before
            assert snapshot.base_url != "http://changed.example/v1"
            assert snapshot.api_key != "key-changed"
            current = load_model_config()
            assert current.base_url == "http://changed.example/v1"
            clear_provider()
            assert (snapshot.base_url, snapshot.model, snapshot.api_key) == before
            assert load_provider_status().model_dump() == EMPTY_STATUS
        finally:
            model_config.DATA_ROOT = original


def check_null_bodies() -> None:
    # 4. 文件存在但正文为 null 属于损坏，不得被当作未配置。
    with TemporaryDirectory() as directory:
        root = Path(directory)
        original = model_config.DATA_ROOT
        model_config.DATA_ROOT = root
        try:
            (root / "models.json").write_text("null", encoding="utf-8")
            (root / "auth.json").write_text('{"api_key": "k"}', encoding="utf-8")
            with pytest.raises(ModelConfigError):
                load_provider_status()
            with pytest.raises(ModelConfigError):
                load_model_config()
            (root / "models.json").write_text(
                json.dumps(
                    {
                        "api": "openai-completions",
                        "base_url": "http://localhost/v1",
                        "model": "m",
                        "provider": "openai-completions",
                    }
                ),
                encoding="utf-8",
            )
            (root / "auth.json").write_text("null", encoding="utf-8")
            with pytest.raises(ModelConfigError):
                load_provider_status()
            with pytest.raises(ModelConfigError):
                load_model_config()
        finally:
            model_config.DATA_ROOT = original


def check_replace_failure_rollback() -> None:
    # 1. 第二次替换失败时不得留下“新端点 + 旧 Key”的混合配置。
    with TemporaryDirectory() as directory:
        root = Path(directory)
        original = model_config.DATA_ROOT
        model_config.DATA_ROOT = root
        try:
            save_provider(
                api="openai-completions",
                base_url="http://old.example/v1",
                model="old",
                provider=None,
                api_key="old-key",
            )
            before = load_provider_status().model_dump()
            before_models = (root / "models.json").read_text(encoding="utf-8")
            handle = (root / "auth.json").open("rb")
            try:
                with pytest.raises(ModelConfigError):
                    save_provider(
                        api="anthropic-messages",
                        base_url="http://new.example/v1",
                        model="new",
                        provider=None,
                        api_key="new-key",
                    )
            finally:
                handle.close()
            # 替换中途失败即回滚：models.json 不得停留在新值。
            assert (root / "models.json").read_text(encoding="utf-8") == before_models
            # 句柄释放后读取触发日志回滚，恢复一致状态并清理临时与日志文件。
            assert load_provider_status().model_dump() == before
            assert not (root / "provider.journal").exists()
            assert not (root / "models.json.tmp").exists()
            assert not (root / "auth.json.tmp").exists()
        finally:
            model_config.DATA_ROOT = original


def check() -> None:
    check_effective_provider()
    check_stored_schemas()
    check_snapshot_consistency()
    check_null_bodies()
    check_replace_failure_rollback()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        original = model_config.DATA_ROOT
        model_config.DATA_ROOT = root
        try:
            # 1. 未配置：五字段均为 null，凭据为空，模型运行配置缺失。
            assert load_provider_status().model_dump() == EMPTY_STATUS
            assert saved_api_key() is None
            with pytest.raises(ModelNotConfigured):
                load_model_config()

            # 2/3. 两个合法协议、HTTP/HTTPS 与路径式模型 ID。
            first = save_provider(
                api="openai-completions",
                base_url="http://localhost:11434/v1",
                model="vendor/model-7b",
                provider=None,
                api_key="key-openai",
            )
            assert first.model_dump() == {
                "api": "openai-completions",
                "base_url": "http://localhost:11434/v1",
                "model": "vendor/model-7b",
                "provider": "openai-completions",
                "api_key": "key-openai",
            }
            second = save_provider(
                api="anthropic-messages",
                base_url="https://api.example.com",
                model="claude-3-5-sonnet",
                provider="  example  ",
                api_key="key-anthropic",
            )
            assert second.provider == "example"

            # 5. 再次读取与“重启”恢复：文件是唯一事实来源。
            assert load_provider_status().model_dump() == second.model_dump()
            snapshot = load_model_config()
            assert (snapshot.api, snapshot.base_url, snapshot.model) == (
                "anthropic-messages",
                "https://api.example.com",
                "claude-3-5-sonnet",
            )
            assert snapshot.provider == "example" and snapshot.api_key == "key-anthropic"
            assert "key-anthropic" not in repr(snapshot)
            models = json.loads((root / "models.json").read_text(encoding="utf-8"))
            auth = json.loads((root / "auth.json").read_text(encoding="utf-8"))
            assert models == {
                "api": "anthropic-messages",
                "base_url": "https://api.example.com",
                "model": "claude-3-5-sonnet",
                "provider": "example",
            }
            assert auth == {"api_key": "key-anthropic"}

            # 7. 清除移除整条配置；重复清除结果一致。
            cleared = clear_provider()
            assert cleared.model_dump() == EMPTY_STATUS
            assert not (root / "models.json").exists()
            assert not (root / "auth.json").exists()
            assert clear_provider().model_dump() == cleared.model_dump()
            assert load_provider_status().model_dump() == cleared.model_dump()
            with pytest.raises(ModelNotConfigured):
                load_model_config()
            # 清除后重新提交完整配置可恢复。
            restored = save_provider(
                api="openai-completions",
                base_url="https://api.example.com",
                model="m",
                provider="example",
                api_key="key-2",
            )
            assert restored.api_key == "key-2"
            assert load_model_config().api_key == "key-2"

            # 8. 损坏文件严格报错，不回落默认模型。
            (root / "models.json").write_text("{not-json", encoding="utf-8")
            with pytest.raises(ModelConfigError):
                load_provider_status()
            with pytest.raises(ModelConfigError):
                load_model_config()
            (root / "models.json").write_text(
                json.dumps({"api": "openai-completions"}), encoding="utf-8"
            )
            with pytest.raises(ModelConfigError):
                load_provider_status()
            (root / "models.json").write_text(
                json.dumps(
                    {
                        "api": "openai-completions",
                        "base_url": "http://localhost/v1",
                        "model": "m",
                        "provider": "openai-completions",
                    }
                ),
                encoding="utf-8",
            )
            (root / "auth.json").write_text('{"api_key": ""}', encoding="utf-8")
            with pytest.raises(ModelConfigError):
                load_provider_status()
        finally:
            model_config.DATA_ROOT = original

    # 8. 保存失败明确暴露：目标目录不可写。
    with TemporaryDirectory() as directory:
        blocker = Path(directory) / "blocked"
        blocker.write_text("x", encoding="utf-8")
        original = model_config.DATA_ROOT
        model_config.DATA_ROOT = blocker
        try:
            with pytest.raises(ModelConfigError):
                save_provider(
                    api="openai-completions",
                    base_url="http://localhost/v1",
                    model="m",
                    provider=None,
                    api_key="k",
                )
        finally:
            model_config.DATA_ROOT = original

    print("模型配置 JSON 持久化、协议/URL/模型校验、provider 生效规则、清除与损坏处理检查通过")


if __name__ == "__main__":
    check()
