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
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.confirm import (
    DraftDiscarded,
    DraftStale,
    NoBusinessChange,
    ProfileCommitResult,
)
from app.drafts import (
    DraftNotCorrectable,
    DraftNotDiscardable,
    DraftRevisionConflict,
    DraftView,
    ProfileFieldDiff,
    UnknownDraft,
)
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

# 本阶段只有档案草稿（stage2.md §3 不做计划／记录草稿）；草稿类型沿用前端契约的取值。
PROFILE_UPDATE_KIND = "profile_update"

_STALE_DETAIL_NO_FIELD_CHANGE = "业务版本已变化，当前快照无字段差异"


class InvalidRequestShape(ValueError):
    """请求体不是接口约定的 JSON 形状：在触碰应用层之前即拒绝（400 ``invalid_request``）。"""


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
    """档案 → 九个事实字段的三态对象；未收集与明确无显式表达，不用空数组／默认值填充。"""
    return {name: _fact_dto(getattr(profile, name)) for name in FACT_FIELDS}


def profile_response_dto(snapshot: ProfileSnapshot) -> dict[str, Any]:
    """正式档案读取快照 → ``GET /api/profile`` 响应体。

    ``profile: null`` 表示尚未建档（``profile_json IS NULL``）；已建档（含部分事实）时是九个
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

    ``committed_revision``／``committed_business_version`` 只在已提交草稿上有值，就是持久化的
    提交凭据（后续业务版本变化不改写它）；Diff 由后端按基线与拟议快照计算，不接受客户端传入。
    """
    draft = view.draft
    return {
        "id": draft.id,
        "kind": PROFILE_UPDATE_KIND,
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
    """纠错载荷 ``{"profile": {字段: {state, value}}}`` → 拟议档案（九个字段全量）。

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
_ERROR_STATUS: tuple[tuple[type[Exception], int, str], ...] = (
    (InvalidRequestShape, 400, "invalid_request"),
    (UnknownDraft, 404, "invalid_request"),
    (DraftRevisionConflict, 409, "draft_modified"),
    (DraftStale, 409, "draft_stale"),
    (DraftNotCorrectable, 409, "invalid_request"),
    (DraftNotDiscardable, 409, "invalid_request"),
    (DraftDiscarded, 409, "invalid_request"),
    (NoBusinessChange, 409, "invalid_request"),
    (InvalidProfile, 422, "invalid_request"),
    (IncompleteProfile, 422, "invalid_request"),
    (UnknownExerciseReference, 422, "invalid_request"),
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
