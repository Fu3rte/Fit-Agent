"""S4-05a：Harness 本地配置、硬边界、容量派生与 Run 期冻结（stage4.md S4-05；08 8.5）。

覆盖：
1. 文件缺省 = 已拍默认值；边界闭区间接受、越界拒绝（表驱动，逐项 low/high 与 low−1/high+1）；
2. 未知键、未知模型、类型不符（字符串／bool／整数项给小数）、TOML 语法错误一律拒绝，不钳制；
3. 派生容量（触发点 80%、保留目标 10%、摘要输出 6144、安全余量 max(4096, 10%)）与
   启动交叉不变量（保留<触发≤上限；摘要请求上界≤上限；上限+输出预留+余量≤窗口）；
4. 每次 Run 开始冻结的不可变有效配置：之后改文件（或重建）不影响已冻结实例；
5. 启动装配：非法配置拒绝启动，合法（缺省）配置冻结进 ``app.state.harness_config``。

离线确定性：只读写 pytest ``tmp_path`` 下的 ``harness.toml``，不碰真实用户数据目录、
不读凭据、不构造 Provider、不发任何请求。
"""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from api.app import create_app
from config import (
    HARNESS_BOUNDS,
    HARNESS_CONFIG_FILENAME,
    EffectiveHarness,
    HarnessConfig,
    HarnessConfigError,
    effective_harness_config,
    freeze_effective_harness,
    harness_config_path,
    load_harness_config,
    require_capacity_invariants,
)
from runtime.models import QWEN36_FLASH

BOUND_KEYS = sorted(HARNESS_BOUNDS)


def _write_config(data_dir: Path, body: str) -> None:
    (data_dir / HARNESS_CONFIG_FILENAME).write_text(body, encoding="utf-8")


def _effective(**overrides: object) -> EffectiveHarness:
    """默认有效配置的副本，便于直接构造违反交叉不变量的组合。"""
    return replace(effective_harness_config(HarnessConfig()), **overrides)


def test_defaults_when_config_file_absent(tmp_path: Path) -> None:
    assert harness_config_path(tmp_path) == tmp_path / HARNESS_CONFIG_FILENAME
    assert load_harness_config(tmp_path) == HarnessConfig()

    effective = effective_harness_config(load_harness_config(tmp_path))
    # 默认模型是 Stage 6 目录项（owner 指定 qwen3.6-flash；不再默认 DeepSeek 官方 URL）
    assert effective.spec is QWEN36_FLASH
    assert effective.context_window == 1_000_000
    assert effective.max_model_requests == 20
    assert effective.max_tool_calls == 32
    assert effective.max_output_tokens == 8192
    assert effective.run_timeout_seconds == 300.0
    assert effective.request_timeout_seconds == 120.0
    assert effective.connect_timeout_seconds == 10.0
    assert effective.effective_input_tokens == 250_000
    assert effective.compression_trigger_tokens == 200_000
    assert effective.retained_history_tokens == 25_000
    assert effective.summary_output_tokens == 6144
    assert effective.output_reserve_tokens == 8192


def test_safety_margin_and_request_timeout_bounds(tmp_path: Path) -> None:
    effective = freeze_effective_harness(tmp_path)
    assert effective.safety_margin_tokens(1000) == 4096  # 地板值
    assert effective.safety_margin_tokens(250_000) == 25_000  # 10% 取上整
    assert effective.safety_margin_tokens(45_001) == 4501
    # 单次请求总时限受 Run 剩余时间约束（08 参数表）
    assert effective.request_timeout_bounded(600.0) == 120.0
    assert effective.request_timeout_bounded(45.0) == 45.0


def test_partial_file_keeps_defaults_for_missing_keys(tmp_path: Path) -> None:
    _write_config(tmp_path, "max_model_requests = 5\n")
    config = load_harness_config(tmp_path)
    assert config.max_model_requests == 5
    assert config.model_id == QWEN36_FLASH.model_id
    assert config.max_tool_calls == 32
    assert config.effective_input_tokens == 250_000


@pytest.mark.parametrize("key", BOUND_KEYS)
def test_bound_edges_accept(tmp_path: Path, key: str) -> None:
    """闭区间两端都接受，且边界值仍须通过容量交叉校验。"""
    low, high, _default = HARNESS_BOUNDS[key]
    for value in (low, high):
        _write_config(tmp_path, f"{key} = {value}\n")
        config = load_harness_config(tmp_path)
        assert getattr(config, key) == value
        effective = effective_harness_config(config)
        if key == "effective_input_tokens":
            assert effective.effective_input_tokens == value
            assert effective.compression_trigger_tokens == int(value) * 80 // 100
            assert effective.retained_history_tokens == int(value) * 10 // 100


@pytest.mark.parametrize("key", BOUND_KEYS)
def test_out_of_range_rejected_never_clamped(tmp_path: Path, key: str) -> None:
    low, high, _default = HARNESS_BOUNDS[key]
    for value in (low - 1, high + 1):
        _write_config(tmp_path, f"{key} = {value}\n")
        with pytest.raises(HarnessConfigError, match="硬边界"):
            load_harness_config(tmp_path)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "max_model_requests = 20\nretries = 1\n")
    with pytest.raises(HarnessConfigError, match="未知项"):
        load_harness_config(tmp_path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('max_model_requests = "20"\n', "必须是整数"),
        ("max_model_requests = 20.0\n", "必须是整数"),
        ("max_model_requests = true\n", "必须是整数"),
        ("max_tool_calls = [32]\n", "必须是整数"),
        ("effective_input_tokens = 250000.0\n", "必须是整数"),
        ('run_timeout_seconds = "300"\n', "必须是数字"),
        ("request_timeout_seconds = true\n", "必须是数字"),
        ("model = 7\n", "必须是字符串"),
    ],
)
def test_type_mismatch_rejected(tmp_path: Path, body: str, message: str) -> None:
    _write_config(tmp_path, body)
    with pytest.raises(HarnessConfigError, match=message):
        load_harness_config(tmp_path)


def test_toml_syntax_error_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "max_model_requests = \n")
    with pytest.raises(HarnessConfigError, match="无法解析"):
        load_harness_config(tmp_path)


def test_fractional_seconds_accepted(tmp_path: Path) -> None:
    _write_config(
        tmp_path, "run_timeout_seconds = 30.5\nconnect_timeout_seconds = 1.5\n"
    )
    config = load_harness_config(tmp_path)
    assert config.run_timeout_seconds == 30.5
    assert config.connect_timeout_seconds == 1.5
    assert effective_harness_config(config).run_timeout_seconds == 30.5


@pytest.mark.parametrize(
    "model_id",
    ["deepseek-chat", "deepseek-v4-flash", "deepseek-flash-latest", "gpt-5", ""],
)
def test_unknown_model_rejected(tmp_path: Path, model_id: str) -> None:
    _write_config(tmp_path, f'model = "{model_id}"\n')
    config = load_harness_config(tmp_path)  # 字符串类型本身合法
    with pytest.raises(HarnessConfigError, match="不支持的模型 id"):
        effective_harness_config(config)
    with pytest.raises(HarnessConfigError):
        freeze_effective_harness(tmp_path)


def test_output_limit_beyond_model_capability_rejected() -> None:
    """边界表只到 8192，但模型能力上限仍是独立交叉校验（不靠配置值不可达来兜底）。"""
    with pytest.raises(HarnessConfigError, match="超过模型"):
        effective_harness_config(HarnessConfig(max_output_tokens=384_001))


@pytest.mark.parametrize(
    "overrides",
    [
        {"retained_history_tokens": 200_000},  # 保留目标 ≥ 触发点
        {"compression_trigger_tokens": 300_000},  # 触发点 > 有效输入上限
        {"summary_output_tokens": 100_000},  # 摘要请求上界 > 有效输入上限
        {"context_window": 280_000},  # 上限 + 输出预留 + 安全余量 > 窗口
    ],
)
def test_capacity_invariant_violations_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(HarnessConfigError, match="容量不变量"):
        require_capacity_invariants(_effective(**overrides))


def test_default_effective_config_satisfies_capacity_invariants() -> None:
    assert require_capacity_invariants(_effective()) is None


def test_run_start_freeze_is_immune_to_later_file_changes(tmp_path: Path) -> None:
    """Run 开始冻结的有效配置是标量快照：之后改文件只影响之后才开始的 Run（08 8.5）。"""
    first = freeze_effective_harness(tmp_path)
    _write_config(
        tmp_path,
        "run_timeout_seconds = 600\nrequest_timeout_seconds = 180\nmax_output_tokens = 1024\n",
    )
    second = freeze_effective_harness(tmp_path)
    assert second is not first
    assert second.run_timeout_seconds == 600.0
    assert second.request_timeout_seconds == 180.0
    assert second.max_output_tokens == 1024
    # 已冻结实例保持原值，不受文件变化影响
    assert first.run_timeout_seconds == 300.0
    assert first.request_timeout_seconds == 120.0
    assert first.max_output_tokens == 8192
    with pytest.raises(FrozenInstanceError):
        first.run_timeout_seconds = 1.0  # type: ignore[misc]


async def test_startup_freezes_valid_config_into_app_state(tmp_path: Path) -> None:
    app = create_app(tmp_path / "data")
    async with app.router.lifespan_context(app):
        assert app.state.harness_config == effective_harness_config(HarnessConfig())


async def test_startup_rejects_invalid_config(tmp_path: Path) -> None:
    """越界配置（此处 24 上限 +1）拒绝启动，不静默用默认值或钳制后的值服务。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_config(data_dir, "max_model_requests = 25\n")
    app = create_app(data_dir)
    with pytest.raises(HarnessConfigError, match="硬边界"):
        async with app.router.lifespan_context(app):
            pass
    # 非法配置下不设置有效配置，也不留下任何已打开资源
    assert not hasattr(app.state, "harness_config")
