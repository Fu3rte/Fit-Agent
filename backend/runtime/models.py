"""生产模型目录与框架 profile 覆盖（S4-01；08「容量、估算与溢出」）。

目录里每个模型都在启动校验阶段**校验 id**：目录外一律**拒绝启动**，不做别名猜测。
``deepseek-flash``（官方 context length 1,000,000、最大输出 384,000）是 Stage 4 历史端点；
Stage 6（2026-09-13 拍板）生产端点为 owner 指定的 OpenAI 兼容端点（阿里云百炼
compatible-mode），模型 ``qwen3.7-flash``（2026-09-13 官方文档只读核对：上下文
1,000,000、最大输入 991,808、最大输出 131,072；见 :mod:`runtime.fees` 的费用依据）。
框架已知模型名不含这些 id（框架只有 ``deepseek-v4-flash`` 等旧别名），依赖框架名推断会把
生产配置静默落到别的模型上，因此这里显式覆盖 profile，不做别名猜测。

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

#: DeepSeek 官方兼容端点（PLAN.md：仅该端点完成 spike 验证；Stage 4 历史端点）。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: Stage 6 生产端点：owner 指定的 OpenAI 兼容端点（阿里云百炼 compatible-mode，
#: 业务空间专属域名；正本见 design-decisions「Stage 6 真实联调 Provider 范围」）。
#: 与 owner 本地 env ``MODEL_BASE_URL`` 一致；产品运行时只读目录，不读环境变量。
ALIYUN_BAILIAN_BASE_URL = (
    "https://ws-o2404joh7zvfydxz.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)


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

QWEN37_FLASH = ModelSpec(
    model_id="qwen3.7-flash",
    provider="aliyun-bailian",
    base_url=ALIYUN_BAILIAN_BASE_URL,
    context_window=1_000_000,
    max_output_tokens=131_072,
)

#: 受支持模型目录：键即配置中允许出现的模型 id，其他一律拒绝启动。
SUPPORTED_MODELS: Mapping[str, ModelSpec] = {
    DEEPSEEK_FLASH.model_id: DEEPSEEK_FLASH,
    QWEN37_FLASH.model_id: QWEN37_FLASH,
}


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

    1. ``context_window`` = 目录事实（框架对未知名字不给窗口，可能被 genai-prices 填成别的值）。
    2. 思考支持与默认行为：``supports_thinking=True``、``openai_reasoning_enabled_by_default=True``
       （Provider 默认即开启思考）。``thinking_always_enabled`` 保持 False——Provider 同时支持
       非思考模式，写成 True 是不实的能力声明；生产策略（不提供按 Run 的思考开关、不传关闭参数）
       已足以让默认行为生效，不得靠把模型描述成“无法关闭思考”来实现策略。

    两个模型都是混合思考模式、默认开启（``qwen3.7-flash`` 见官方「深度思考模型的用法」
    2026-09-13 只读核对），隐藏推理经 ``reasoning_content`` 返回并由框架映射为
    ``ThinkingPart``（不属于可见文本，产品/SSE 不外发）；本目录不设置该字段名，
    框架默认即识别 ``reasoning_content``。
    """
    spec = require_supported_model_id(model_id)
    framework = (
        deepseek_model_profile(spec.model_id)
        if spec.provider == DEEPSEEK_FLASH.provider
        else OpenAIModelProfile()
    )
    return merge_profile(
        framework,
        ModelProfile(context_window=spec.context_window),
        OpenAIModelProfile(
            supports_thinking=True,
            thinking_always_enabled=False,
            openai_reasoning_enabled_by_default=True,
        ),
    )
