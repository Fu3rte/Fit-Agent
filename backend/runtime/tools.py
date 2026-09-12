"""业务工具适配（stage4.md S4-04；README 硬规则 2、全局不变量 7）。

工具面只有两类能力，没有第三类：

- **只读查询**：复用 Stage 3 应用层读取入口（:class:`~app.plan_reads.PlanReadService`、
  :class:`~domain.records.service.RecordReadService`、:class:`~domain.stats.service.StatsService`）；
  计划指导一律经 ``PlanReadService`` 复核，``usable=false`` 时不投影可执行处方。
- **提出 Pending 草稿**：复用 Stage 3 草稿创建服务（档案／计划／记录／安排四族）。校验与事务
  语义原样复用（服务内部完成），本层只做形状解码与把领域拒绝翻成「需向用户追问」的工具结果；
  草稿落盘前不写正式事实、不推进 ``context_version``（由应用层服务保证）。

没有确认／丢弃／作废、没有直写正式事实、没有公开建草稿入口：正式写入只能由用户经业务接口
触发（PLAN.md 系统边界；旁路扫描见 ``tests/test_stage4_agent_wiring.py``）。工具也不改
Harness 配置、不扩目录、不做文件访问。

取消语义（08 8.3）：每个工具在执行任何读取或写入之前先看执行名额的取消标记；取消后抛
:class:`asyncio.CancelledError`，不启动后续模型／工具尝试，也不落新草稿。
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from app.arrangement_drafts import (
    ArrangementDraftService,
    ArrangementDraftView,
    ArrangementPreparation,
)
from app.drafts import DraftService, profile_patch
from app.plan_drafts import PlanDraftService, require_long_term_revision
from app.plan_reads import PlanReadService
from app.record_drafts import RecordDraftService
from domain.plan.rules import ArrangementAdjustment
from domain.plan.schema import ARRANGEMENT_ITEM_DISPOSITIONS, IntRange
from domain.plan.service import (
    PlanGenerationBlocked,
    generate_ppl_plan,
    revise_plan_payload,
)
from domain.profile.rules import missing_first_time_fields
from domain.profile.schema import profile_from_json
from domain.records.schema import (
    RECORD_DRAFT_SCHEMA_VERSION,
    record_draft_from_json,
    record_draft_to_json,
)
from domain.records.service import RecordReadService
from domain.stats.service import StatsService
from runtime.context import pending_proposed_profile, plan_guidance_facts
from storage.db import Database
from storage.errors import InvalidInput

#: 领域与应用层把「输入不合法／缺事实」统一表达为 ``ValueError``（含各 ``Invalid*``）与
#: ``InvalidInput``；只有这两类翻成「需向用户追问」，存储与未知异常照旧上报（不吞）。
_REJECTED_INPUT = (ValueError, InvalidInput)

_DRAFT_NOTE = (
    "草稿已保存为 pending；用户在草稿卡确认前正式事实与 context_version 不变。"
)
_ASK_USER_NOTE = (
    "输入不合法或缺事实：向用户说明并追问，不要自行补齐、猜测或重复提交同一内容。"
)


def _load_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("业务 JSON 无法解析") from exc


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    """一次 Run 的工具身份：会话与 Run 来源（草稿必须记录来源）、业务日期、取消探针。

    ``message_red_flags`` 是当前 Run 最新用户消息命中的 C 层兜底红旗词（2026-09-12 拍板）：
    非空时本 Run 的安全复核强制不可用、也不生成任何处方草稿，只做精确子串文本兜底。
    """

    conversation_id: str
    run_id: str
    business_date: date
    cancel_requested: Callable[[], bool] | None = None
    message_red_flags: tuple[str, ...] = ()


class BusinessTools:
    """一次 Run 的业务工具面；每个方法就是注册给模型的一个工具。"""

    def __init__(self, db: Database, identity: ToolIdentity) -> None:
        self._db = db
        self._identity = identity
        self._plans = PlanReadService(db)
        self._records = RecordReadService(db)
        self._stats = StatsService(db)
        self._profile_drafts = DraftService(db)
        self._plan_drafts = PlanDraftService(db)
        self._record_drafts = RecordDraftService(db)
        self._arrangement_drafts = ArrangementDraftService(db)

    # ---------- 只读查询 ----------

    async def read_plan_guidance(self) -> dict[str, Any]:
        """读取当前正式计划与整份计划安全复核（以最新限制与红旗判定）。

        返回计划行字段、日程与 ``safety``；``safety.usable=false`` 时不含可执行处方，
        此时只能说明阻断原因，不得给出训练处方。会话内**待确认档案草稿**的拟议条件
        （含限制与红旗）与当前 Run 最新用户消息命中的 C 层兜底红旗词一并计入复核
        （fail-closed）：两者任一生效期间同样不放可执行处方。
        """
        self._require_active()
        guidance = await self._plans.read_current_plan_guidance(
            business_date=self._identity.business_date,
            safety_profile=await pending_proposed_profile(
                self._db, conversation_id=self._identity.conversation_id
            ),
            message_red_flags=self._identity.message_red_flags,
        )
        if guidance is None:
            return {
                "plan": None,
                "safety": None,
                "note": "尚无正式计划：不得凭空造计划。",
            }
        return plan_guidance_facts(guidance)

    async def read_training_records(self) -> dict[str, Any]:
        """读取全部训练身份与各自当前修订事实（作废／待补全状态如实给出，不重算统计）。"""
        self._require_active()
        views = await self._records.list_records()
        return {
            "records": [
                {
                    "id": view.session.id,
                    "created_at": view.session.created_at,
                    "revision": (
                        None
                        if view.session.current is None
                        else {
                            "id": view.session.current.id,
                            "revision_no": view.session.current.revision_no,
                            "status": view.session.current.status,
                            "occurred_on": view.session.current.occurred_on.isoformat(),
                        }
                    ),
                    "record": _load_json(record_draft_to_json(view.payload)),
                }
                for view in views
            ]
        }

    async def read_week_completion(
        self, plan_version_id: str, week_no: int
    ) -> dict[str, Any]:
        """读取某计划版本第 ``week_no`` 个计划周（W1 起）的完成率。

        没有已到期应训练次数时返回 ``completion: null``（「暂无」），不是 0%。
        """
        self._require_active()
        completion = await self._stats.weekly_completion(
            plan_version_id,
            week_no,
            business_date=self._identity.business_date,
        )
        if completion is None:
            return {
                "week_no": week_no,
                "completion": None,
                "note": "该周暂无应训练次数。",
            }
        return {
            "week_no": completion.week_no,
            "completion": {
                "plan_version_id": completion.plan_version_id,
                "week_start": completion.week_start.isoformat(),
                "week_end": completion.week_end.isoformat(),
                "planned": completion.denominator,
                "completed": completion.numerator,
                "rate": (
                    None
                    if completion.denominator == 0
                    else completion.numerator / completion.denominator
                ),
            },
        }

    async def read_personal_record(
        self, exercise_id: str, load_notation: str, load_kg_key: int | None = None
    ) -> dict[str, Any]:
        """读取现算 PR：给 ``load_kg_key``（kg×1000）时是该重量下单组最高次数，否则最高重量。

        无候选返回对应字段 ``null``（不是 0）；数值一律来自确定性统计，不得改写。
        """
        self._require_active()
        if load_kg_key is None:
            return {
                "exercise_id": exercise_id,
                "load_notation": load_notation,
                "load_kg_key": None,
                "max_load_kg_key": await self._stats.pr_max_load(
                    exercise_id=exercise_id, load_notation=load_notation
                ),
                "best_reps": None,
            }
        return {
            "exercise_id": exercise_id,
            "load_notation": load_notation,
            "load_kg_key": load_kg_key,
            "max_load_kg_key": None,
            "best_reps": await self._stats.pr_max_reps_at_load(
                exercise_id=exercise_id,
                load_notation=load_notation,
                load_kg_key=load_kg_key,
            ),
        }

    # ---------- 提出 Pending 草稿（四族） ----------

    async def propose_profile_draft(self, proposed: dict[str, Any]) -> dict[str, Any]:
        """提出一条**待用户确认**的档案草稿。

        ``proposed`` 是八字段全量的三态对象（与档案读取同一形状，``{state, value}``）。
        缺字段、状态非法或限制引用不合法时不落库，返回需追问的结果。
        """
        self._require_active()
        try:
            profile = profile_from_json(json.dumps(proposed, ensure_ascii=False))
            baseline = await self._profile_drafts.prepare_generation_baseline()
            view = await self._profile_drafts.create_profile_draft(
                draft_id=_new_draft_id(),
                generation_baseline=baseline,
                conversation_id=self._identity.conversation_id,
                run_id=self._identity.run_id,
                proposed=profile,
            )
        except _REJECTED_INPUT as exc:
            return _ask_user("profile_update", exc)
        return _created(view)

    async def propose_plan_draft(
        self,
        starts_on: str,
        review_on: str,
        anchor_date: str | None = None,
        long_term_adjustment: bool = False,
        proposed_profile: dict[str, Any] | None = None,
        adjustments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """提出一条**待用户确认**的计划草稿（D9 处方由确定性生成器产出，不由模型手写）。

        路由（已拍，自然语言只决定生成哪条 Pending 草稿；正式生效仍靠用户点击确认）：

        - **首次建档**（当前无正式计划）：按用户给的生效区间直接生成计划草稿。
        - **长期调整**：当前已有正式计划时，必须把 ``long_term_adjustment=True`` 显式传入——
          只有用户**明确要求**调整长期计划才能生成；未传时本工具不落库，返回需追问的结果
          （先问 «只记录» 还是 «也调整长期计划»）。用户回绝则不调用本工具。
        - **只记录 + 同时调整长期计划**：同一调用里再传 ``proposed_profile``（与
          ``propose_profile_draft`` 同一三态形状）→ 生成受限组合计划草稿：档案补丁与计划
          一并待确认，两项 Diff 都在草稿里，一次确认、只推进一次 ``context_version``。
        - **档案已确认、用户随后要求调整**：不传 ``proposed_profile``，生成独立计划草稿。
        - **当次安排不算长期同意**：``propose_arrangement_draft`` 只改当次目标，不能当成本
          工具的授权。

        长期调整的确定性边界（04 4.5）：生效日固定为**草稿生成业务日期的次日**（生成时确定
        并展示，确认时不再改；确认日等于或晚于它都允许），保留原复核节点、日历循环与训练日
        结构，且拟议 payload 必须真的与当前基线不同——原样续期不当调整，不生成草稿。
        ``adjustments`` 是长期修订的逐项处置（与 ``propose_arrangement_draft`` 同一形状：
        ``item_key`` + 处置 + 减载参数／替代身份）：修订以**当前正式 payload 为基线**，只改
        列出的条目，未列出的条目、循环与训练日集合保持原样；档案补丁不能代替计划本身的真实
        变化。首次建档不使用 ``adjustments``（没有可保留的基线）。
        档案缺事实、红旗阻断或不满足排程时不落库；当前 Run 消息命中 C 层兜底红旗词时同样
        不落库（2026-09-12 拍板：本 Run 不给任何处方草稿）。
        """
        self._require_active()
        if self._identity.message_red_flags:
            return _message_red_flag_block("plan", self._identity.message_red_flags)
        try:
            start = _iso_date("starts_on", starts_on)
            review = _iso_date("review_on", review_on)
            anchor = (
                start if anchor_date is None else _iso_date("anchor_date", anchor_date)
            )
            preparation = await self._plan_drafts.prepare_generation_input()
            current = preparation.current_plan
            patch = None
            if proposed_profile is not None:
                proposed = profile_from_json(
                    json.dumps(proposed_profile, ensure_ascii=False)
                )
                patch = profile_patch(preparation.snapshot.profile, proposed)
            if current is None:
                if adjustments:
                    return _ask_user(
                        "plan",
                        InvalidInput(
                            "首次建档没有可保留的当前基线：不使用长期修订处置，"
                            "按生成器直接产生首个计划"
                        ),
                    )
                generation = generate_ppl_plan(
                    preparation.snapshot.profile,
                    preparation.candidates,
                    starts_on=start,
                    review_on=review,
                    anchor_date=anchor,
                    patch=patch,
                )
                if isinstance(generation, PlanGenerationBlocked):
                    return _blocked("plan", generation)
                payload = generation.payload
            else:
                if not long_term_adjustment:
                    return _needs_long_term_decision()
                # 档案缺事实不给处方：长期修订不因「计划已存在」放宽这条 fail-closed 边界。
                profile = preparation.snapshot.profile
                if profile is None:
                    return _blocked(
                        "plan",
                        PlanGenerationBlocked(
                            code="no_profile",
                            reason="尚未建立正式档案：不生成计划处方",
                        ),
                    )
                missing = missing_first_time_fields(profile)
                if missing:
                    return _blocked(
                        "plan",
                        PlanGenerationBlocked(
                            code="incomplete_profile",
                            reason=f"档案缺少明确回答的事实，不给处方：{list(missing)}",
                            missing_fields=missing,
                        ),
                    )
                expected = self._identity.business_date + timedelta(days=1)
                if start != expected:
                    return _ask_user(
                        "plan",
                        InvalidInput(
                            "长期调整的生效日固定为草稿生成业务日期的次日"
                            f"（{expected.isoformat()}）：收到 {start.isoformat()}"
                        ),
                    )
                # 受限修订：以当前 payload 为基线，只改列出的条目；不是原样续期，档案补丁
                # 不能代替计划本身的业务变化（决策 7）。
                payload = revise_plan_payload(
                    current.payload,
                    _adjustments(adjustments),
                    catalog={
                        exercise.id: exercise for exercise in preparation.candidates
                    },
                )
                require_long_term_revision(
                    payload=payload, review_on=review, current=current
                )
            view = await self._plan_drafts.create_plan_draft(
                draft_id=_new_draft_id(),
                preparation=preparation,
                conversation_id=self._identity.conversation_id,
                run_id=self._identity.run_id,
                payload=payload,
                starts_on=start,
                review_on=review,
                business_date=self._identity.business_date,
                patch=patch,
            )
        except _REJECTED_INPUT as exc:
            return _ask_user("plan", exc)
        return _created(view)

    async def propose_record_draft(self, record: dict[str, Any]) -> dict[str, Any]:
        """提出一条**待用户确认**的训练记录草稿。

        ``record`` 是记录草稿载荷（与 ``read_training_records`` 的 ``record`` 同一形状）：
        ``occurred_on`` 与 ``exercises`` 必填，``training_session_id`` 无默认值——
        ``null`` 是显式的「新增一次训练」，补充／更正既有训练必须给出稳定身份 id，
        同日多练时本工具不按日期推断归属。
        """
        self._require_active()
        try:
            payload = record_draft_from_json(
                json.dumps(
                    {"schema_version": RECORD_DRAFT_SCHEMA_VERSION, **record},
                    ensure_ascii=False,
                )
            )
            preparation = await self._record_drafts.prepare_input(payload.occurred_on)
            view = await self._record_drafts.create_record_draft(
                draft_id=_new_draft_id(),
                preparation=preparation,
                conversation_id=self._identity.conversation_id,
                run_id=self._identity.run_id,
                training_session_id=payload.training_session_id,
                exercises=payload.exercises,
                arrangement_revision_id=payload.arrangement_revision_id,
                started_at=payload.started_at,
                time_precision=payload.time_precision,
                completion_declared=payload.completion_declared,
                is_return_phase=payload.is_return_phase,
                feedback=payload.feedback,
            )
        except _REJECTED_INPUT as exc:
            return _ask_user("training_record", exc)
        return _created(view)

    async def propose_arrangement_draft(
        self,
        scheduled_session_id: str,
        adjustments: list[dict[str, Any]] | None = None,
        adjustment_reason: str | None = None,
    ) -> dict[str, Any]:
        """提出当天／当次的**待用户确认**安排草稿（按处置调整目标）。

        ``scheduled_session_id`` 必须是当前正式计划里未取消的应训练名额；``adjustments``
        每项形如 ``{"item_key", "disposition"?, "work_sets"?, "target_rir": {"min", "max"}?,
        "reps_range": {"min", "max"}?, "load_value"?, "replacement_exercise_id"?}``；处置取
        保留／减载／同等刺激替换／局部跳过（缺省不标注，沿用保守规则）。减载只减不增，改负荷只
        能是已验证重量的 50–70%，未校准动作只能回落方案 1；替换必须通过 ``modes`` ∩ 主要肌群 ∩
        器械 ∩ 限制 ∩ 启用且可推荐；有差异必须给出非空 ``adjustment_reason``。

        本工具只改对应训练的当次目标，**不是**长期计划调整的同意：不改长期计划、不推进计划
        版本；需要长期调整时走计划草稿路径。

        当次安排同样是处方：当前 Run 消息命中 C 层兜底红旗词时不落库（2026-09-12 拍板）。
        """
        self._require_active()
        if self._identity.message_red_flags:
            return _message_red_flag_block(
                "arrangement", self._identity.message_red_flags
            )
        try:
            decoded = _adjustments(adjustments)
            preparation: ArrangementPreparation = (
                await self._arrangement_drafts.prepare_input()
            )
            view: ArrangementDraftView = (
                await self._arrangement_drafts.create_arrangement_draft(
                    draft_id=_new_draft_id(),
                    preparation=preparation,
                    conversation_id=self._identity.conversation_id,
                    run_id=self._identity.run_id,
                    scheduled_session_id=scheduled_session_id,
                    adjustments=decoded,
                    adjustment_reason=adjustment_reason,
                )
            )
        except _REJECTED_INPUT as exc:
            return _ask_user("arrangement", exc)
        return _created(view)

    # ---------- 取消 ----------
    def _require_active(self) -> None:
        """取消后不启动新的读取或写入（08 8.3），底层执行由取消标记与驱动双重把关。"""
        if (
            self._identity.cancel_requested is not None
            and self._identity.cancel_requested()
        ):
            raise asyncio.CancelledError("取消后不启动新的工具调用")


def _new_draft_id() -> str:
    """草稿身份：与正式事实身份同一实现（``uuid4().hex``），由服务端生成，不接受模型指定。"""
    return uuid4().hex


def _iso_date(name: str, raw: str) -> date:
    """ISO 日期文本 → ``date``；形状不符按输入不合法处理（422 同口径）。"""
    if not isinstance(raw, str):
        raise InvalidInput(f"{name} 必须是 ISO 日期文本：{raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidInput(f"{name} 不是 ISO 日期：{raw!r}") from exc


def _adjustments(
    raw: Sequence[dict[str, Any]] | None,
) -> tuple[ArrangementAdjustment, ...]:
    """工具载荷 → 当次调整结构；形状不符即拒绝，不改写、不补默认。

    每项形如 ``{"item_key", "disposition"?, "work_sets"?, "target_rir"?,
    "reps_range"?, "load_value"?, "replacement_exercise_id"?}``；处置缺省不标注（沿用保守
    规则），四类处置与参数边界由 ``domain/plan/rules`` 逐项复核。
    """
    decoded: list[ArrangementAdjustment] = []
    for item in raw or ():
        if not isinstance(item, dict):
            raise InvalidInput(f"调整条目必须是对象：{item!r}")
        item_key = item.get("item_key")
        if not isinstance(item_key, str) or not item_key:
            raise InvalidInput(f"调整条目缺 item_key：{item!r}")
        work_sets = item.get("work_sets")
        if work_sets is not None and not isinstance(work_sets, int):
            raise InvalidInput(f"work_sets 必须是整数：{work_sets!r}")
        target_rir = item.get("target_rir")
        if target_rir is not None and not (
            isinstance(target_rir, dict)
            and isinstance(target_rir.get("min"), int)
            and isinstance(target_rir.get("max"), int)
        ):
            raise InvalidInput(f"target_rir 必须是 {{min, max}}：{target_rir!r}")
        disposition = item.get("disposition")
        if disposition is not None and disposition not in ARRANGEMENT_ITEM_DISPOSITIONS:
            raise InvalidInput(f"处置不在已拍四类内：{disposition!r}")
        reps_range = item.get("reps_range")
        if reps_range is not None and not (
            isinstance(reps_range, dict)
            and isinstance(reps_range.get("min"), int)
            and isinstance(reps_range.get("max"), int)
        ):
            raise InvalidInput(f"reps_range 必须是 {{min, max}}：{reps_range!r}")
        load_value = item.get("load_value")
        replacement_id = item.get("replacement_exercise_id")
        if replacement_id is not None and not isinstance(replacement_id, str):
            raise InvalidInput(
                f"replacement_exercise_id 必须是文本：{replacement_id!r}"
            )
        decoded.append(
            ArrangementAdjustment(
                item_key=item_key,
                work_sets=work_sets,
                target_rir=(
                    None
                    if target_rir is None
                    else IntRange(min=target_rir["min"], max=target_rir["max"])
                ),
                disposition=disposition,
                reps_range=(
                    None
                    if reps_range is None
                    else IntRange(min=reps_range["min"], max=reps_range["max"])
                ),
                load_value=load_value,
                replacement_exercise_id=replacement_id,
            )
        )
    return tuple(decoded)


def _created(view: Any) -> dict[str, Any]:
    """草稿创建成功 → 工具结果：身份 + 修订 + 状态（确认前不生效）。

    计划草稿额外给出两项 Diff 的字段名（受限组合草稿同时有档案补丁与计划 Diff）：模型只
    描述草稿里已有的对比，不得自行编造变更。
    """
    draft = view.draft
    result: dict[str, Any] = {
        "created": True,
        "draft_id": draft.id,
        "kind": draft.kind,
        "revision": draft.revision,
        "status": draft.status,
        "note": _DRAFT_NOTE,
    }
    plan_diff = getattr(view, "plan_diff", None)
    if plan_diff is not None:
        result["plan_fields_changed"] = [
            item.field for item in plan_diff if item.changed
        ]
    profile_diff = getattr(view, "profile_diff", None)
    if profile_diff:
        result["profile_fields_changed"] = [
            item.field for item in profile_diff if item.changed
        ]
    return result


def _needs_long_term_decision() -> dict[str, Any]:
    """当前已有正式计划但未获用户明确选择 → 不落库，先追问（04 4.5、stage4.md S4-04）。

    自然语言只决定生成哪条 Pending 草稿（只记录 / 同时调整长期计划）；当次安排不算长期同意。
    """
    return {
        "created": False,
        "kind": "plan",
        "needs_user_input": True,
        "reason": "当前已有正式计划：先问用户只要记录身体状况，还是也要调整长期计划",
        "note": (
            "未获用户明确要求调整长期计划前不生成计划草稿；用户回绝则本次不生成计划草稿。"
            "当次安排（propose_arrangement_draft）只改对应训练，不构成长期调整的同意。"
        ),
    }


def _ask_user(kind: str, exc: Exception) -> dict[str, Any]:
    """领域／应用层拒绝 → 工具结果：不落库，把原样原因交给模型向用户追问（08 8.6）。"""
    return {
        "created": False,
        "kind": kind,
        "needs_user_input": True,
        "reason": str(exc),
        "note": _ASK_USER_NOTE,
    }


def _blocked(kind: str, blocked: PlanGenerationBlocked) -> dict[str, Any]:
    """生成阻断 → 工具结果：不给任何处方，只说明原因（fail-closed，08 8.6）。"""
    return {
        "created": False,
        "kind": kind,
        "needs_user_input": True,
        "block": {
            "code": blocked.code,
            "reason": blocked.reason,
            "missing_fields": list(blocked.missing_fields),
            "red_flags": list(blocked.red_flags),
        },
        "note": "生成被阻断：不得给出替代处方或凭空补齐事实，只说明原因并按需追问。",
    }


def _message_red_flag_block(kind: str, hits: tuple[str, ...]) -> dict[str, Any]:
    """当前 Run 消息命中 C 层兜底红旗词 → 不落库、不给处方（2026-09-12 拍板）。

    复用既有安全阻断结果形状（``_blocked``）；命中词已在 :class:`ToolIdentity` 上扫好，
    本层不重扫文本、不并入档案事实。
    """
    return _blocked(
        kind,
        PlanGenerationBlocked(
            code="red_flag",
            reason="当前消息命中安全兜底红旗词，不给结构化处方：" + "、".join(hits),
            red_flags=hits,
        ),
    )
