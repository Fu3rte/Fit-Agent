"""actions 用例编排：目录读取与写入训练记录前的口径复验。"""

from domain.actions.repo import ExerciseRepo
from domain.actions.rules import UnknownExercise, validate_record_against_exercise
from domain.actions.schema import Exercise, LoadConvention, RecordType
from storage.db import Database


class ActionCatalogService:
    """动作目录读取与写入前校验（repo + 规则的组合点）。"""

    def __init__(self, db: Database):
        self._repo = ExerciseRepo(db)

    async def validate_record_write(
        self,
        exercise_id: str,
        *,
        record_type: RecordType,
        load_convention: LoadConvention | None,
    ) -> Exercise:
        """写入训练记录前的复验：动作存在且记录口径／负重口径与目录一致。"""
        exercise = await self._repo.get_by_id(exercise_id)
        if exercise is None:
            raise UnknownExercise(f"动作身份不在目录内：{exercise_id}")
        validate_record_against_exercise(
            exercise, record_type=record_type, load_convention=load_convention
        )
        return exercise
