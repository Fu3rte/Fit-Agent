"""生产模型目录与框架 profile 覆盖（S4-01；08「容量、估算与溢出」）。

首版只支持一个模型 ``deepseek-flash``（官方 context length 1,000,000、最大输出
384,000），并在启动校验阶段**拒绝未知模型 id**：框架已知模型名与 DeepSeek profile
都不含该 id（框架只有 ``deepseek-v4-flash`` 等旧别名），依赖框架名推断会把生产配置
静默落到别的模型上，因此这里显式覆盖 profile，不做别名猜测。

思考模式（2026-09-12 用户拍板）：生产保持 Provider 默认**开启** Thinking——这是产品策略，
不是模型能力限制（Provider 两种模式都支持）：本阶段不提供按 Run 的思考开关、不传任何关闭参数，
因此实际行为就是默认开启。框架对未知名字的 DeepSeek profile 按 ``deepseek-v4-*`` 前缀推断，
会把该 id 误报为 ``supports_thinking=False`` / ``openai_reasoning_enabled_by_default=False``；
真实调用实测响应含思考内容，故本目录如实覆盖为支持思考且默认开启（见 :func:`resolve_model_profile`）。

Provider 接入（含 ``openai`` SDK）在 :mod:`runtime.provider`；本模块不构造模型对象、
不读取凭据、不发请求。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.deepseek import deepseek_model_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile

#: DeepSeek 官方兼容端点（PLAN.md：仅该端点完成 spike 验证）。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class ModelSpec:
    """一个受支持模型的固定事实：窗口与输出上限来自官方文档只读核对（08）。"""

    model_id: str
    provider: str
    base_url: str
    context_window: int
    max_output_tokens: int


DEEPSEEK_FLASH = ModelSpec(
    model_id="deepseek-flash",
    provider="deepseek",
    base_url=DEEPSEEK_BASE_URL,
    context_window=1_000_000,
    max_output_tokens=384_000,
)

#: 受支持模型目录：键即配置中允许出现的模型 id，其他一律拒绝启动。
SUPPORTED_MODELS: Mapping[str, ModelSpec] = {DEEPSEEK_FLASH.model_id: DEEPSEEK_FLASH}


class UnknownModelId(RuntimeError):
    """配置了目录外的模型 id：拒绝启动，不推断、不降级到别的模型（08）。"""


def require_supported_model_id(model_id: str) -> ModelSpec:
    """启动校验入口：目录内返回规格，目录外抛 :class:`UnknownModelId`。"""
    spec = SUPPORTED_MODELS.get(model_id)
    if spec is None:
        raise UnknownModelId(
            f"不支持的模型 id: {model_id!r}；首版仅支持 {sorted(SUPPORTED_MODELS)}"
        )
    return spec


def resolve_model_profile(model_id: str) -> ModelProfile:
    """目录内模型的框架 profile：框架 DeepSeek profile + 本目录的显式事实覆盖。

    覆盖两件事，都是目录事实而非框架推断：

    1. ``context_window`` = 1,000,000（框架对未知名字不给窗口，可能被 genai-prices 填成别的值）。
    2. 思考支持与默认行为：``supports_thinking=True``、``openai_reasoning_enabled_by_default=True``
       （Provider 默认即开启思考）。``thinking_always_enabled`` 保持 False——Provider 同时支持
       非思考模式，写成 True 是不实的能力声明；生产策略（不提供按 Run 的思考开关、不传关闭参数）
       已足以让默认行为生效，不得靠把模型描述成“无法关闭思考”来实现策略。
    """
    spec = require_supported_model_id(model_id)
    framework = deepseek_model_profile(spec.model_id)
    return merge_profile(
        framework,
        ModelProfile(context_window=spec.context_window),
        OpenAIModelProfile(
            supports_thinking=True,
            thinking_always_enabled=False,
            openai_reasoning_enabled_by_default=True,
        ),
    )
