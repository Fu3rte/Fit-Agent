import asyncio
import json
import math
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import openai
from pydantic import ValidationError

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_config import (
    API_KEY_VARIABLE,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    DIMENSIONS_VARIABLE,
    MODEL_VARIABLE,
    WORKSPACE_VARIABLE,
    EmbeddingConfig,
    MissingRetrievalConfig,
    RetrievalConfigError,
    load_embedding_config,
)
from app.application.business.retrieval_embedding import (
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingResponseError,
    validate_embedding_data,
)

# RFC 5737 保留地址：仅用于超时与取消的失败路径探针，不是 API 服务调用。
PROBE_BASE_URL = "https://192.0.2.1/compatible-mode/v1"
PROBE_KEY = "probe-not-a-credential"

BACKEND = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = BACKEND.parent / "tmp" / "action-retrieval-t4"


def write_evidence(name: str, payload: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return path


def probe_config(**overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = {
        "api_key": PROBE_KEY,
        "base_url": PROBE_BASE_URL,
        "model": DEFAULT_EMBEDDING_MODEL,
        "dimensions": DEFAULT_EMBEDDING_DIMENSIONS,
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def _item(index: object, embedding: object) -> SimpleNamespace:
    return SimpleNamespace(index=index, embedding=embedding)


def _vector(value: float, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    vector[0] = float(value)
    return vector


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    return dot / (_norm(left) * _norm(right))


def _expect_response_error(callable_) -> None:
    try:
        callable_()
    except EmbeddingResponseError:
        return
    raise AssertionError("返回校验器未拒绝非法输入")


async def _expect_async_error(callable_, exc, *args) -> None:
    try:
        await callable_(*args)
    except exc:
        return
    raise AssertionError(f"未按预期抛出 {exc.__name__}")


def check_validators() -> None:
    dims = 4
    # 服务端顺序乱序时仍按 index 对齐输入。
    out = validate_embedding_data(
        [_item(1, _vector(2.0, dims)), _item(0, _vector(1.0, dims))], 2, dims
    )
    assert out[0][0] == 1.0 and out[1][0] == 2.0, "必须按 index 对齐而非服务端顺序"

    _expect_response_error(
        lambda: validate_embedding_data([_item(0, _vector(1.0, dims))], 2, dims)
    )
    _expect_response_error(
        lambda: validate_embedding_data(
            [
                _item(0, _vector(1.0, dims)),
                _item(1, _vector(1.0, dims)),
                _item(2, _vector(1.0, dims)),
            ],
            2,
            dims,
        )
    )
    _expect_response_error(
        lambda: validate_embedding_data(
            [_item(0, _vector(1.0, dims)), _item(0, _vector(1.0, dims))], 2, dims
        )
    )
    _expect_response_error(
        lambda: validate_embedding_data(
            [_item(0, _vector(1.0, dims)), _item(2, _vector(1.0, dims))], 2, dims
        )
    )
    _expect_response_error(
        lambda: validate_embedding_data(
            [_item(-1, _vector(1.0, dims)), _item(1, _vector(1.0, dims))], 2, dims
        )
    )
    for bad_index in (1.0, True, "0", None):
        _expect_response_error(
            lambda bad=bad_index: validate_embedding_data(
                [_item(bad, _vector(1.0, dims)), _item(1, _vector(1.0, dims))], 2, dims
            )
        )
    _expect_response_error(
        lambda: validate_embedding_data([_item(0, [0.0] * dims)], 1, dims)
    )
    for bad_value in (float("nan"), float("inf"), float("-inf")):
        _expect_response_error(
            lambda bad=bad_value: validate_embedding_data(
                [_item(0, [bad] + [1.0] * (dims - 1))], 1, dims
            )
        )
    _expect_response_error(
        lambda: validate_embedding_data(
            [_item(0, [True] + [1.0] * (dims - 1))], 1, dims
        )
    )
    _expect_response_error(
        lambda: validate_embedding_data([_item(0, _vector(1.0, dims - 1))], 1, dims)
    )


def _expect_missing(environ: dict, variables: set[str]) -> None:
    try:
        load_embedding_config(environ)
    except MissingRetrievalConfig as error:
        assert set(error.variables) == variables, (error.variables, variables)
        return
    raise AssertionError("缺少必需配置时未失败")


def _expect_config_error(environ: dict) -> None:
    try:
        load_embedding_config(environ)
    except RetrievalConfigError:
        return
    raise AssertionError("非法配置未失败")


def check_config_failures() -> dict:
    _expect_missing({}, {API_KEY_VARIABLE, WORKSPACE_VARIABLE})
    _expect_missing({API_KEY_VARIABLE: "k"}, {WORKSPACE_VARIABLE})
    _expect_missing({WORKSPACE_VARIABLE: "w"}, {API_KEY_VARIABLE})
    _expect_missing(
        {API_KEY_VARIABLE: "   ", WORKSPACE_VARIABLE: "w"}, {API_KEY_VARIABLE}
    )
    _expect_missing(
        {API_KEY_VARIABLE: "k", WORKSPACE_VARIABLE: "  "}, {WORKSPACE_VARIABLE}
    )

    base = {API_KEY_VARIABLE: "k", WORKSPACE_VARIABLE: "6956884"}
    for bad_dimensions in ("512", "abc", "1024.0"):
        _expect_config_error({**base, DIMENSIONS_VARIABLE: bad_dimensions})
    _expect_config_error({API_KEY_VARIABLE: "k", WORKSPACE_VARIABLE: "bad space"})

    default = load_embedding_config(base)
    assert default.model == DEFAULT_EMBEDDING_MODEL
    assert default.dimensions == DEFAULT_EMBEDDING_DIMENSIONS
    assert default.timeout is None
    assert (
        default.base_url
        == "https://6956884.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    override = load_embedding_config(
        {**base, MODEL_VARIABLE: "custom-model", DIMENSIONS_VARIABLE: "1024"}
    )
    assert override.model == "custom-model"

    secret = "probe-secret-9f3"
    hidden = load_embedding_config({API_KEY_VARIABLE: secret, WORKSPACE_VARIABLE: "w"})
    assert secret not in repr(hidden), "配置对象表示不得包含密钥"
    try:
        load_embedding_config({API_KEY_VARIABLE: secret, WORKSPACE_VARIABLE: "bad space"})
    except RetrievalConfigError as error:
        assert secret not in str(error), "校验错误不得包含密钥"
    return {"base_url_suffix": "maas.aliyuncs.com/compatible-mode/v1"}


async def _input_failures() -> None:
    # 探针 Base URL 不可达：以下输入必须在发请求前失败，否则会连接失败而非抛输入异常。
    client = EmbeddingClient(probe_config())
    for blank in ("", "   ", "\t\n"):
        await _expect_async_error(client.encode_query, ValueError, blank)
    await _expect_async_error(client.encode_batch, ValueError, [])
    await _expect_async_error(client.encode_batch, ValueError, ["有效文本", "  "])
    await _expect_async_error(client.encode_query, TypeError, 123)


async def _lifecycle() -> None:
    client = EmbeddingClient(probe_config())
    async with client as entered:
        assert entered is client
        first = client.client
        assert first.is_closed() is False
    assert first.is_closed() is True, "上下文退出必须释放客户端"
    try:
        client.client
    except RuntimeError:
        pass
    else:
        raise AssertionError("上下文退出后仍可访问客户端")

    async with client:
        second = client.client
        assert second.is_closed() is False
    assert second.is_closed() is True

    other = EmbeddingClient(probe_config())
    async with other:
        raw = other.client
        try:
            async with other:
                raise AssertionError("重复进入上下文应失败")
        except RuntimeError:
            pass
    assert raw.is_closed() is True


def _timeout_parts(value: object) -> SimpleNamespace:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return SimpleNamespace(connect=float(value), read=float(value), write=float(value))
    connect = getattr(value, "connect", None)
    read = getattr(value, "read", None)
    write = getattr(value, "write", None)
    return SimpleNamespace(
        connect=float(connect) if connect is not None else None,
        read=float(read) if read is not None else None,
        write=float(write) if write is not None else None,
    )


async def _timeout_values() -> tuple[SimpleNamespace, SimpleNamespace]:
    default_client = EmbeddingClient(probe_config())
    async with default_client:
        default_parts = _timeout_parts(default_client.effective_timeout)
    assert default_parts.connect is not None and default_parts.connect > 0

    explicit_client = EmbeddingClient(probe_config(), timeout=7.5)
    async with explicit_client:
        explicit_parts = _timeout_parts(explicit_client.effective_timeout)
    assert explicit_parts.connect == 7.5 and explicit_parts.read == 7.5

    try:
        EmbeddingClient(probe_config(), timeout=0)
    except ValueError:
        pass
    else:
        raise AssertionError("非正超时应失败")
    return default_parts, explicit_parts


async def _probe() -> dict:
    # 有限超时传播。
    timeout_client = EmbeddingClient(probe_config(), timeout=1.0)
    async with timeout_client as client:
        raw = timeout_client.client
        start = time.perf_counter()
        try:
            await client.encode_query("超时探针")
            timeout_outcome = "completed"
        except (openai.APITimeoutError, openai.APIConnectionError) as error:
            timeout_outcome = type(error).__name__
        timeout_elapsed = time.perf_counter() - start
    assert raw.is_closed() is True
    assert timeout_outcome != "completed", "保留地址不应编码成功"

    # 任务取消传播：进入连接等待后取消，客户端仍须释放。
    cancel_client = EmbeddingClient(probe_config(), timeout=30.0)
    async with cancel_client as client:
        raw = cancel_client.client
        task = asyncio.create_task(client.encode_query("取消探针"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            cancel_outcome = "completed"
        except asyncio.CancelledError:
            cancel_outcome = "CancelledError"
        except (openai.APITimeoutError, openai.APIConnectionError) as error:
            cancel_outcome = type(error).__name__
    assert raw.is_closed() is True
    assert cancel_outcome == "CancelledError", f"取消未正常传播: {cancel_outcome}"
    return {
        "timeout_outcome": timeout_outcome,
        "timeout_elapsed_seconds": round(timeout_elapsed, 3),
        "cancel_outcome": cancel_outcome,
    }


def check_timeout_boundaries() -> None:
    for value in (None, 1, 0.25, 7.5):
        config = probe_config(timeout=value)
        assert config.timeout == value
        client = EmbeddingClient(probe_config(), timeout=value)
        assert client._timeout == value
        inherited = EmbeddingClient(config)
        assert inherited._timeout == value
    assert EmbeddingClient(probe_config(timeout=2.5), timeout=1)._timeout == 1
    for value in (0, -1, -0.25, True, False, float("nan"), float("inf"),
                  float("-inf"), "1", [], {}):
        try:
            probe_config(timeout=value)
        except ValidationError:
            pass
        else:
            raise AssertionError("配置接受了非法 timeout")
        try:
            EmbeddingClient(probe_config(), timeout=value)
        except ValueError:
            pass
        else:
            raise AssertionError("显式覆盖接受了非法 timeout")


async def _reject_foreign_loop(client: EmbeddingClient) -> None:
    for operation, args in (
        (client.encode_query, ("循环归属探针",)),
        (client.encode_batch, (["循环归属探针"],)),
        (client.__aexit__, (None, None, None)),
    ):
        try:
            await operation(*args)
        except RuntimeError as error:
            assert str(error) == "EmbeddingClient 必须在所属事件循环中使用"
        else:
            raise AssertionError("跨循环操作未被拒绝")


async def _owner_still_usable(client: EmbeddingClient, raw: openai.AsyncOpenAI) -> None:
    assert client.client is raw
    assert not raw.is_closed()
    assert client.usage.requests == 0
    assert client.effective_timeout is not None
    await _expect_async_error(client.encode_query, ValueError, " ")
    await _expect_async_error(client.__aenter__, RuntimeError)


def check_loop_boundaries() -> None:
    client = EmbeddingClient(probe_config())
    with asyncio.Runner() as owner:
        owner.run(client.__aenter__())
        raw = client._client
        assert raw is not None
        try:
            # 两个 Runner 在同一线程顺序运行，SDK 不执行网络请求。
            with asyncio.Runner() as foreign:
                foreign.run(_reject_foreign_loop(client))
            owner.run(_owner_still_usable(client, raw))
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(asyncio.run, _reject_foreign_loop(client)).result()
            owner.run(_owner_still_usable(client, raw))
        finally:
            owner.run(client.__aexit__(None, None, None))
        assert raw.is_closed()
        assert client._client is None and client._loop is None
        owner.run(_expect_async_error(client.encode_query, RuntimeError, "上下文外探针"))
        owner.run(_lifecycle())


async def _cancel_context() -> None:
    client = EmbeddingClient(probe_config())
    entered = asyncio.Event()
    raw = None

    async def use_client() -> None:
        nonlocal raw
        async with client:
            raw = client.client
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use_client())
    await entered.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("上下文取消未传播")
    assert raw is not None and raw.is_closed()
    assert client._client is None and client._loop is None
    async with client:
        assert not client.client.is_closed()


def check_boundary() -> None:
    check_timeout_boundaries()
    check_loop_boundaries()
    asyncio.run(_cancel_context())
    write_evidence(
        "boundary-check.json",
        {
            "config_timeout_boundaries": "passed",
            "explicit_timeout_boundaries": "passed",
            "same_thread_foreign_loop_request_and_close": "passed",
            "foreign_thread_request_and_close": "passed",
            "owner_resource_retained_and_closed": "passed",
            "repeat_entry_and_outside_context": "passed",
            "cancel_context_cleanup_and_reentry": "passed",
            "sdk_network_requests": 0,
        },
    )
    print("边界校验：timeout、循环归属、资源保留与取消收尾通过")


def _sample_texts(count: int) -> list[str]:
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    supplements = rt.supplement_index(rt.load_supplements())
    texts: list[str] = []
    for exercise in catalog.all()[:count]:
        texts.append(
            rt.build_unified_text(exercise, supplements.get(exercise.id), mappings)
        )
    return texts


async def _live() -> dict:
    texts = _sample_texts(MAX_BATCH_SIZE + 1)
    client = EmbeddingClient.from_env()
    async with client as active:
        started = time.perf_counter()
        zh = await active.encode_query("杠铃卧推")
        english = await active.encode_query("barbell bench press")
        mixed = await active.encode_query("哑铃卧推 dumbbell bench press")
        query_elapsed = time.perf_counter() - started

        before = active.usage.requests
        batch_started = time.perf_counter()
        vectors = await active.encode_batch(texts)
        batch_elapsed = time.perf_counter() - batch_started
        batch_requests = active.usage.requests - before

        first = await active.encode_query(texts[0])
        last = await active.encode_query(texts[MAX_BATCH_SIZE])
        usage = active.usage
        model = active.config.model
        configured_dimensions = active.config.dimensions

    assert configured_dimensions == 1024, "本任务只支持 1024 维"
    dimensions = configured_dimensions
    for vector in (zh, english, mixed):
        assert len(vector) == dimensions
        assert all(isinstance(value, float) for value in vector)
        assert all(math.isfinite(value) for value in vector)
        assert _norm(vector) > 0.0
    assert len(vectors) == MAX_BATCH_SIZE + 1
    assert all(len(vector) == dimensions for vector in vectors)
    assert all(_norm(vector) > 0.0 for vector in vectors)
    assert batch_requests == 2, "21 条输入应分为 20 + 1 两批"
    first_alignment = _cosine(vectors[0], first)
    last_alignment = _cosine(vectors[MAX_BATCH_SIZE], last)
    assert first_alignment > 0.99 and last_alignment > 0.99, "跨批输出顺序未对齐输入"

    return {
        "model": model,
        "dimensions": dimensions,
        "query_samples": ["杠铃卧推", "barbell bench press", "哑铃卧推 dumbbell bench press"],
        "batch_input_count": len(texts),
        "batch_requests": batch_requests,
        "vector_count": len(vectors),
        "query_elapsed_seconds": round(query_elapsed, 3),
        "batch_elapsed_seconds": round(batch_elapsed, 3),
        "requests_total": usage.requests,
        "prompt_tokens_total": usage.prompt_tokens,
        "total_tokens_total": usage.total_tokens,
        "first_alignment_cosine": round(first_alignment, 6),
        "last_alignment_cosine": round(last_alignment, 6),
    }


async def _auth() -> dict:
    config = load_embedding_config()
    invalid = config.model_copy(update={"api_key": "sk-invalid-probe-000"})
    client = EmbeddingClient(invalid)
    async with client as active:
        try:
            await active.encode_query("鉴权探针")
            outcome = "completed"
        except openai.AuthenticationError:
            outcome = "AuthenticationError"
        except openai.APIStatusError as error:
            outcome = f"APIStatusError:{error.status_code}"
    assert outcome != "completed", "非法凭据不应编码成功"
    return {"auth_outcome": outcome}


def check_offline() -> None:
    check_validators()
    config_summary = check_config_failures()
    asyncio.run(_input_failures())
    asyncio.run(_lifecycle())
    default_parts, explicit_parts = asyncio.run(_timeout_values())
    write_evidence(
        "boundary-offline.json",
        {
            "validators": "passed",
            "config_failures": "passed",
            "input_failures": "passed",
            "lifecycle": "passed",
            "default_timeout_seconds": {
                "connect": default_parts.connect,
                "read": default_parts.read,
                "write": default_parts.write,
            },
            "explicit_timeout_seconds": {
                "connect": explicit_parts.connect,
                "read": explicit_parts.read,
                "write": explicit_parts.write,
            },
            "config": config_summary,
        },
    )
    print("离线校验：返回校验、配置与输入失败、生命周期、超时取值通过")


def check_probe() -> None:
    result = asyncio.run(_probe())
    write_evidence("boundary-probe.json", result)
    print(f"失败路径探针：{result}")


def check_live() -> int:
    try:
        result = asyncio.run(_live())
    except MissingRetrievalConfig as error:
        print(f"BLOCKED 缺少必需配置: {', '.join(error.variables)}")
        return 2
    write_evidence("live.json", result)
    print(f"真实编码：{result}")
    return 0


def check_auth() -> int:
    try:
        result = asyncio.run(_auth())
    except MissingRetrievalConfig as error:
        print(f"BLOCKED 缺少必需配置: {', '.join(error.variables)}")
        return 2
    write_evidence("auth.json", result)
    print(f"鉴权失败：{result}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "offline"
    if command == "offline":
        check_offline()
    elif command == "probe":
        check_probe()
    elif command == "boundary":
        check_boundary()
    elif command == "live":
        return check_live()
    elif command == "auth":
        return check_auth()
    elif command == "all":
        check_offline()
        check_probe()
        return check_live()
    else:
        raise SystemExit(f"未知子命令: {command}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
