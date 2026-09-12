"""业务 API 传输层：JSON ↔ 应用层载体的 DTO 映射与统一错误形状（S2-07）。

边界（stage2.md §5 S2-07）：本模块只做**传输**——请求体形状校验、领域载荷的双向映射、
应用层结果／错误到响应体与 HTTP 状态码的映射。领域规则（结构、限制引用、首次建档完整性）、
SQL 与事务编排都不在这里：路由调用应用层（``app/drafts.py``／``app/confirm.py``），本模块把
它们的结果与异常翻译成传输形状。

三条硬边界：

- **三态不折叠**（S2-01 §2）：档案事实一律以 ``{"state": "unknown|denied|known", "value": ...}``
  表达；unknown（未收集）与 denied（明确无）都不是空数组／默认值，也不互相代替；限制用稳定
  身份（``scope``＋``target``）表达，不用展示名，扩展空集合与 denied 也保持可分。
- **客户端载荷不是可信最终事实**：纠错载荷只做形状映射，值是否符合字段类型与领域规则由应用层
  的领域校验拒绝；身份、来源、生成基线、状态与提交凭据在请求体里根本不存在（未知字段即拒绝），
  不会被当作可替换内容。
- **错误形状统一且不泄漏**：前端 ``ApiError`` 形状 ``{"http_status", "error_code", "message",
  "detail"?}``；``error_code`` 只用前端契约已登记的值（``invalid_request``／``draft_stale``／
  ``draft_modified``），不新增业务语义；``message`` 只取业务异常文本，不含 SQL、路径或堆栈。
  未登记的异常不映射（例如库内草稿行损坏 ``InvalidDraftRow`` 属服务端故障，保持 500，
  不伪装成客户端错误）。
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.arrangement_drafts import ArrangementDraftView
from app.confirm import (
    ArrangementCommitResult,
    DraftDiscarded,
    DraftStale,
    NoBusinessChange,
    PlanCommitResult,
    ProfileCommitResult,
    RecordCommitResult,
)
from app.drafts import (
    DraftKindMismatch,
    DraftNotCorrectable,
    DraftNotDiscardable,
    DraftRevisionConflict,
    DraftView,
    ProfileFieldDiff,
    UnknownDraft,
)
from app.plan_drafts import PlanDraftView
from app.plan_reads import (
    PlanGuidance,
    PlanScheduleView,
    ScheduledSessionView,
)
from app.record_drafts import RecordDraftView
from app.review_store import ReviewView
from domain.plan.rules import InvalidArrangementTarget, InvalidPlanPayload
from domain.plan.schema import (
    InvalidPlanRow,
    PlanPayload,
    arrangement_target_to_json,
    payload_from_json,
    payload_to_json,
)
from domain.plan.service import PlanSafetyRecheck
from domain.profile.rules import (
    IncompleteProfile,
    InvalidProfile,
    UnknownExerciseReference,
)
from domain.profile.schema import (
    FACT_FIELDS,
    FACT_STATES,
    FACT_VALUE_KINDS,
    ActionRestriction,
    Fact,
    FactState,
    Profile,
    ProfileSnapshot,
    RestrictionScope,
)
from domain.records.rules import InvalidRecordFact
from domain.records.schema import (
    InvalidRecordRow,
    RecordDraftPayload,
    record_draft_from_json,
    record_draft_to_json,
)
from domain.records.service import TrainingRecordView
from domain.stats.schema import (
    TargetJudgement,
    WeekCompletion,
    review_basis_to_json,
)
from runtime.error_codes import CONVERSATION_BUSY
from storage.errors import ConversationBusy

_STALE_DETAIL_NO_FIELD_CHANGE = "业务版本已变化，当前快照无字段差异"


class InvalidRequestShape(ValueError):
    """请求体不是接口约定的 JSON 形状：在触碰应用层之前即拒绝（400 ``invalid_request``）。"""


class UnknownResource(ValueError):
    """按身份读取的只读资源不存在（计划版本／记录／复盘）：404 ``invalid_request``。

    与 :class:`~app.drafts.UnknownDraft` 同口径（明确未找到，不创建资源、不伪造空结果），
    不新增前端契约之外的 ``error_code``。
    """


def _reject_json_constant(literal: str) -> object:
    """``json.loads`` 的 ``parse_constant`` 回调：拒绝非标准 JSON 常量。

    Python 的 JSON 解析器默认接受 ``NaN``／``Infinity``／``-Infinity``，但 JSON 语法没有这些
    字面量；一旦进来，非有限数值既无法可靠落盘（SQLite ``json_valid`` 拒绝）也无法回传
    （Starlette 响应编码拒绝非有限浮点），因此在这里就当非法 JSON 拒绝。
    """
    raise InvalidRequestShape(f"请求体含非标准 JSON 常量：{literal}")


# ---------- 应用层 → 响应体 ----------


def _fact_dto(fact: Fact[Any]) -> dict[str, Any]:
    """单个三态事实 → 传输对象：unknown／denied 不带值，known 带值；限制按身份表达。"""
    value = fact.value
    if isinstance(value, tuple):
        value = [
            {"scope": item.scope, "target": item.target}
            if isinstance(item, ActionRestriction)
            else item
            for item in value
        ]
    return {"state": fact.state, "value": value if fact.is_known else None}


def profile_facts_dto(profile: Profile) -> dict[str, Any]:
    """档案 → 八个事实字段的三态对象；未收集与明确无显式表达，不用空数组／默认值填充。"""
    return {name: _fact_dto(getattr(profile, name)) for name in FACT_FIELDS}


def profile_response_dto(snapshot: ProfileSnapshot) -> dict[str, Any]:
    """正式档案读取快照 → ``GET /api/profile`` 响应体。

    ``profile: null`` 表示尚未建档（``profile_json IS NULL``）；已建档（含部分事实）时是八个
    事实字段的三态对象，缺失／未知与明确无都在字段对里显式表达，不显示成完整档案。
    """
    return {
        "context_version": snapshot.context_version,
        "profile": (
            None if snapshot.profile is None else profile_facts_dto(snapshot.profile)
        ),
    }


def _field_diff_dto(item: ProfileFieldDiff) -> dict[str, Any]:
    return {
        "field": item.field,
        "before": _fact_dto(item.before),
        "after": _fact_dto(item.after),
        "changed": item.changed,
    }


def draft_dto(view: DraftView) -> dict[str, Any]:
    """草稿查询形态 → 传输对象：当前内容、revision、状态、结构化 Diff 与已提交结果。

    ``kind`` 取草稿行保存的类型（非传输层常量）；``payload`` 当前只映射档案形状——
    计划／记录／安排载荷的形状映射归 S3-04/S3-08/S3-10，未接入前不得把别的 kind
    静默按档案形状发出。``committed_revision``／``committed_business_version`` 只在
    已提交草稿上有值，就是持久化的提交凭据（后续业务版本变化不改写它）；Diff 由后端
    按基线与拟议快照计算，不接受客户端传入。
    """
    draft = view.draft
    return {
        "id": draft.id,
        "kind": draft.kind,
        "status": draft.status,
        "revision": draft.revision,
        "base_business_version": draft.base_business_version,
        "payload": {"profile": profile_facts_dto(view.proposed_profile)},
        "diff": [_field_diff_dto(item) for item in view.diff],
        "committed_revision": draft.committed_revision,
        "committed_business_version": draft.committed_business_version,
    }


def commit_result_dto(result: ProfileCommitResult) -> dict[str, Any]:
    """确认提交结果 → 响应体：草稿身份、已提交 revision 与该次提交后的业务版本。"""
    return {
        "draft_id": result.draft_id,
        "status": "committed",
        "committed_revision": result.committed_revision,
        "committed_business_version": result.committed_business_version,
    }


def any_commit_result_dto(result: Any) -> dict[str, Any]:
    """按提交结果类型补充各自建立的正式事实身份（计划版本／安排修订／训练修订）。

    公共凭据部分与档案确认同形状；额外字段都是**首次确认**建立的那一条（不取「最新」，
    重复确认与后续业务版本变化返回同一份数据）。
    """
    base = commit_result_dto(result)
    if isinstance(result, PlanCommitResult):
        return {
            **base,
            "plan_version_id": result.plan_version_id,
            "plan_version": result.plan_version,
        }
    if isinstance(result, ArrangementCommitResult):
        return {
            **base,
            "arrangement_revision_id": result.arrangement_revision_id,
            "arrangement_revision_no": result.arrangement_revision_no,
            "scheduled_session_id": result.scheduled_session_id,
            "accepted_at": result.accepted_at,
        }
    if isinstance(result, RecordCommitResult):
        return {
            **base,
            "training_session_id": result.training_session_id,
            "session_revision_id": result.session_revision_id,
            "revision_no": result.revision_no,
            "revision_status": result.status,
        }
    return base


# ---------- 请求体 → 应用层载体 ----------


async def json_object_body(request: Request, *, keys: frozenset[str]) -> dict[str, Any]:
    """解析请求体 JSON 对象：字段集必须恰为 ``keys``，否则拒绝。

    空请求体按空对象处理（丢弃接口不带请求体），因此本函数也用于「不接受任何字段」的端点。
    非 JSON、非对象、未知字段、缺字段都是传输形状错误（400 ``invalid_request``）；``NaN``、
    ``Infinity``、``-Infinity`` 不是合法 JSON（PEP/ECMA JSON 语法不允许），显式拒绝而不是让
    Python 的宽松解析器接受它们（否则非有限数值会一路走到库里，既不能可靠落盘也不能回传）。
    指数溢出（如 ``1e999`` → ``inf``）是合法 JSON 数字，交由领域校验拒绝（见
    ``domain.profile.rules`` 的有限数值要求）。
    """
    raw = (await request.body()).strip()
    if raw:
        try:
            body = json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidRequestShape("请求体不是合法 JSON") from exc
    else:
        body = {}
    if not isinstance(body, dict):
        raise InvalidRequestShape("请求体必须是 JSON 对象")
    unknown = sorted(set(body) - keys)
    if unknown:
        raise InvalidRequestShape(f"请求体含未登记字段：{unknown}")
    missing = sorted(keys - set(body))
    if missing:
        raise InvalidRequestShape(f"请求体缺字段：{missing}")
    return body


def seen_revision_from_dto(body: dict[str, Any]) -> int:
    """所见 revision：>= 1 的整数（布尔不是整数）；类型不符即拒绝。"""
    revision = body["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise InvalidRequestShape(f"revision 必须是 >= 1 的整数：{revision!r}")
    return revision


def proposed_profile_from_dto(payload: object) -> Profile:
    """纠错载荷 ``{"profile": {字段: {state, value}}}`` → 拟议档案（八个字段全量）。

    只做形状映射：``state`` 取值、known 必须带值、列表与限制条目的 JSON 形状在这里校验；
    值是否符合字段类型与领域规则（模式词表、限制引用的动作身份、非空文本）由应用层的领域
    校验拒绝（``InvalidProfile``／``UnknownExerciseReference`` → 422），这里不重复实现。
    """
    if not isinstance(payload, dict) or set(payload) != {"profile"}:
        raise InvalidRequestShape("payload 必须是只含 profile 的 JSON 对象")
    facts = payload["profile"]
    if not isinstance(facts, dict):
        raise InvalidRequestShape("profile 必须是 JSON 对象")
    unknown = sorted(set(facts) - set(FACT_FIELDS))
    if unknown:
        raise InvalidRequestShape(f"profile 含未登记字段：{unknown}")
    missing = sorted(set(FACT_FIELDS) - set(facts))
    if missing:
        raise InvalidRequestShape(f"profile 缺字段：{missing}")
    return Profile(**{name: _fact_from_dto(name, facts[name]) for name in FACT_FIELDS})


def _fact_from_dto(name: str, raw: object) -> Fact[Any]:
    if not isinstance(raw, dict) or set(raw) != {"state", "value"}:
        raise InvalidRequestShape(f"档案字段 {name} 必须是 {{state, value}} 对象")
    state = raw["state"]
    if not isinstance(state, str) or state not in FACT_STATES:
        raise InvalidRequestShape(f"档案字段 {name} 状态非法：{state!r}")
    value = raw["value"]
    if state != "known":
        if value is not None:
            raise InvalidRequestShape(f"档案字段 {name} 在 {state} 状态不得带值")
        return Fact(state=cast(FactState, state))
    if value is None:
        raise InvalidRequestShape(f"档案字段 {name} 的 known 值不能为空")
    return Fact.known(_value_from_dto(name, value))


def _value_from_dto(name: str, value: object) -> object:
    kind = FACT_VALUE_KINDS[name]
    if kind == "text_list":
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise InvalidRequestShape(f"档案字段 {name} 需要文本数组：{value!r}")
        return tuple(value)
    if kind == "restrictions":
        if not isinstance(value, list):
            raise InvalidRequestShape(f"档案字段 {name} 需要限制数组：{value!r}")
        return tuple(_restriction_from_dto(item) for item in value)
    return value


def _restriction_from_dto(raw: object) -> ActionRestriction:
    if not isinstance(raw, dict) or set(raw) != {"scope", "target"}:
        raise InvalidRequestShape(f"限制条目必须是 {{scope, target}} 对象：{raw!r}")
    scope, target = raw["scope"], raw["target"]
    if not isinstance(scope, str) or not isinstance(target, str):
        raise InvalidRequestShape(f"限制条目 scope／target 必须是文本：{raw!r}")
    return ActionRestriction(scope=cast(RestrictionScope, scope), target=target)


# ---------- 异常 → 统一错误形状 ----------

#: 应用层／领域异常 → (HTTP 状态码, error_code)。404 与 409 的 ``invalid_request`` 表示
#: 「身份未找到」与「草稿状态不允许该操作」，与 revision 冲突（``draft_modified``）、
#: 业务基线冲突（``draft_stale``）分开表达；不新增前端契约之外的 error_code。
#: ``conversation_busy`` 是 Stage 4 已冻结的运行时错误码（``runtime.error_codes``），
#: 语义与状态码只登记一次，不在路由里另写。
_ERROR_STATUS: tuple[tuple[type[Exception], int, str], ...] = (
    (InvalidRequestShape, 400, "invalid_request"),
    (ConversationBusy, 409, CONVERSATION_BUSY),
    (UnknownDraft, 404, "invalid_request"),
    (DraftRevisionConflict, 409, "draft_modified"),
    (DraftStale, 409, "draft_stale"),
    (DraftNotCorrectable, 409, "invalid_request"),
    (DraftNotDiscardable, 409, "invalid_request"),
    (DraftKindMismatch, 409, "invalid_request"),
    (DraftDiscarded, 409, "invalid_request"),
    (NoBusinessChange, 409, "invalid_request"),
    (InvalidProfile, 422, "invalid_request"),
    (IncompleteProfile, 422, "invalid_request"),
    (UnknownExerciseReference, 422, "invalid_request"),
    (UnknownResource, 404, "invalid_request"),
    (InvalidPlanPayload, 422, "invalid_request"),
    (InvalidArrangementTarget, 422, "invalid_request"),
    (InvalidRecordFact, 422, "invalid_request"),
)


def _error_body(exc: Exception, *, status: int, error_code: str) -> dict[str, Any]:
    """异常 → ``ApiError`` 响应体；``draft_stale`` 额外带可核实的字段变化提示。"""
    body: dict[str, Any] = {
        "http_status": status,
        "error_code": error_code,
        "message": str(exc),
    }
    if isinstance(exc, DraftStale):
        changed = ", ".join(item.field for item in exc.changes)
        body["detail"] = (
            f"可核实字段变化：{changed}" if changed else _STALE_DETAIL_NO_FIELD_CHANGE
        )
    return body


def _handler_for(
    status: int, error_code: str
) -> Callable[[Request, Exception], JSONResponse]:
    def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=_error_body(exc, status=status, error_code=error_code),
        )

    return handler


def install_error_handlers(app: FastAPI) -> None:
    """按 :data:`_ERROR_STATUS` 注册异常处理器：全部业务端点共享同一错误形状。"""
    for exc_type, status, error_code in _ERROR_STATUS:
        app.add_exception_handler(exc_type, _handler_for(status, error_code))


# ---------- Stage 3：计划／日程只读投影（04 4.2/4.5；S3-14） ----------


def _jsonable(value: Any) -> Any:
    """领域值 → JSON 可序列化的传输值（dataclass／date／tuple／Mapping 递归）。

    与 ``domain.plan.schema`` 的存储编码同口径：``None`` 字段省略、日期输出 ISO 文本。
    用于 Diff 的 before／after 与没有单独文本契约的结构（安排的计划训练日）。
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
            if getattr(value, item.name) is not None
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _roundtrip_json(text: str) -> Any:
    """存储契约文本 → JSON 传输形状：解析本模块调用的 ``*_to_json`` 刚生成的文本。

    文本来自同一路径的 ``json.dumps``，正常编码不可能产出解析不了的 JSON；真解析失败只能是
    服务端编码器坏了，因此显式翻译为 ``ValueError``——它不在 :data:`_ERROR_STATUS` 里，保持
    500 服务端故障，不伪装成客户端 400（同模块「未登记异常不映射」口径）。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"内部载荷不是合法 JSON：{exc}") from exc


def schedule_dto(item: ScheduledSessionView) -> dict[str, Any]:
    """一条应训练名额 → 传输对象：存储锁定与到期判定分别给出，``effective`` 是拒绝改期的依据。

    ``weekday`` 由 ``scheduled_on`` 派生（1=周一；D9：weekday 不进 payload）；``status`` 取
    ``cancelled``／``locked``／``scheduled`` 三值，取消优先（取消的名额不再是应训练义务）。
    """
    session = item.session
    return {
        "id": session.id,
        "plan_version_id": session.plan_version_id,
        "plan_workout_key": session.plan_workout_key,
        "scheduled_on": session.scheduled_on.isoformat(),
        "weekday": session.scheduled_on.isoweekday(),
        "cancelled": item.cancelled,
        "cancelled_at": session.cancelled_at,
        "locked_at": session.locked_at,
        "lock": {
            "stored": item.lock.stored,
            "by_business_date": item.lock.by_business_date,
            "effective": item.lock.effective,
        },
        "status": (
            "cancelled"
            if item.cancelled
            else ("locked" if item.lock.effective else "scheduled")
        ),
    }


def plan_view_dto(view: PlanScheduleView) -> dict[str, Any]:
    """一个计划版本（当前或历史）→ 传输对象：行字段 + D9 payload + 全部日程（含取消／锁定）。

    payload 直接以 D9 文本契约形状给出（``payload_to_json`` 的 JSON），不另造第二份计划
    结构；``is_current`` 区分当前与历史（历史不重激活，仅可查看）。
    """
    version = view.version
    return {
        "id": version.id,
        "version": version.version,
        "source_plan_version_id": version.source_plan_version_id,
        "starts_on": version.starts_on.isoformat(),
        "review_on": version.review_on.isoformat(),
        "mode": version.mode,
        "is_current": view.is_current,
        "confirmed_at": version.confirmed_at,
        "template_key": version.payload.template_key,
        "plan": _roundtrip_json(payload_to_json(version.payload)),
        "schedules": [schedule_dto(item) for item in view.sessions],
    }


def plan_safety_dto(
    safety: PlanSafetyRecheck, *, context_version: int, reviewed_at: str
) -> dict[str, Any]:
    """整份计划安全复核 → 传输对象（04 4.5；已拍：目录身份读不到给具体阻断）。

    限制冲突与红旗各自独立可读；``unknown_exercise_ids`` 非空时给具体用户可见安全阻断
    ``block_code='plan_action_unavailable'``（不降级为「需澄清」、不当作「无冲突」），且
    ``usable=false``。``clarifications`` 只是需澄清项，不等于安全放行。
    """
    return {
        "context_version": context_version,
        "reviewed_at": reviewed_at,
        "usable": not safety.is_blocked,
        "red_flag_blocked": safety.red_flags.is_blocked,
        "conflicts": [
            {
                "exercise_id": hit.exercise_id,
                "exercise_name": hit.standard_name,
                "restriction": {
                    "scope": hit.restriction.scope,
                    "target": hit.restriction.target,
                },
                "matched_modes": list(hit.matched_modes),
            }
            for hit in safety.restriction_conflicts
        ],
        "action_unavailable": bool(safety.unknown_exercise_ids),
        "block_code": (
            "plan_action_unavailable" if safety.unknown_exercise_ids else None
        ),
        "unknown_exercise_ids": list(safety.unknown_exercise_ids),
        "reasons": list(safety.blocking_reasons),
        "clarifications": list(safety.clarification_reasons),
    }


def guidance_dto(guidance: PlanGuidance, *, reviewed_at: str) -> dict[str, Any]:
    """「基于计划的指导」前置复核 → 传输对象：计划投影 + 安全复核（不给绕过复核的指导）。"""
    return {
        "plan": plan_view_dto(guidance.plan),
        "safety": plan_safety_dto(
            guidance.safety,
            context_version=guidance.context_version,
            reviewed_at=reviewed_at,
        ),
    }


# ---------- Stage 3：记录与统计只读（05／06；S3-14） ----------


def record_dto(view: TrainingRecordView) -> dict[str, Any]:
    """一次训练 → 传输对象：稳定身份 + 当前修订摘要 + 当前修订完整事实（06 只消费当前修订）。"""
    session = view.session
    current = session.current
    return {
        "id": session.id,
        "created_at": session.created_at,
        "revision": (
            None
            if current is None
            else {
                "id": current.id,
                "revision_no": current.revision_no,
                "status": current.status,
                "occurred_on": current.occurred_on.isoformat(),
            }
        ),
        "record": _roundtrip_json(record_draft_to_json(view.payload)),
    }


def target_judgement_dto(judgement: TargetJudgement) -> dict[str, Any]:
    """组级三桶判定 → 传输对象；三桶用契约用语 met／unmet／pending（无对照时全零）。"""
    counts = judgement.counts
    return {
        "session_revision_id": judgement.session_revision_id,
        "has_comparison": judgement.has_comparison,
        "is_return_phase": judgement.is_return_phase,
        "buckets": {
            "met": counts.fit,
            "unmet": counts.unmet,
            "pending": counts.incomplete,
        },
    }


def week_completion_dto(completion: WeekCompletion) -> dict[str, Any]:
    """一个计划周完成率 → 传输对象；分母为零的「暂无」由服务返回 None，本函数不伪造。"""
    return {
        "plan_version_id": completion.plan_version_id,
        "week_no": completion.week_no,
        "week_start": completion.week_start.isoformat(),
        "week_end": completion.week_end.isoformat(),
        "planned": completion.denominator,
        "completed": completion.numerator,
        "rate": (
            None
            if completion.denominator == 0
            else completion.numerator / completion.denominator
        ),
    }


def review_dto(view: ReviewView) -> dict[str, Any]:
    """一条复盘 → 传输对象：Markdown 正文 + 生成时快照 + 现算 stale（不静默改写正文）。"""
    return {
        "id": view.id,
        "body_markdown": view.body_markdown,
        "stale": view.stale,
        "generated_at": view.generated_at,
        "source_revision_ids": list(view.source_revision_ids),
        "basis": _roundtrip_json(review_basis_to_json(view.basis)),
    }


# ---------- Stage 3：草稿载荷映射（计划／记录／安排；S3-14） ----------


def _draft_row_dto(draft: Any) -> dict[str, Any]:
    """草稿行的传输字段（与档案草稿同一形状）：身份、kind、状态、revision 与提交凭据。"""
    return {
        "id": draft.id,
        "kind": draft.kind,
        "status": draft.status,
        "revision": draft.revision,
        "base_business_version": draft.base_business_version,
        "committed_revision": draft.committed_revision,
        "committed_business_version": draft.committed_business_version,
    }


def _structured_diff_dto(items: Sequence[Any]) -> list[dict[str, Any]]:
    """结构化字段 Diff → 传输对象：保留字段名与 before／after 结构，不压成文本 diff。"""
    return [
        {
            "field": item.field,
            "before": _jsonable(item.before),
            "after": _jsonable(item.after),
            "changed": item.changed,
        }
        for item in items
    ]


def plan_draft_dto(view: PlanDraftView) -> dict[str, Any]:
    """计划草稿 → 传输对象：拟议计划（D9 文本契约形状）＋日程／取消预览／档案补丁 + 结构化 Diff。

    计划行字段（``starts_on``／``review_on``／``mode``／``source_plan_version_id``）与 payload
    分开给出（D9：关系字段不进 payload）；``cancellations`` 只是拟议预览，正式取消在确认事务
    内按当刻规则重算。
    """
    proposal = view.proposal
    return {
        **_draft_row_dto(view.draft),
        "payload": {
            "starts_on": proposal.starts_on.isoformat(),
            "review_on": proposal.review_on.isoformat(),
            "mode": proposal.mode,
            "source_plan_version_id": proposal.source_plan_version_id,
            "plan": _roundtrip_json(payload_to_json(proposal.payload)),
            "schedules": _jsonable(view.schedules),
            "cancellations": _jsonable(proposal.cancellations),
            "proposed_profile": profile_facts_dto(view.proposed_profile),
            "profile_patch": (
                None
                if view.proposed_profile_patch is None
                else _jsonable(view.proposed_profile_patch)
            ),
        },
        "diff": _structured_diff_dto(view.plan_diff),
        "profile_diff": (
            None
            if view.profile_diff is None
            else [_field_diff_dto(item) for item in view.profile_diff]
        ),
    }


def record_draft_dto(view: RecordDraftView) -> dict[str, Any]:
    """记录草稿 → 传输对象：拟议载荷（存储契约形状）＋派生修订状态 + 结构化 Diff。

    Diff 由后端按存储载荷与库内基线现算，不接受客户端 before／after。
    """
    return {
        **_draft_row_dto(view.draft),
        "payload": {
            "record": _roundtrip_json(record_draft_to_json(view.payload)),
            "status": view.status,
        },
        "diff": _structured_diff_dto(view.diff),
    }


def arrangement_draft_dto(view: ArrangementDraftView) -> dict[str, Any]:
    """安排草稿 → 传输对象：当次完整目标（快照形状）＋绑定版本该训练日（原计划对照）。"""
    return {
        **_draft_row_dto(view.draft),
        "payload": {
            "target": _roundtrip_json(arrangement_target_to_json(view.target)),
            "planned_workout": _jsonable(view.planned_workout),
        },
        "session": _jsonable(view.session),
        "plan_version": {
            "id": view.plan_version.id,
            "version": view.plan_version.version,
        },
    }


def any_draft_dto(view: Any) -> dict[str, Any]:
    """按草稿视图类型分派载荷映射（计划／记录／安排／档案），不把别的 kind 按错形状发出。"""
    if isinstance(view, PlanDraftView):
        return plan_draft_dto(view)
    if isinstance(view, RecordDraftView):
        return record_draft_dto(view)
    if isinstance(view, ArrangementDraftView):
        return arrangement_draft_dto(view)
    return draft_dto(cast(DraftView, view))


# ---------- Stage 3：草稿纠错请求体解码 ----------


def _date_from_dto(name: str, value: object) -> date:
    if not isinstance(value, str):
        raise InvalidRequestShape(f"{name} 必须是 ISO 日期文本：{value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestShape(f"{name} 不是 ISO 日期：{value!r}") from exc


def plan_revision_from_dto(body: dict[str, Any]) -> tuple[date, date, PlanPayload]:
    """计划纠错载荷 → （starts_on, review_on, D9 payload）。

    只做形状解码：payload 用 D9 文本契约（``payload_from_json``），不符合契约按传输形状拒绝
    （400）；业务规则（区间、引用、限制）由应用层领域校验拒绝（422），这里不重复实现。
    """
    payload = body["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "starts_on",
        "review_on",
        "plan",
    }:
        raise InvalidRequestShape(
            "payload 必须是只含 starts_on／review_on／plan 的 JSON 对象"
        )
    starts_on = _date_from_dto("starts_on", payload["starts_on"])
    review_on = _date_from_dto("review_on", payload["review_on"])
    try:
        plan = payload_from_json(json.dumps(payload["plan"], ensure_ascii=False))
    except InvalidPlanRow as exc:
        raise InvalidRequestShape(f"计划载荷形状不合法：{exc}") from exc
    return starts_on, review_on, plan


def record_payload_from_dto(body: dict[str, Any]) -> RecordDraftPayload:
    """记录纠错载荷 → :class:`RecordDraftPayload`（存储契约形状；形状不符按 400 拒绝）。"""
    payload = body["payload"]
    if not isinstance(payload, dict):
        raise InvalidRequestShape("payload 必须是 JSON 对象")
    try:
        return record_draft_from_json(json.dumps(payload, ensure_ascii=False))
    except InvalidRecordRow as exc:
        raise InvalidRequestShape(f"记录载荷形状不合法：{exc}") from exc
