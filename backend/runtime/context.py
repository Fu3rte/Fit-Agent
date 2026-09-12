"""业务事实投影与会话历史构造（07「消息存储与上下文适配契约」；stage4.md S4-04）。

两条硬边界：

1. **当前事实每 Run 重读、只注入一次**：正式档案、限制／红旗与计划指导由 Stage 3 应用层
   读取入口（:class:`~domain.profile.service.ProfileService`、
   :class:`~app.plan_reads.PlanReadService`）读取，作为**一条** ``SystemPromptPart`` 注入本次
   请求；不落库、不进历史，因此下一 Run 读到的仍是当刻事实——历史消息与摘要不得冒充最新
   业务事实（4.3）。计划指导一律经 ``PlanReadService`` 复核：``usable=false`` 时不投影可执行
   处方（只给计划行字段、日程与阻断原因）。
2. **历史只读、按框架原生格式往返**：已完成 Run 的框架消息用 ``ModelMessagesTypeAdapter``
   反序列化，不造第二套 transcript 格式；构建历史**不执行工具**、不恢复旧 Run。未完成
   （failed／cancelled／重启中断）的 Run 只保留用户请求事实并显式标注中断；部分回答
   （``kind='partial'``）不进模型上下文（07 7.4）。

本模块只读业务事实与已保存消息，不写正式事实、不推进 ``context_version``。档案与计划指导
各自经应用层读入口读取（两次读事务，不跨服务合并事务）：单用户、全局单 Run 且正式写入只由
用户确认触发，运行中不存在第二个写入者。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
    UserPromptPart,
)

from app.draft_repo import Draft, DraftRepo, InvalidDraftRow
from app.plan_reads import PlanGuidance, PlanReadService
from domain.plan.schema import payload_to_json
from domain.profile.safety import message_red_flag_hits
from domain.profile.schema import (
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo

#: 中断标注前缀：非用户发言、非业务事实，只说明上一条请求没有产出完整回答（07 7.4）。
INTERRUPTION_MARKER = "[系统标注] 上一条用户请求未完成，用户没有收到完整回答"

#: 未完成 Run 的历史形态：请求事实 + 显式中断标注，不把残留部分回答当作已发生事实。
_INTERRUPTED_STATUS_LABEL = {
    "failed": "执行失败",
    "cancelled": "已被用户取消",
    "pending": "尚未执行",
    "running": "仍在执行",
}

_FACTS_HEADER = """以下是系统在本次请求开始时从业务库读取的**当前事实**（只读，权威）。

- 只把本段内容与工具查询结果当作最新事实；历史消息、早先回答与摘要都可能过期，不得冒充当前事实。
- 缺事实或目标歧义时必须向用户询问，不得自行补齐、猜测或编造。
- 正式业务事实只能由用户经业务接口确认后写入；你只能读取业务数据、提出待确认草稿或回答。
- 计划指导一律以本段的计划安全复核为准：``safety.usable=false`` 时不得给出任何可执行处方。
- 本段安全复核已把会话内**待确认档案草稿**的拟议条件（含限制与红旗）一并计入：草稿未确认
  期间 ``usable=false``，同样不得给出可执行处方，直到用户确认或丢弃该草稿。
- 本次用户消息中出现已拍红旗说法（文本兜底词表命中，仅扫本条消息、精确子串、不解析否定句）
  时，本 Run 的安全复核已强制 ``usable=false``：只可追问或说明，不得给出任何可执行处方；
  该兜底不覆盖「功能受限」等结构化评估（B 未实现）。
- 身体状况更新后的路由（已拍）：用户明确「只记录」→ 只提档案草稿；明确「同时调整长期计划」
  → 提**一条**受限组合计划草稿（档案补丁＋计划 payload 一起待确认、两项 Diff）；未明确选择→
  先追问，不落库；已有正式计划且用户随后明确要求调整 → 独立计划草稿。当次安排不是长期调整
  的同意；自然语言只产生 Pending 草稿，正式生效必须由用户在草稿卡点击确认。
"""


@dataclass(frozen=True, slots=True)
class BusinessFacts:
    """一次 Run 开始时读取的当前权威事实；不落库、不来自历史或摘要。"""

    business_date: date
    snapshot: ProfileSnapshot
    guidance: PlanGuidance | None
    drafts: tuple[Draft, ...]


async def read_business_facts(
    db: Database,
    *,
    conversation_id: str,
    business_date: date,
    message_red_flags: Sequence[str] = (),
) -> BusinessFacts:
    """读取同一业务版本的正式事实，并查询会话草稿当前状态。

    安全复核把会话内待确认档案草稿的拟议条件一并计入（fail-closed）：未确认的红旗同样不得
    放出可执行处方，但草稿不是正式事实——``context_version`` 仍取正式业务版本。
    ``message_red_flags`` 是由调用方从**当前 Run 最新用户消息**扫出的 C 层兜底词（2026-09-12
    拍板）：非空即把本 Run 的安全复核强制为不可用（只做精确子串文本兜底）。
    """
    profiles = ProfileService(db)
    plans = PlanReadService(db)
    drafts = await DraftRepo(db).list_for_conversation(conversation_id)
    safety_profile = pending_profile_from_drafts(drafts)
    while True:
        before = await profiles.read_formal_profile()
        guidance = await plans.read_current_plan_guidance(
            business_date=business_date,
            safety_profile=safety_profile,
            message_red_flags=message_red_flags,
        )
        snapshot = await profiles.read_formal_profile()
        if before.context_version == snapshot.context_version and (
            guidance is None or guidance.context_version == snapshot.context_version
        ):
            break
    return BusinessFacts(
        business_date=business_date,
        snapshot=snapshot,
        guidance=guidance,
        drafts=drafts,
    )


async def pending_proposed_profile(
    db: Database, *, conversation_id: str
) -> Profile | None:
    """会话内最近一条待确认档案草稿的拟议条件；无则 ``None``（仍按正式档案复核）。

    未确认草稿不是正式事实：只用于把其中的限制与红旗 fail-closed 地纳入安全投影，
    不推进 ``context_version``、不写库。
    """
    return pending_profile_from_drafts(
        await DraftRepo(db).list_for_conversation(conversation_id)
    )


def message_red_flags(user_text: str) -> tuple[str, ...]:
    """当前 Run 最新用户消息的 C 层兜底红旗词（词表与判定归 ``domain.profile.safety``）。

    runtime 只经本文件引用档案领域（Stage 1 接线白名单）；只做精确子串文本兜底。
    """
    return message_red_flag_hits(user_text)


def pending_profile_from_drafts(drafts: Sequence[Draft]) -> Profile | None:
    """已读草稿行 → 最近的待确认档案草稿拟议条件；草稿结构损坏即显式失败。"""
    pending = [
        draft
        for draft in drafts
        if draft.kind == "profile_update" and draft.status == "pending"
    ]
    if not pending:
        return None
    raw = pending[-1].proposed_profile_json
    if raw is None:
        raise InvalidDraftRow(f"待确认档案草稿缺少拟议条件：{pending[-1].id}")
    return profile_from_json(raw)


def _load_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("已持久化业务或消息 JSON 无法解析") from exc


def profile_facts(snapshot: ProfileSnapshot) -> dict[str, Any] | None:
    """正式档案 → 八字段三态对象（存储契约形状）；未建档为 ``None``，不伪造默认值。"""
    if snapshot.profile is None:
        return None
    return _load_json(profile_to_json(snapshot.profile))


def plan_guidance_facts(guidance: PlanGuidance) -> dict[str, Any]:
    """计划指导 → 模型可见投影：计划行字段与日程 + 安全复核；处方仅在放行时给出。

    ``safety.usable=false``（限制冲突、红旗或目录身份读不到）时 ``payload`` 为 ``None``：
    计划与历史仍可查看，但不投影可执行处方（S3-07 已拍）。字段拼写与 ``GET /api/plan/guidance``
    的语义一致（传输层 DTO 归 api，本投影只服务模型上下文）。
    """
    version = guidance.plan.version
    safety = guidance.safety
    blocked = safety.is_blocked
    return {
        "plan": {
            "id": version.id,
            "version": version.version,
            "is_current": guidance.plan.is_current,
            "starts_on": version.starts_on.isoformat(),
            "review_on": version.review_on.isoformat(),
            "mode": version.mode,
            "payload": (
                None if blocked else _load_json(payload_to_json(version.payload))
            ),
            "schedules": [
                {
                    "id": item.session.id,
                    "plan_workout_key": item.session.plan_workout_key,
                    "scheduled_on": item.session.scheduled_on.isoformat(),
                    "cancelled": item.cancelled,
                    "locked": item.lock.effective,
                }
                for item in guidance.plan.sessions
            ],
        },
        "safety": {
            "context_version": guidance.context_version,
            "usable": not blocked,
            "red_flag_blocked": safety.red_flags.is_blocked,
            "message_red_flags": list(safety.message_red_flags),
            "action_unavailable": bool(safety.unknown_exercise_ids),
            "unknown_exercise_ids": list(safety.unknown_exercise_ids),
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
            "reasons": list(safety.blocking_reasons),
            "clarifications": list(safety.clarification_reasons),
        },
    }


def draft_facts(draft: Draft) -> dict[str, Any]:
    """已持久化草稿的当前状态与拟议内容；用于恢复中断 Run 已发生的操作语境。"""
    proposal = {
        "profile_update": draft.proposed_profile_json,
        "plan": draft.proposed_plan_json,
        "training_record": draft.proposed_record_json,
        "arrangement": draft.proposed_arrangement_json,
    }[draft.kind]
    return {
        "id": draft.id,
        "kind": draft.kind,
        "run_id": draft.run_id,
        "status": draft.status,
        "revision": draft.revision,
        "base_business_version": draft.base_business_version,
        "committed_revision": draft.committed_revision,
        "committed_business_version": draft.committed_business_version,
        "proposal": None if proposal is None else _load_json(proposal),
    }


def facts_prompt(facts: BusinessFacts) -> str:
    """当前事实投影 → 本次请求的唯一 ``SystemPromptPart`` 文本（确定性、可比较）。"""
    projection = {
        "business_date": facts.business_date.isoformat(),
        "context_version": facts.snapshot.context_version,
        "profile": profile_facts(facts.snapshot),
        "plan_guidance": (
            None if facts.guidance is None else plan_guidance_facts(facts.guidance)
        ),
        "conversation_drafts": [draft_facts(draft) for draft in facts.drafts],
    }
    return (
        _FACTS_HEADER
        + json.dumps(projection, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    )


def current_facts_request(facts: BusinessFacts) -> ModelRequest:
    """把当前事实投影包成请求部件：一次 Run 只注入这一条 ``SystemPromptPart``。"""
    return ModelRequest(parts=[SystemPromptPart(content=facts_prompt(facts))])


async def load_conversation_history(
    repo: RunRepo, *, conversation_id: str, current_run_id: str
) -> list[ModelMessage]:
    """已保存会话 → 原生 :class:`ModelMessage` 列表（不含本次 Run 的用户请求）。

    - 已完成 Run：``kind='framework'`` 行逐条 ``ModelMessagesTypeAdapter`` 反序列化，顺序即
      ``seq``；历史里的 ``SystemPromptPart``（旧系统事实）被剔除，当前事实只由本次注入给出。
    - 未完成 Run（failed／cancelled／重启中断等）：只保留用户请求事实，并标注上一条请求未完成；
      部分回答不进上下文。用户请求在模型上下文里只出现一次（完成 Run 的框架消息已含它）。
    - 本函数不执行工具、不调用模型、不写库：历史里的工具调用与结果只是已保存记录。
    """
    rows = await repo.list_messages(conversation_id)
    history: list[ModelMessage] = []
    for run_id, run_rows in _group_by_run(rows):
        if run_id == current_run_id:
            # 本次请求的用户文本由 agent 的 user prompt 注入，不重复注入（07 7.4）。
            continue
        run = await repo.get_run(run_id)
        status = "pending" if run is None else str(run["status"])
        text = _user_request_text(run_rows)
        if status == "completed":
            history.extend(_framework_history(run_rows, user_text=text))
            continue
        if text is None:
            continue
        label = _INTERRUPTED_STATUS_LABEL.get(status, status)
        history.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=f"{text}\n\n{INTERRUPTION_MARKER}（{label}）："
                        "不要把这次请求当作已完成的回答或已发生的结果。"
                    )
                ]
            )
        )
    return history


def _group_by_run(rows: Sequence[dict[str, Any]]) -> list[tuple[str, list[dict]]]:
    """按 ``run_id`` 分组、保持消息顺序（``list_messages`` 已按 ``seq`` 排序）。"""
    grouped: list[tuple[str, list[dict]]] = []
    for row in rows:
        run_id = str(row["run_id"])
        if grouped and grouped[-1][0] == run_id:
            grouped[-1][1].append(row)
        else:
            grouped.append((run_id, [row]))
    return grouped


def _framework_history(
    rows: Sequence[dict[str, Any]], *, user_text: str | None
) -> list[ModelMessage]:
    """已完成 Run 的框架消息：原生反序列化 + 剔除旧系统事实部件。

    框架把本次用户提示合并进传入历史的第一条请求（本 Run 的用户请求因此不进 ``new_messages``），
    所以本 Run 的框架消息可能没有用户内容；此时从用户请求应用事实补回**一条**请求，
    保证上下文里用户请求只出现一次（07 7.4）。
    """
    history: list[ModelMessage] = []
    has_user_prompt = False
    for row in rows:
        if row["kind"] != "framework":
            continue  # partial 等其余行不进模型上下文
        for message in ModelMessagesTypeAdapter.validate_json(str(row["payload_json"])):
            if isinstance(message, ModelRequest):
                parts = [
                    part
                    for part in message.parts
                    if not isinstance(part, SystemPromptPart)
                ]
                if not parts:
                    continue  # 只含旧系统事实的请求没有可回灌内容
                has_user_prompt = has_user_prompt or any(
                    isinstance(part, UserPromptPart) for part in parts
                )
                history.append(ModelRequest(parts=parts, timestamp=message.timestamp))
            else:
                history.append(message)
    if not has_user_prompt and user_text is not None:
        history.insert(0, ModelRequest(parts=[UserPromptPart(content=user_text)]))
    return history


def _user_request_text(rows: Sequence[dict[str, Any]]) -> str | None:
    """未完成 Run 的用户请求文本（``kind='user_request'`` 应用事实）；无则 ``None``。"""
    for row in rows:
        if row["kind"] == "user_request":
            return str(_load_json(str(row["payload_json"]))["text"])
    return None


def framework_messages(
    messages: Sequence[ModelMessage],
) -> list[tuple[str, str]]:
    """本 Run 新产生的框架消息 → ``complete_run`` 输入 ``(role, payload_json)``。

    只序列化传入的新消息；已加载的历史不在返回值里，调用方不得把历史重新回写（07 7.4）。
    """
    return [
        (
            "user" if isinstance(message, ModelRequest) else "assistant",
            ModelMessagesTypeAdapter.dump_json([message]).decode("utf-8"),
        )
        for message in messages
    ]
