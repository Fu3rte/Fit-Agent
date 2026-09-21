"""profile 用例编排：画像读取与整份更新。"""

from app.application.ports import ExerciseCatalog, Profiles
from app.domain.profile.rules import (
    UnknownExerciseReference,
    validate_profile_structure,
)
from app.domain.profile.schema import Profile


class ProfileService:
    """单用户画像读取与整份更新。"""

    def __init__(self, profiles: Profiles, exercises: ExerciseCatalog) -> None:
        self._profiles = profiles
        self._exercises = exercises

    async def read(self) -> Profile | None:
        """读取画像；未建档（``profile_json`` 为 NULL）时返回 None。"""
        return await self._profiles.read()

    async def update(self, profile: Profile) -> None:
        """整份覆盖写入画像（PUT）：七字段三态事实一次写入，未填写用 unknown 表达。"""
        validate_profile_structure(profile)
        await self._require_forbidden_exercises_exist(profile)
        await self._profiles.write(profile)

    async def _require_forbidden_exercises_exist(self, profile: Profile) -> None:
        """禁用动作必须引用有效稳定 ``exercise_id``；未填写或明确为空时无引用可校验。"""
        fact = profile.forbidden_exercise_ids
        if not fact.is_known or fact.value is None:
            return
        for exercise_id in fact.value:
            if await self._exercises.get_by_id(exercise_id) is None:
                raise UnknownExerciseReference(
                    f"禁用动作引用的身份不在目录内：{exercise_id}"
                )
