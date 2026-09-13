"""S4-01：模型目录/profile 覆盖与错误码封闭契约（stage4.md S4-01 验收）。

覆盖：
1. ``deepseek-flash`` 是唯一受支持模型，窗口 1,000,000、输出上限 384,000；
   profile 显式覆盖 ``context_window``（框架对该 id 不给窗口，必须自己写死）。
   思考支持与默认行为同样由目录如实覆盖（2026-09-12 已拍：Provider 默认开启思考；不提供按 Run 开关）。
2. 目录外模型 id（含框架已知的旧别名）拒绝启动：不推断、不降级到别的模型。
3. 错误码封闭集合恰为已冻结的六个（含 2026-09-12 拍板新增的 ``model_request_failed``），
   且 ``conversation_busy`` 的 HTTP 409 映射只登记一次。
4. 冻结的对话／Run／SSE 路由集合（S4-07 交付后按 stage4.md §6 正向断言，不新增旁路）。

离线：全部为纯函数与静态表检查，不构造客户端、不读凭据、不发请求
（真实连通性证据见 ``scripts/deepseek_smoke.py``，默认套件保持离线）。
"""

from pathlib import Path

import pytest
from pydantic_ai.models import known_model_names
from pydantic_ai.profiles.deepseek import deepseek_model_profile

from api.app import create_app
from api.dto import _ERROR_STATUS
from runtime.error_codes import (
    CONTEXT_BUDGET_EXCEEDED,
    CONVERSATION_BUSY,
    INTERRUPTED_BY_RESTART,
    MODEL_REQUEST_FAILED,
    MODEL_REQUEST_TIMEOUT,
    RUN_FAILURE_CODES,
    RUN_TIMEOUT,
    RUNTIME_ERROR_CODES,
    is_runtime_error_code,
)
from runtime.models import (
    DEEPSEEK_FLASH,
    SUPPORTED_MODELS,
    UnknownModelId,
    require_supported_model_id,
    resolve_model_profile,
)
from storage.errors import ConversationBusy


def test_deepseek_flash_is_the_only_supported_model() -> None:
    spec = require_supported_model_id("deepseek-flash")
    assert spec == DEEPSEEK_FLASH
    assert spec.provider == "deepseek"
    assert spec.base_url == "https://api.deepseek.com"
    assert spec.context_window == 1_000_000
    assert spec.max_output_tokens == 384_000
    assert set(SUPPORTED_MODELS) == {"deepseek-flash"}


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek-v4-flash",  # 框架已知的旧别名：停用，不静默映射到生产模型
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-flash-latest",
        "gpt-5",
        "",
        "deepseek:deepseek-flash",  # 带 provider 前缀的写法同样不是目录内的 id
    ],
)
def test_unknown_model_id_is_rejected(model_id: str) -> None:
    with pytest.raises(UnknownModelId):
        require_supported_model_id(model_id)
    with pytest.raises(UnknownModelId):
        resolve_model_profile(model_id)


def test_profile_reports_thinking_support_and_default_truthfully() -> None:
    """框架对该 id 名按 deepseek-v4-* 前缀推断，会把思考误报为关闭；目录必须如实覆盖。

    已拍策略（2026-09-12）：保持 Provider 默认开启 Thinking（Provider 两种模式都支持，
    所以这不是能力限制）；本阶段不提供按 Run 的开关、不传关闭参数，因此默认行为生效。
    """
    framework = deepseek_model_profile("deepseek-flash")
    assert framework is not None
    assert (
        framework.get("supports_thinking") is False
    )  # 框架的既有推断（与我们的事实不符）
    assert framework.get("thinking_always_enabled") is False

    profile = resolve_model_profile("deepseek-flash")
    assert profile.get("supports_thinking") is True
    # 能力声明必须如实：Provider 支持非思考模式，故不能声称模型无法关闭思考
    assert profile.get("thinking_always_enabled") is False
    assert profile.get("openai_reasoning_enabled_by_default") is True
    # 窗口覆盖不受影响
    assert profile.get("context_window") == 1_000_000


def test_profile_override_supplies_context_window_framework_does_not_have() -> None:
    # 框架的已知模型名与 DeepSeek profile 都不含 deepseek-flash：
    # 因此窗口只能由我们的目录显式覆盖，不能依赖框架名推断或 genai-prices 兜底。
    assert "deepseek:deepseek-flash" not in known_model_names()
    assert "deepseek:deepseek-v4-flash" in known_model_names()

    profile = resolve_model_profile("deepseek-flash")
    assert profile.get("context_window") == 1_000_000
    # 框架层其它既有事实仍被保留（本目录只覆盖窗口与思考支持/默认行为）
    assert profile.get("ignore_streamed_leading_whitespace") is False


def test_runtime_error_codes_are_closed() -> None:
    assert {
        CONVERSATION_BUSY,
        INTERRUPTED_BY_RESTART,
        MODEL_REQUEST_TIMEOUT,
        RUN_TIMEOUT,
        CONTEXT_BUDGET_EXCEEDED,
        MODEL_REQUEST_FAILED,  # 2026-09-12 拍板新增：永久模型失败与计数池耗尽
    } == RUNTIME_ERROR_CODES
    assert set(RUN_FAILURE_CODES).issubset(RUNTIME_ERROR_CODES)
    assert CONVERSATION_BUSY not in RUN_FAILURE_CODES  # HTTP 错误码不是 Run 终态原因

    assert is_runtime_error_code(INTERRUPTED_BY_RESTART)
    assert not is_runtime_error_code(
        "draft_stale"
    )  # Stage 2/3 的业务码不属于运行时集合
    assert not is_runtime_error_code(None)
    assert not is_runtime_error_code(123)


def test_conversation_busy_http_mapping_is_registered_once() -> None:
    entries = [
        (status, code)
        for exc_type, status, code in _ERROR_STATUS
        if exc_type is ConversationBusy
    ]
    assert entries == [(409, CONVERSATION_BUSY)]


def test_chat_run_and_sse_route_surface_is_the_frozen_one(tmp_path: Path) -> None:
    """S4-07 接线后：对话／Run／SSE 路由就是 stage4.md §6 冻结的那一组，且不新增旁路。

    本用例原为 S4-01 的切片内负向守卫（当时禁止预支 S4-07 的 HTTP 面）；S4-07 交付后按已拍
    契约改为正向断言，并修正路由枚举：FastAPI 0.141 把 ``include_router`` 条目保留为
    ``_IncludedRouter``，直接读 ``app.routes`` 看不到任何子路由（旧写法已空转）。
    """
    frozen = {
        ("/api/sessions", "POST"),
        ("/api/sessions/{session_id}", "GET"),
        ("/api/sessions/{session_id}/requests", "POST"),
        ("/api/runs/{run_id}", "GET"),
        ("/api/runs/{run_id}/retry", "POST"),
        ("/api/runs/{run_id}/cancel", "POST"),
        ("/api/runs/{run_id}/events", "GET"),
    }
    table = _route_table(create_app(tmp_path))
    assert frozen <= table
    # 非空断言：路由表确实被展开了（空表会让上面的子集断言也“通过”）
    assert ("/healthz", "GET") in table
    assert ("/api/profile", "GET") in table
    # 不得新增旁路：没有公开建草稿入口，也没有聊天以外的执行面或事件面
    assert not any(
        path == "/api/drafts" and method in ("POST", "PUT", "PATCH")
        for path, method in table
    )
    for forbidden in ("/api/chat", "/api/events", "/api/models"):
        assert not any(path.startswith(forbidden) for path in _paths(table)), forbidden


def _route_table(app) -> set[tuple[str, str]]:
    """(路径, 方法) 全集：展开 ``_IncludedRouter``（FastAPI 0.141 不再扁平化子路由）。"""
    table: set[tuple[str, str]] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", ()) or ():
            table.add((path, method))
    return table


def _paths(table: set[tuple[str, str]]) -> set[str]:
    return {path for path, _method in table}
