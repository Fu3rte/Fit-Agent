"""数据目录（platformdirs）、固定业务时区来源、Harness 本地配置加载与硬边界校验（07 7.3、08 8.5、10.2）。

Stage 0 实现范围：数据目录/数据库路径解析 + 固定业务时区采样接缝。
Stage 4 S4-05a：Harness 本地配置（``<数据目录>/harness.toml``，文件缺省即全部默认值）、
已拍硬边界校验、容量值派生与启动交叉校验，以及每次 Run 开始时的有效配置冻结。越界、
未知键、未知模型或交叉不变量不成立均**拒绝启动**，不静默钳制（08 8.5）；运行期间修改
文件不影响已冻结的 Run（:class:`EffectiveHarness` 为不可变标量快照）。

分层：模型目录是冻结事实来源（:mod:`runtime.models`，只含 pydantic_ai）；本模块只向下读它，
runtime 不反向依赖本模块，因此不构成循环导入。
"""

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import platformdirs
import tzlocal

from runtime.models import (
    QWEN36_FLASH,
    ModelSpec,
    UnknownModelId,
    require_supported_model_id,
)

APP_NAME = "Fit-Agent"
DATABASE_FILENAME = "app.db"
DATA_DIR_OVERRIDE_ENV = "FIT_AGENT_DATA_DIR"

#: Harness 本地配置文件（08 8.5 选 B：本地配置 + 硬边界；格式与位置属实现细节）。
HARNESS_CONFIG_FILENAME = "harness.toml"


def resolve_data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """平台用户数据目录（10.2）。

    默认经 platformdirs 解析：``user_data_dir(APP_NAME, appauthor=False)``，
    Windows 为 ``%LOCALAPPDATA%\\Fit-Agent``——appauthor=False 保证不多出厂商或版本子目录，
    数据文件继承操作系统用户目录 ACL（10.2，不自建跨平台权限系统）。

    显式 override 与环境变量 ``FIT_AGENT_DATA_DIR`` 仅用于测试与 Windows 人工验收的
    临时数据隔离，不改变生产默认解析；优先级 override > 环境变量 > 默认。
    """
    if override is not None:
        return Path(override)
    env = os.environ.get(DATA_DIR_OVERRIDE_ENV)
    if env:
        return Path(env)
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def database_path(data_dir: Path) -> Path:
    """数据目录内的唯一数据库文件（10.2：Windows 为 ``%LOCALAPPDATA%\\Fit-Agent\\app.db``）。"""
    return data_dir / DATABASE_FILENAME


def frontend_dist_dir() -> Path:
    """前端生产构建产物目录（10.1）：仓库内 ``frontend/dist``，由 FastAPI 静态托管。

    只做位置解析，不判断是否存在（未构建／未打包时由托管层按缺失处理，不假装已交付前端）；
    构建产物随项目一起发布，运行时不需要 Node.js（10.1）。
    """
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


# ---------------------------------------------------------------------------
# Harness 本地配置（S4-05a；08 8.5、08「Stage 4 已拍 Harness 策略」）
# ---------------------------------------------------------------------------

#: 可配置项的已拍硬边界（闭区间）与默认值：``{配置键: (下界, 上界, 默认值)}``。
#: 正本见 08 参数表；越界一律拒绝，不钳制。整数项与秒数项分别走下面的两个校验函数。
HARNESS_BOUNDS: Mapping[str, tuple[float, float, float]] = {
    "max_model_requests": (1, 24, 20),
    "max_tool_calls": (1, 40, 32),
    "max_output_tokens": (512, 8192, 8192),
    "run_timeout_seconds": (30, 600, 300),
    "request_timeout_seconds": (10, 180, 120),
    "connect_timeout_seconds": (1, 30, 10),
    "effective_input_tokens": (32_768, 524_288, 250_000),
}

#: 触发点与保留目标按有效输入上限派生，不单独配置（08「容量、估算与溢出」）。
COMPRESSION_TRIGGER_PERCENT = 80
RETAINED_HISTORY_PERCENT = 10

#: 摘要请求的输出上限（08 已拍值）。
SUMMARY_OUTPUT_TOKENS = 6144

#: 摘要请求自身的提示词/结构余量：08 只给出不变量公式，未给该数值，取 2048——
#: 已拍下限 32,768 仍满足「摘要请求上界 ≤ 有效输入上限」，且余量不随配置放宽。
SUMMARY_PROMPT_MARGIN_TOKENS = 2048

#: 安全余量 = max(4096, 10% × 估算)（08 3.2A）。
SAFETY_MARGIN_FLOOR_TOKENS = 4096
SAFETY_MARGIN_PERCENT = 10


class HarnessConfigError(ValueError):
    """Harness 配置非法：越界、未知键/模型、类型不符或交叉不变量不成立（拒绝启动）。"""


@dataclass(frozen=True)
class HarnessConfig:
    """加载并逐项校验后的源配置（尚未做容量派生与交叉校验）。

    默认模型 id 是 Stage 6 目录项（owner 指定：``qwen3.6-flash`` 因 3.7 403 quota
    不可用而入目录并设为默认）：生产端点已改拍为 OpenAI 兼容端点，**不再默认
    DeepSeek 官方 URL**（10 章 2026-09-13 补充）；``deepseek-flash`` 与 ``qwen3.7-flash``
    仍在目录里可选（历史端点与测试），但不作为默认值。
    """

    model_id: str = QWEN36_FLASH.model_id
    max_model_requests: int = 20
    max_tool_calls: int = 32
    max_output_tokens: int = 8192
    run_timeout_seconds: float = 300.0
    request_timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 10.0
    effective_input_tokens: int = 250_000


def harness_config_path(data_dir: Path) -> Path:
    """Harness 配置文件位置：数据目录下的 ``harness.toml``（可缺省，缺省即默认值）。"""
    return data_dir / HARNESS_CONFIG_FILENAME


def load_harness_config(data_dir: Path) -> HarnessConfig:
    """读取本地 Harness 配置并做硬边界校验；非法值抛 :class:`HarnessConfigError`。

    文件不存在视为全部默认值；文件存在时**严格**解析：未知键、非数字、整数项给小数、
    越界值一律拒绝（不忽略、不猜测、不钳制到边界）。模型 id 的目录校验在
    :func:`effective_harness_config`（需要模型窗口做交叉校验）。
    """
    path = harness_config_path(data_dir)
    document: Mapping[str, object] = {}
    if path.exists():
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise HarnessConfigError(f"Harness 配置无法解析（{path}）: {exc}") from exc
    allowed = {"model", *HARNESS_BOUNDS}
    unknown = set(document) - allowed
    if unknown:
        raise HarnessConfigError(
            f"Harness 配置含未知项 {sorted(unknown)}（{path}）；只允许 {sorted(allowed)}"
        )
    model_id = document.get("model", QWEN36_FLASH.model_id)
    if not isinstance(model_id, str):
        raise HarnessConfigError(
            f"Harness 配置项 model 必须是字符串，实际为 {type(model_id).__name__}"
        )
    return HarnessConfig(
        model_id=model_id,
        max_model_requests=_harness_int(
            "max_model_requests", _raw(document, "max_model_requests")
        ),
        max_tool_calls=_harness_int("max_tool_calls", _raw(document, "max_tool_calls")),
        max_output_tokens=_harness_int(
            "max_output_tokens", _raw(document, "max_output_tokens")
        ),
        run_timeout_seconds=_harness_seconds(
            "run_timeout_seconds", _raw(document, "run_timeout_seconds")
        ),
        request_timeout_seconds=_harness_seconds(
            "request_timeout_seconds", _raw(document, "request_timeout_seconds")
        ),
        connect_timeout_seconds=_harness_seconds(
            "connect_timeout_seconds", _raw(document, "connect_timeout_seconds")
        ),
        effective_input_tokens=_harness_int(
            "effective_input_tokens", _raw(document, "effective_input_tokens")
        ),
    )


def _raw(document: Mapping[str, object], key: str) -> object:
    """配置原值；文件缺该键时用已拍默认值（缺省即默认，不是错误）。"""
    return document.get(key, HARNESS_BOUNDS[key][2])


def _harness_int(key: str, value: object) -> int:
    """整数项校验：非整数（含 bool、小数）或越界抛 :class:`HarnessConfigError`。"""
    low, high, _default = HARNESS_BOUNDS[key]
    # bool 是 int 的子类，必须显式拒绝，不能当成 0/1
    if isinstance(value, bool) or not isinstance(value, int):
        raise HarnessConfigError(f"Harness 配置项 {key} 必须是整数，实际为 {value!r}")
    if not low <= value <= high:
        raise HarnessConfigError(
            f"Harness 配置项 {key}={value!r} 越过已拍硬边界 [{low}, {high}]：拒绝启动，不钳制"
        )
    return value


def _harness_seconds(key: str, value: object) -> float:
    """秒数项校验：非数字或越界抛 :class:`HarnessConfigError`；接受整数秒与小数秒。"""
    low, high, _default = HARNESS_BOUNDS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessConfigError(
            f"Harness 配置项 {key} 必须是数字，实际为 {type(value).__name__}"
        )
    if not low <= value <= high:
        raise HarnessConfigError(
            f"Harness 配置项 {key}={value!r} 越过已拍硬边界 [{low}, {high}]：拒绝启动，不钳制"
        )
    return float(value)


@dataclass(frozen=True)
class EffectiveHarness:
    """一次 Run 冻结后的有效 Harness 配置（08 8.5）：派生完成的不可变标量快照。

    构造后字段不可改；运行期间修改配置文件（或重建工厂函数）都不会影响已冻结实例，
    只会影响之后才开始的 Run。S4-05b（预算/重试/超时）与 S4-06（压缩/投影）共用本对象。
    """

    spec: ModelSpec
    max_model_requests: int
    max_tool_calls: int
    max_output_tokens: int
    run_timeout_seconds: float
    request_timeout_seconds: float
    connect_timeout_seconds: float
    context_window: int
    effective_input_tokens: int
    compression_trigger_tokens: int
    retained_history_tokens: int
    summary_output_tokens: int

    @property
    def output_reserve_tokens(self) -> int:
        """普通请求输出预留 = 单次输出上限（08「容量、估算与溢出」）。"""
        return self.max_output_tokens

    def safety_margin_tokens(self, estimate_tokens: int) -> int:
        """安全余量 = max(4,096, 10% × 估算)（08 3.2A；上取整，取保守侧）。"""
        return max(
            SAFETY_MARGIN_FLOOR_TOKENS,
            math.ceil(estimate_tokens * SAFETY_MARGIN_PERCENT / 100),
        )

    def request_timeout_bounded(self, run_remaining_seconds: float) -> float:
        """单次模型请求总时限受 Run 剩余时间约束（08 参数表）。"""
        return min(self.request_timeout_seconds, float(run_remaining_seconds))


def effective_harness_config(config: HarnessConfig) -> EffectiveHarness:
    """派生容量值并做启动交叉校验；任何不成立都抛 :class:`HarnessConfigError`。

    模型 id 必须来自目录（未知 id 拒绝，不推断别名）；输出上限不得超过模型能力；
    容量不变量见 :func:`require_capacity_invariants`。
    """
    try:
        spec = require_supported_model_id(config.model_id)
    except UnknownModelId as exc:
        raise HarnessConfigError(str(exc)) from exc
    if config.max_output_tokens > spec.max_output_tokens:
        raise HarnessConfigError(
            f"单次输出上限 {config.max_output_tokens} 超过模型 {spec.model_id} 的能力"
            f"（{spec.max_output_tokens}）"
        )
    cap = config.effective_input_tokens
    effective = EffectiveHarness(
        spec=spec,
        max_model_requests=config.max_model_requests,
        max_tool_calls=config.max_tool_calls,
        max_output_tokens=config.max_output_tokens,
        run_timeout_seconds=config.run_timeout_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
        connect_timeout_seconds=config.connect_timeout_seconds,
        context_window=spec.context_window,
        effective_input_tokens=cap,
        compression_trigger_tokens=cap * COMPRESSION_TRIGGER_PERCENT // 100,
        retained_history_tokens=cap * RETAINED_HISTORY_PERCENT // 100,
        summary_output_tokens=SUMMARY_OUTPUT_TOKENS,
    )
    require_capacity_invariants(effective)
    return effective


def freeze_effective_harness(data_dir: Path) -> EffectiveHarness:
    """启动校验与每次 Run 开始共用的冻结入口（08 8.5）。

    每次调用重新读文件并派生：返回的不可变实例与调用方共享，之后文件变化不会改变它。
    非法配置在这里拒绝对外服务（启动）或拒绝 Run 开始（运行期编辑），不静默沿用旧值。
    """
    return effective_harness_config(load_harness_config(data_dir))


def require_capacity_invariants(effective: EffectiveHarness) -> None:
    """启动交叉校验（08「容量、估算与溢出」）；不成立拒绝启动，不静默修正。

    1. 保留目标 < 压缩触发点 ≤ 有效输入上限；
    2. 摘要请求上界（触发点 − 保留目标 + 旧摘要上限 + 提示词余量）≤ 有效输入上限；
    3. 有效输入上限 + 输出预留 + 安全余量 ≤ 模型窗口（安全余量按估算上界取，即上限本身）。
    """
    if not (
        effective.retained_history_tokens
        < effective.compression_trigger_tokens
        <= effective.effective_input_tokens
    ):
        raise HarnessConfigError(
            "容量不变量不成立：要求 保留目标（"
            f"{effective.retained_history_tokens}）< 触发点（{effective.compression_trigger_tokens}）"
            f"≤ 有效输入上限（{effective.effective_input_tokens}）"
        )
    summary_request_bound = (
        effective.compression_trigger_tokens
        - effective.retained_history_tokens
        + effective.summary_output_tokens
        + SUMMARY_PROMPT_MARGIN_TOKENS
    )
    if summary_request_bound > effective.effective_input_tokens:
        raise HarnessConfigError(
            f"容量不变量不成立：摘要请求上界 {summary_request_bound} 超过有效输入上限"
            f"（{effective.effective_input_tokens}）"
        )
    margin = effective.safety_margin_tokens(effective.effective_input_tokens)
    window_bound = (
        effective.effective_input_tokens + effective.output_reserve_tokens + margin
    )
    if window_bound > effective.context_window:
        raise HarnessConfigError(
            f"容量不变量不成立：有效输入上限 + 输出预留 + 安全余量 = {window_bound} "
            f"超过模型窗口（{effective.context_window}）"
        )


def local_timezone_name() -> str:
    """生产默认的本机时区采样来源（07 7.3：首次启动取本机时区并固定保存）。

    tzlocal（S0-05 拍板新增依赖）跨平台返回与系统时区匹配的 IANA 地区名：
    Linux/macOS 读系统时区配置；Windows 将注册表时区 ID 映射为 zoneinfo 兼容
    地区名，解析全年地区规则所需的 IANA 数据由 tzdata 提供。

    检测失败由 tzlocal 异常大声上抛；采样结果再用 ZoneInfo 复验可解析，
    失败同样上抛——不得静默选 UTC 或固定偏移替代地区规则（07 7.3）。
    """
    name = tzlocal.get_localzone_name()
    if not name:
        raise RuntimeError(
            "本机时区采样失败：tzlocal 未返回地区名，不降级为 UTC 或固定偏移"
        )
    ZoneInfo(name)  # 可解析性复验：Windows 依赖 tzdata；解析失败不持久化、不降级
    return name
