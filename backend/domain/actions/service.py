"""actions 用例编排：目录读取与写入训练记录前的口径复验（Stage 1 子任务 02 §4）。

边界：这里只提供目录查询与写入前校验，不实现打卡匹配、不写目录、不写档案。记录写入本身归
``domain.records``（03）：它在落库前调用 :meth:`ActionCatalogService.validate_record_write`
拿到目录动作并确认口径一致，避免把「动作是否存在、口径是否匹配」两件事各写一遍。
"""

from domain.actions.repo import ExerciseRepo
from domain.actions.rules import UnknownExercise, validate_record_against_exercise
from domain.actions.schema import Exercise, LoadConvention, RecordType
from storage.db import Database


class ActionCatalogService:
    """动作目录读取与写入前校验（repo + 规则的组合点）。"""

    def __init__(self, db: Database):
        self._repo = ExerciseRepo(db)

    async def list_all(self) -> tuple[Exercise, ...]:
        """目录全量（按稳定身份排序）。"""
        return await self._repo.list_all()

    async def get_by_id(self, exercise_id: str) -> Exercise | None:
        """按稳定身份读取；不存在即 None。"""
        return await self._repo.get_by_id(exercise_id)

    async def validate_record_write(
        self,
        exercise_id: str,
        *,
        record_type: RecordType,
        load_convention: LoadConvention | None,
    ) -> Exercise:
        """写入训练记录前的复验：动作存在且记录口径／负重口径与目录一致。

        返回目录动作供调用方使用；动作不存在抛 :class:`UnknownExercise`，口径不符抛
        :class:`RecordLoadMismatch`（库内 CHECK 与 ``workout_sets`` 外键只是兜底，不是唯一防线）。
        """
        exercise = await self._repo.get_by_id(exercise_id)
        if exercise is None:
            raise UnknownExercise(f"动作身份不在目录内：{exercise_id}")
        validate_record_against_exercise(
            exercise, record_type=record_type, load_convention=load_convention
        )
        return exercise
