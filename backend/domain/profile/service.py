"""profile 用例编排：正式档案读取、补丁校验与纯内存应用（正本 architecture/02）。

边界（stage1.md §5 S1-04）：

- 本服务不接 HTTP／Agent／CLI，也不提供建档旁路：正式档案写入只有
  :meth:`ProfileService.write_profile_in_transaction`，且必须在 Stage 2 确认事务内调用。
- :meth:`ProfileService.preview_patch` 只做纯内存计算：读正式档案 → 应用补丁 → 返回
  拟议条件，不写库、不推进 ``context_version``、不修改输入对象。
- 引用校验复用 S1-03 契约：具体动作引用经 :class:`ActionCatalogService` 按稳定身份读取
  （停用动作仍可引用），动作模式引用经 13 项已拍词表校验。限制是否命中动作由 S1-05 判定。
- :meth:`ProfileService.check_candidate_actions_safety` 只读：读正式档案后按补丁后条件做
  本地确定性安全校验（``domain.profile.safety``），不写档案、不增删永久限制、不解除红旗。
"""

from collections.abc import Sequence

from domain.actions.service import ActionCatalogService
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    UnknownExerciseReference,
    apply_patch,
    validate_patch,
    validate_profile_structure,
    validate_session_conditions,
)
from domain.profile.safety import SafetyCheckResult, evaluate_safety
from domain.profile.schema import (
    Profile,
    ProfilePatch,
    ProfileSnapshot,
    SessionConditions,
)
from storage.db import Database


class ProfileService:
    """档案读取、拟议补丁校验与纯内存应用（供后续 Stage 2 确认事务复用）。"""

    def __init__(self, db: Database, catalog: ActionCatalogService | None = None):
        self._repo = ProfileRepo(db)
        self._catalog = catalog if catalog is not None else ActionCatalogService(db)

    async def read_formal_profile(self) -> ProfileSnapshot:
        """读取正式档案与统一业务版本同一快照；未建档时 ``profile`` 为 None。"""
        return await self._repo.read()

    async def validate_patch(self, patch: ProfilePatch) -> None:
        """校验拟议补丁：结构 + 限制引用（具体动作身份存在、模式在已拍词表内）。

        当次条件（``SessionConditions``）不是长期补丁，在触碰数据库前即被拒绝（02 2.4）。
        """
        validate_patch(patch)
        for restriction in patch.add_restrictions + patch.remove_restrictions:
            if restriction.scope != "specific_action":
                continue
            if await self._catalog.get_by_id(restriction.target) is None:
                raise UnknownExerciseReference(
                    f"限制引用的动作身份不在目录内：{restriction.target}"
                )

    async def preview_patch(self, patch: ProfilePatch) -> Profile:
        """按拟议补丁计算补丁后条件（纯内存）；正式档案与版本保持原样。

        未建档时以全未知档案为基线，只返回计算结果，不落库（02 2.4）。
        """
        await self.validate_patch(patch)
        snapshot = await self._repo.read()
        base = snapshot.profile if snapshot.profile is not None else Profile.empty()
        return apply_patch(base, patch)

    async def check_candidate_actions_safety(
        self,
        candidate_exercise_ids: Sequence[str],
        *,
        patch: ProfilePatch | None = None,
        session: SessionConditions | None = None,
    ) -> SafetyCheckResult:
        """对候选动作集合做安全校验：正式档案 + 补丁后条件 + 当次条件（S1-05）。

        只读路径：读取正式档案与 ``context_version`` 同一快照，按拟议补丁后的条件计算
        限制命中（01 1.4），红旗按正式／拟议／当次三来源独立评估。未建档时以全未知档案
        为基线，因此返回「未收集/需澄清」而不是默认无限制、无红旗。
        候选动作身份必须存在于目录（含停用动作）；本方法不写档案、不改永久限制、不解除红旗。
        """
        if patch is not None:
            await self.validate_patch(patch)
        if session is not None:
            validate_session_conditions(session)
        snapshot = await self._repo.read()
        profile = snapshot.profile if snapshot.profile is not None else Profile.empty()
        actions = []
        for exercise_id in candidate_exercise_ids:
            exercise = await self._catalog.get_by_id(exercise_id)
            if exercise is None:
                raise UnknownExerciseReference(f"候选动作身份不在目录内：{exercise_id}")
            actions.append(exercise)
        return evaluate_safety(profile, tuple(actions), patch=patch, session=session)

    async def write_profile_in_transaction(self, conn, profile: Profile) -> None:
        """在**外层事务**内写入正式档案：不提交、不推进版本（归 Stage 2 确认事务）。

        写入前做结构校验；缺失必填事实的档案可以保存（建档是逐步补全的过程），
        但不得被当作完整档案（``ensure_complete_profile`` 会拒绝）。
        """
        validate_profile_structure(profile)
        await self._repo.write_in_transaction(conn, profile)
