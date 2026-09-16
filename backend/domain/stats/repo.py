"""stats 业务表手写 SQL：统一「有效工作组」查询与趋势／月历的只读输入
（讨论总结 §7.1、REFACTOR_PLAN §6.2／§6.4、stage2.md §6.1／§7／§8）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）；本层只读——PB 与趋势都不落表，
也不建数据库 View。``_VALID_WORK_SETS_SQL`` 是有效工作组过滤口径的**唯一一份**实现：热身组、
辅助（借力）组与不完整组的排除只在这里写一次，三类 PB 与力量趋势共用它，不在各统计项里各写
一套过滤条件（REFACTOR_PLAN §6.2）。身体指标与训练事实窗口查询只服务趋势摘要与月历，均按业务
日期参数读取，不在本层取「今天」。SQL 一律以字面量书写（storage/README 硬规则 3）。

过滤口径（讨论总结 §7.1：只有这些组算「有效工作组」）：

- 训练组行仍存在（JOIN ``workout_sessions``；一次训练物理删除即其组随外键级联消失）；
- ``set_type='work'``：``warmup`` 与 ``assisted`` 不入选；
- ``reps_weight``（外加重量）：重量与次数完整且次数不少于 1；
- ``reps_bodyweight``（纯自重）：次数完整且不少于 1；
- ``time``（计时）：``duration_seconds`` 完整且大于 0。

排序固定为 ``performed_on, workout_session_id, set_no``：并列 PB 的来源排序（见
``domain.stats.service``）依靠它可复算。
"""

from datetime import date

import aiosqlite

from domain.body_metrics.schema import BodyMetric
from domain.stats.schema import ValidWorkSet, WorkoutFact
from storage.db import Database

#: 统一有效工作组查询：列清单与三类过滤口径的唯一出处（PB 与趋势共用）。
_VALID_WORK_SETS_SQL = """
SELECT ws.exercise_id,
       e.standard_name_zh,
       e.record_type,
       ws.load_convention,
       ws.weight_kg,
       ws.reps,
       ws.duration_seconds,
       ws.workout_session_id,
       ws.set_no,
       sess.performed_on
  FROM workout_sets AS ws
  JOIN workout_sessions AS sess ON sess.id = ws.workout_session_id
  JOIN exercises AS e ON e.id = ws.exercise_id
 WHERE ws.set_type = 'work'
   AND (
           (e.record_type = 'reps_weight'
               AND ws.weight_kg IS NOT NULL AND ws.reps IS NOT NULL AND ws.reps >= 1)
        OR (e.record_type = 'reps_bodyweight'
               AND ws.reps IS NOT NULL AND ws.reps >= 1)
        OR (e.record_type = 'time'
               AND ws.duration_seconds IS NOT NULL AND ws.duration_seconds > 0)
       )
 ORDER BY sess.performed_on, ws.workout_session_id, ws.set_no
"""


#: 身体指标列清单与 ``domain.body_metrics`` 的行类型一一对应（复用该行类型解码，不新建影子类型）。
_SELECT_METRIC = "SELECT id, measured_on, weight_kg, body_fat_pct FROM body_metrics"

#: 最近两条体重记录（最新在前）：趋势摘要只比最近两条，不必把全部历史读进内存。
_SELECT_LATEST_TWO_WEIGHTS = _SELECT_METRIC + " ORDER BY measured_on DESC, id DESC LIMIT 2"

#: 最近两条非空体脂记录（最新在前）：未记录体脂的行不参与变化计算。
_SELECT_LATEST_TWO_BODY_FATS = (
    _SELECT_METRIC
    + " WHERE body_fat_pct IS NOT NULL ORDER BY measured_on DESC, id DESC LIMIT 2"
)

#: 训练事实的最小列清单（停训天数与月历共用）。
_SELECT_WORKOUT_FACT = "SELECT id, performed_on, plan_session_id FROM workout_sessions"

#: 最近一次训练的发生日期：停训天数用注入的业务日期减去它。
_SELECT_LAST_WORKOUT_ON = (
    "SELECT performed_on FROM workout_sessions"
    " ORDER BY performed_on DESC, id DESC LIMIT 1"
)

#: 月历窗口内的全部训练事实（含额外训练）；``performed_on`` 是 ``YYYY-MM-DD`` 文本，字典序即日期序。
_SELECT_WORKOUTS_BETWEEN = (
    _SELECT_WORKOUT_FACT
    + " WHERE performed_on >= ? AND performed_on <= ? ORDER BY performed_on, id"
)

#: 关联了计划日程的训练（不限月份）：日程完成状态跨日、跨月都按它现算。
_SELECT_LINKED_WORKOUTS = (
    _SELECT_WORKOUT_FACT + " WHERE plan_session_id IS NOT NULL ORDER BY performed_on, id"
)


async def _read_valid_work_sets(conn: aiosqlite.Connection) -> tuple[ValidWorkSet, ...]:
    async with conn.execute(_VALID_WORK_SETS_SQL) as cursor:
        rows = await cursor.fetchall()
    return tuple(ValidWorkSet.from_row(dict(row)) for row in rows)


async def _read_metrics(
    conn: aiosqlite.Connection, sql: str, params: tuple[object, ...] = ()
) -> tuple[BodyMetric, ...]:
    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return tuple(BodyMetric.from_row(dict(row)) for row in rows)


async def _read_workouts(
    conn: aiosqlite.Connection, sql: str, params: tuple[object, ...] = ()
) -> tuple[WorkoutFact, ...]:
    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return tuple(WorkoutFact.from_row(dict(row)) for row in rows)


async def _read_last_workout_on(conn: aiosqlite.Connection) -> date | None:
    async with conn.execute(_SELECT_LAST_WORKOUT_ON) as cursor:
        row = await cursor.fetchone()
    return None if row is None else date.fromisoformat(str(row["performed_on"]))


class StatsRepo:
    """统计只读查询：有效工作组、趋势输入（身体指标／训练事实）与月历事实。"""

    def __init__(self, db: Database):
        self._db = db

    async def list_valid_work_sets(self) -> tuple[ValidWorkSet, ...]:
        """全部有效工作组（按发生日期、训练身份、组序号排序）。"""
        return await self._db.under_lock(_read_valid_work_sets)

    async def list_body_metrics_between(
        self, from_on: date, to_on: date
    ) -> tuple[BodyMetric, ...]:
        """窗口内的全部身体指标（按发生日期、身份排序）；体脂未记录保持 None。"""
        return await self._db.under_lock(
            lambda conn: _read_metrics(
                conn,
                _SELECT_METRIC
                + " WHERE measured_on >= ? AND measured_on <= ?"
                " ORDER BY measured_on, id",
                (from_on.isoformat(), to_on.isoformat()),
            )
        )

    async def read_latest_two_weights(self) -> tuple[BodyMetric, ...]:
        """最近两条体重记录（最新在前）；不足两条就返回已有条数。"""
        return await self._db.under_lock(
            lambda conn: _read_metrics(conn, _SELECT_LATEST_TWO_WEIGHTS)
        )

    async def read_latest_two_body_fats(self) -> tuple[BodyMetric, ...]:
        """最近两条非空体脂记录（最新在前）；未记录体脂的行不参与。"""
        return await self._db.under_lock(
            lambda conn: _read_metrics(conn, _SELECT_LATEST_TWO_BODY_FATS)
        )

    async def read_last_workout_on(self) -> date | None:
        """最近一次训练的发生日期；没有任何训练即 None。"""
        return await self._db.under_lock(_read_last_workout_on)

    async def list_workouts_between(
        self, from_on: date, to_on: date
    ) -> tuple[WorkoutFact, ...]:
        """窗口内的全部实际训练（含额外训练，按发生日期、身份排序）。"""
        return await self._db.under_lock(
            lambda conn: _read_workouts(
                conn,
                _SELECT_WORKOUTS_BETWEEN,
                (from_on.isoformat(), to_on.isoformat()),
            )
        )

    async def list_linked_workouts(self) -> tuple[WorkoutFact, ...]:
        """全部关联了计划日程的训练（日程完成状态跨日、跨月都按它现算）。"""
        return await self._db.under_lock(
            lambda conn: _read_workouts(conn, _SELECT_LINKED_WORKOUTS)
        )
