"""测试公共基类与最小业务样例构造器。

对齐 test-plan.md §1 的最小业务样例：
计划原定卧推 3 组 → 训练前确认减为 2 组 → 实际完成 2 组 → 更正其中一组
→ 本周还有一次训练未打卡。

日期一律以固定业务日历为起点（FIXED_TODAY 派生），不依赖测试当天；
时钟可推进以模拟“服务停机跨过训练日”。
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import TypeVar

from storage.store import BusinessStore

ROOT = Path(__file__).resolve().parent.parent

T = TypeVar("T")

# 固定业务日历起点：任何一天跑测试结果一致，不依赖真实“今天”。
FIXED_TODAY = date(2026, 9, 7)


class TestClock:
    """可冻结、可推进的业务日期时钟。"""

    def __init__(self) -> None:
        self._value = FIXED_TODAY

    def __call__(self) -> date:
        return self._value

    def reset(self) -> None:
        self._value = FIXED_TODAY

    def advance(self, days: int = 1) -> None:
        self._value += timedelta(days=days)

    @property
    def value(self) -> date:
        return self._value


# 每个测试类共享同一时钟；asyncSetUp 会 reset，确保用例互不影响。
CLOCK = TestClock()


def must(x: T | None) -> T:
    """断言查询结果存在并收窄 Optional（测试中对象必须存在，否则测试失败）。"""
    if x is None:
        raise AssertionError("expected non-null value")
    return x


def today() -> date:
    """测试用业务日期：读共享冻结时钟（不依赖测试当天）。"""
    return CLOCK.value


def days(n: int) -> str:
    return (CLOCK.value + timedelta(days=n)).isoformat()


def bench_sets(n: int, start_no: int = 1, load: dict | None = None):
    """构建 n 组卧推目标组（external_load_reps，barbell_total）。"""
    load = load or {"value": "60", "unit": "kg"}
    return [
        {
            "set_type": "work",
            "target_set_key": f"push-bench-{i}",
            "load": load,
            "reps": 8,
            "rir": 2,
            "assistance": "none",
        }
        for i in range(start_no, start_no + n)
    ]


def plan_payload(
    plan_id: str, *, offsets=(0, 3), starts_in: int = -7, work_sets: int = 3
):
    """一周两次 push，目标卧推 work_sets 组。starts_in 表示相对 today 的开始日偏移。"""
    return {
        "plan_id": plan_id,
        "starts_on": days(starts_in),
        "payload": {
            "template_key": "ppl",
            "days": [
                {
                    "day_key": "push",
                    "weekday_offsets": list(offsets),
                    "exercises": [
                        {
                            "item_key": "push-bench",
                            "exercise_id": "bench_barbell_flat",
                            "exercise_name": "平板杠铃卧推",
                            "record_type": "external_load_reps",
                            "load_notation": "barbell_total",
                            "sets": [
                                {
                                    "set_key": f"push-bench-{i}",
                                    "reps_range": [8, 10],
                                    "rir_range": [1, 3],
                                    "load": {"value": "60", "unit": "kg"},
                                }
                                for i in range(1, work_sets + 1)
                            ],
                        }
                    ],
                }
            ],
        },
        "weeks": 1,
    }


def record_payload(
    session_id: str,
    occurred_on: str,
    *,
    arrangement_revision_id=None,
    sets=None,
    completion_declared=True,
    status="valid",
    is_return_phase=False,
    assistance="none",
    load=None,
    reps=8,
    rir=2,
    session_id_suffix="",
):
    """一条训练记录草稿。sets 未提供时用 1 组工作组的默认事实。"""
    sets = (
        sets
        if sets is not None
        else [
            {
                "set_type": "work",
                "target_set_key": "push-bench-1",
                "load": load or {"value": "60", "unit": "kg"},
                "reps": reps,
                "rir": rir,
                "assistance": assistance,
            }
        ]
    )
    return {
        "session_id": f"{session_id}{session_id_suffix}",
        "occurred_on": occurred_on,
        "arrangement_revision_id": arrangement_revision_id,
        "completion_declared": completion_declared,
        "status": status,
        "is_return_phase": is_return_phase,
        "exercises": [
            {
                "item_key": "push-bench",
                "exercise_id": "bench_barbell_flat",
                "exercise_name": "平板杠铃卧推",
                "record_type": "external_load_reps",
                "load_notation": "barbell_total",
                "sets": sets,
            }
        ],
    }


def correct_payload(
    session_id: str, sets, occurred_on: str, completion_declared: bool = True
) -> dict:
    """更正草稿：完整替换本次训练的动作/组事实（完整修订，非字段差量）。"""
    return {
        "session_id": session_id,
        "occurred_on": occurred_on,
        "completion_declared": completion_declared,
        "status": "valid",
        "exercises": [
            {
                "item_key": "push-bench",
                "exercise_id": "bench_barbell_flat",
                "record_type": "external_load_reps",
                "load_notation": "barbell_total",
                "sets": sets,
            }
        ],
    }


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    """每个用例独立临时 SQLite 文件，并使用共享冻结时钟。"""

    async def asyncSetUp(self):
        CLOCK.reset()  # 每个用例回到固定起点，避免用例间互相污染
        self._tmp = tempfile.TemporaryDirectory(prefix="biz-table-")
        self.db_path = str(Path(self._tmp.name) / "app.db")
        self.store = BusinessStore(self.db_path, today_fn=CLOCK)
        await self.store.open()

    async def asyncTearDown(self):
        if getattr(self, "store", None) is not None:
            await self.store.close()
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    async def reopen(self):
        """关闭后重新打开同一文件（模拟服务重启）。"""
        await self.store.close()
        self.store = BusinessStore(self.db_path, today_fn=CLOCK)
        await self.store.open()
