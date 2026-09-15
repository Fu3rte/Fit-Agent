"""profile 用例编排：画像读取与整份更新（Stage 1 子任务 02 §5；REFACTOR_PLAN §6.1 普通业务接口）。

边界：不接 HTTP／Agent／CLI，不提供字段级补丁入口——更新是整份覆盖（PUT），写入前做结构校验
（``domain.profile.rules``）与禁用动作 ID 的存在性校验（经 ``domain.actions`` 按稳定身份读取）。
不推进任何业务版本：``context_version`` 机制已删除（讨论总结 §8）。
"""

from domain.actions.service import ActionCatalogService
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    UnknownExerciseReference,
    validate_profile_structure,
)
from domain.profile.schema import Profile
from storage.db import Database


class ProfileService:
    """单用户画像读取与整份更新。"""

    def __init__(self, db: Database):
        self._repo = ProfileRepo(db)
        self._catalog = ActionCatalogService(db)

    async def read(self) -> Profile | None:
        """读取画像；未建档（``profile_json`` 为 NULL）时返回 None。"""
        return await self._repo.read()

    async def update(self, profile: Profile) -> None:
        """整份覆盖写入画像（PUT）：七字段三态事实一次写入，未填写用 unknown 表达。

        写入前校验结构（含每周训练次数 1–7）与禁用动作 ID 的目录存在性；任一项不通过即整体
        拒绝，不落盘半份画像、不补造缺失字段。
        """
        validate_profile_structure(profile)
        await self._require_forbidden_exercises_exist(profile)
        await self._repo.write(profile)

    async def _require_forbidden_exercises_exist(self, profile: Profile) -> None:
        """禁用动作必须引用有效稳定 ``exercise_id``；未填写或明确为空时无引用可校验。"""
        fact = profile.forbidden_exercise_ids
        if not fact.is_known or fact.value is None:
            return
        for exercise_id in fact.value:
            if await self._catalog.get_by_id(exercise_id) is None:
                raise UnknownExerciseReference(
                    f"禁用动作引用的身份不在目录内：{exercise_id}"
                )
