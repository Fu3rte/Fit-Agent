import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from app.agent import compaction as C
from app.agent.agent_loop import run_compaction
from app.agent.config import CompactionHooks, LoopCompactionResult
from app.agent.message_context import business_context_to_llm
from app.agent.tools.business import business_tool_declarations
from app.ai.context import get_current_system_message
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import BusinessService, business_date
from app.application.session.business_context import (
    BusinessBindings,
    collect_message_nodes,
    collect_pending_proposals,
    next_business_context_event,
)
from app.application.session.service import SessionService
from app.domain.business.errors import BusinessError
from app.domain.business.models import (
    BusinessContext,
    PendingProposalArguments,
    ProfileContent,
    ProfileProposalArguments,
    SaveConfirmationRequest,
)
from app.domain.session.errors import SessionConflict
from app.domain.session.models import (
    BusinessContextEntry,
    BusinessContextSnapshot,
    CompactionDetails,
    CompactionEntry,
    EditCommand,
    EditRequest,
    RequestContext,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    build_business_context_entry,
    business_baseline_entry_id,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import model_visible_entries
from test.regression_support import temporary_root

EVIDENCE = temporary_root("business-context-compaction")
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SPEC = C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=100)
PROFILE_PAYLOAD = {
    "goal": "增肌",
    "experience": None,
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def uid() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


def system_message() -> SystemMessage:
    return SystemMessage(
        role="system",
        content="系统提示词。",
        tools_added=business_tool_declarations(),
        timestamp=now_ms(),
    )


def assistant(calls, stop_reason: str, text: str = "") -> AssistantMessage:
    content = [
        ToolCall(type="toolCall", id=cid, name=name, arguments=args)
        for cid, name, args in calls
    ]
    if text:
        content.append(TextContent(type="text", text=text))
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="openai",
        model="test",
        usage=USAGE,
        stop_reason=stop_reason,
        timestamp=now_ms(),
    )


def tool_result(call_id: str, name: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(type="text", text=text)],
        is_error=False,
        timestamp=now_ms(),
    )


async def build_services(path: Path):
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    business = BusinessService(repository, catalog, service, ReplacementCoordinator())
    service.attach_snapshots(business)
    async with repository.transaction():
        await repository.replace_exercises(catalog.all())
    return database, service, repository, business


async def start_run(service, session_id: str, text: str):
    await service.create_session(session_id, "压缩资料")
    outcome = await service.accept_send(
        SendCommand(
            operation_id=uid(), session_id=session_id, request=SendRequest(text=text)
        ),
        system_message=system_message(),
    )
    return outcome.run


async def append(service, session_id, run_id, parent_id, message) -> str:
    node_id = uid()
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id, id=node_id, parent_id=parent_id, run_id=run_id,
            type="message", messages=[message], created_at=now_ms(),
        )
    )
    return node_id


async def bindings(business, session_id) -> BusinessBindings:
    return BusinessBindings(
        profile_confirmations=await business.list_confirmation_bindings(session_id),
        workout_confirmations=await business.list_workout_confirmation_bindings(session_id),
        plan_confirmations=await business.list_plan_confirmation_bindings(session_id),
        profile_displays=await business.list_display_bindings(session_id),
        workout_displays=await business.list_workout_display_bindings(session_id),
        plan_displays=await business.list_plan_display_bindings(session_id),
    )


async def append_delta(service, business, session_id, run_id, leaf_id, request_entry_id) -> str | None:
    projection = await service.get_projection(session_id, leaf_id)
    visible = set(projection.context.source_entry_ids)
    nodes = collect_message_nodes(
        model_visible_entries(projection.path, visible), await bindings(business, session_id)
    )
    proposals = collect_pending_proposals(
        await business.list_pending_references(session_id), {e.id for e in projection.path}
    )
    entry = await service.get_entry(session_id, request_entry_id)
    details = await next_business_context_event(
        path=projection.path,
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=business_date(entry.created_at),
            request_entry_id=request_entry_id,
        ),
        nodes=nodes,
        proposals=proposals,
        removal_reason=business.removed_proposal_reason,
    )
    if details is None:
        return None
    node = build_business_context_entry(
        session_id=session_id, id=uid(), parent_id=leaf_id, run_id=run_id,
        details=details, created_at=now_ms(),
    )
    outcome = await service.append_business_context(node)
    return outcome.entry.id


# 与生产 http.build_compaction_baseline 同口径：按拟提交检查点形成的压缩后投影重建完整基线。
async def build_baseline(service, business, session_id, run_id, path, entry, request_entry_id):
    prospective = C.build_effective_context([*path, entry])
    visible = set(prospective.source_entry_ids)
    nodes = collect_message_nodes(
        model_visible_entries([*path, entry], visible), await bindings(business, session_id)
    )
    proposals = collect_pending_proposals(
        await business.list_pending_references(session_id), {e.id for e in path}
    )
    entry_node = await service.get_entry(session_id, request_entry_id)
    snapshot = BusinessContextSnapshot(
        mode="snapshot",
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=business_date(entry_node.created_at),
            request_entry_id=request_entry_id,
        ),
        message_nodes=list(nodes.values()),
        pending_proposals=list(proposals.values()),
    )
    return build_business_context_entry(
        session_id=session_id,
        id=business_baseline_entry_id(entry.id),
        parent_id=entry.id,
        run_id=run_id,
        details=snapshot,
        created_at=now_ms(),
    )


async def prepare_checkpoint(
    service, business, session_id, run_id, path, request_entry_id, *, summary="固定摘要"
):
    projection = C.build_effective_context(path)
    system_state = get_current_system_message(projection.messages)
    preparation = C.prepare_compaction(path, 128000, 16384, SPEC)
    checkpoint = CompactionEntry(
        session_id=session_id, id=uid(), parent_id=path[-1].id, run_id=run_id,
        type="compaction", summary=summary,
        first_kept_entry_id=preparation.first_kept_entry_id,
        tokens_before=preparation.tokens_before, usage=USAGE, system_message=system_state,
        details=CompactionDetails(attachments=preparation.attachments) if preparation.attachments else None,
        created_at=now_ms(),
    )
    baseline = await build_baseline(
        service, business, session_id, run_id, path, checkpoint, request_entry_id
    )
    return checkpoint, baseline


async def commit_checkpoint(
    service, business, session_id, run_id, path, request_entry_id, *, summary="固定摘要"
):
    checkpoint, baseline = await prepare_checkpoint(
        service, business, session_id, run_id, path, request_entry_id, summary=summary
    )
    outcome = await service.commit_compaction(checkpoint, baseline, prepared_position=checkpoint.parent_id)
    return checkpoint, baseline, outcome


async def scenario_summary_input_and_cut(root: Path) -> dict:
    database, service, _repo, business = await build_services(root / "summary.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "长需求" * 40)
        user_id = run.request_entry_id
        delta_leaf = await append_delta(service, business, session_id, run.id, user_id, user_id)
        long_id = await append(
            service, session_id, run.id, delta_leaf,
            assistant([], "stop", "长历史回答" * 400),
        )
        call_id = await append(
            service, session_id, run.id, long_id,
            assistant([("c1", "get_profile", {})], "toolUse"),
        )
        # 工具结果紧跟发起调用的助手节点；该资料节点携带固定业务文本，准备进入历史摘要范围。
        tool_id = await append(service, session_id, run.id, call_id, tool_result("c1", "get_profile", "{}"))
        second_leaf = await append_delta(service, business, session_id, run.id, tool_id, user_id)
        tail = await append(service, session_id, run.id, second_leaf, assistant([], "stop", "保留原文" * 200))
        await append(service, session_id, run.id, tail, assistant([], "stop", "收尾" * 400))

        projection = await service.get_projection(session_id, tail)
        by_id = {e.id: e for e in projection.path}
        business_nodes = [e for e in projection.path if isinstance(e, BusinessContextEntry)]
        assert business_nodes, "分支应含资料节点"

        preparation = C.prepare_compaction(projection.path, 128000, 16384, SPEC)
        # 摘要输入由历史摘要与任务前缀摘要共同组成；单用户回合长分支的切点落在回合内部，
        # 资料节点经共享转换进入任务前缀摘要输入，其固定 content 原样出现。
        summarized_text = C.serialize_conversation(
            [*preparation.messages_to_summarize, *preparation.turn_prefix_messages]
        )
        assert all(node.message.content in summarized_text for node in business_nodes)
        first_kept = by_id[preparation.first_kept_entry_id]
        assert isinstance(first_kept, SessionMessageEntry)
        assert all(not C._is_cut_point(e) for e in business_nodes)
        assert all(not C._is_turn_start(e) for e in business_nodes)

        # 资料按实际投影计入保留预算与摘要输入：计数即其 LLM 投影，非空且正 token。
        business_node = business_nodes[-1]
        projected = business_context_to_llm(business_node.message)
        assert C._counted_messages(business_node) == [projected]
        assert C._summarizable_messages(business_node) == [projected]
        assert C.estimate_tokens(projected) > 0
        with_business = C.estimate_projected_context_tokens(projection.context, projection.path)
        return {
            "first_kept_is_real": isinstance(first_kept, SessionMessageEntry),
            "summarized_has_business": "<business_context>" in summarized_text,
            "business_nodes": [e.id for e in business_nodes],
            "with_business_tokens": with_business.tokens,
        }
    finally:
        await database.close()


async def stage_profile_branch(service, business, session_id, title):
    run = await start_run(service, session_id, title)
    user_id = run.request_entry_id
    leaf = await append_delta(service, business, session_id, run.id, user_id, user_id)
    call_id = "call-prepare"
    arg = ProfileProposalArguments(
        profile_id=1, base_profile_version=None, payload=ProfileContent(**PROFILE_PAYLOAD)
    )
    assistant_id = await append(
        service, session_id, run.id, leaf, assistant([(call_id, "prepare_profile_update", arg.model_dump())], "toolUse")
    )
    context = BusinessContext(
        timezone="Asia/Shanghai", business_date=business_date((await service.get_entry(session_id, user_id)).created_at),
        session_id=session_id, run_id=run.id, request_entry_id=user_id, source_entry_id=assistant_id,
    )
    proposal = await business.prepare_profile_update(context, arg)
    display = await append(
        service, session_id, run.id, assistant_id,
        tool_result(call_id, "prepare_profile_update", json.dumps(proposal.model_dump(), ensure_ascii=False)),
    )
    await business.bind_display_entry(proposal.proposal_id, display)
    leaf = await append_delta(service, business, session_id, run.id, display, user_id)
    return run, user_id, assistant_id, leaf, proposal, display


async def scenario_atomic_baseline(root: Path) -> dict:
    database, service, _repo, business = await build_services(root / "atomic.db")
    try:
        session_id = uid()
        run, user_id, assistant_id, leaf, proposal, display = await stage_profile_branch(
            service, business, session_id, "建档并压缩"
        )
        for _ in range(3):
            mid = await append(service, session_id, run.id, leaf, assistant([], "stop", "长轮次" * 400))
            leaf = await append_delta(service, business, session_id, run.id, mid, user_id)

        projection = await service.get_projection(session_id, leaf)
        checkpoint, baseline, outcome = await commit_checkpoint(
            service, business, session_id, run.id, projection.path, user_id
        )
        assert outcome.created and not outcome.credential_detected
        assert outcome.entry.id == checkpoint.id and outcome.baseline.id == baseline.id

        run_after = await service.get_run(session_id, run.id)
        session_after = await service.get_session(session_id)
        # 运行末节点与分支叶更新到新基线，而非检查点。
        assert run_after.last_entry_id == baseline.id
        assert session_after.active_leaf_id == baseline.id

        by_id = {e.id: e for e in await service.list_entries(session_id)}
        stored_baseline = by_id[baseline.id]
        assert isinstance(stored_baseline, BusinessContextEntry)
        assert stored_baseline.parent_id == checkpoint.id
        assert stored_baseline.run_id == run.id
        snapshot = stored_baseline.message.details
        assert isinstance(snapshot, BusinessContextSnapshot)
        # 基线 message_nodes 只含压缩后可见的真实消息节点：资料/摘要/压缩节点不进入。
        node_ids = {n.entry_id for n in snapshot.message_nodes}
        assert baseline.id not in node_ids and checkpoint.id not in node_ids
        assert all(isinstance(by_id[i], SessionMessageEntry) for i in node_ids)
        # 被摘要的真实来源仍受控保留在待确认提案集合。
        pending_ids = {p.proposal_id for p in snapshot.pending_proposals}
        assert proposal.proposal_id in pending_ids

        # 受控读取完整提案：来源已被摘要仍可按 proposal_id 读取（完整分支保留）。
        read = await business.get_pending_proposal(
            BusinessContext(
                timezone="Asia/Shanghai", business_date=business_date((await service.get_entry(session_id, user_id)).created_at),
                session_id=session_id, run_id=run.id, request_entry_id=user_id, source_entry_id=assistant_id,
            ),
            PendingProposalArguments(business_kind="profile", proposal_id=proposal.proposal_id),
        )
        assert read.proposal_id == proposal.proposal_id and read.display_entry_id == display

        # 基线为压缩后投影：以基线为叶的投影在其上追加资料节点自身。
        leaf_projection = await service.get_projection(session_id, baseline.id)
        assert leaf_projection.context.source_entry_ids[-1] == baseline.id
        # 新基线后无变化不追加，出现新真实节点才追加增量。
        assert await append_delta(service, business, session_id, run.id, baseline.id, user_id) is None
        new_mid = await append(service, session_id, run.id, baseline.id, assistant([], "stop", "基线后回答"))
        after = await append_delta(service, business, session_id, run.id, new_mid, user_id)
        assert after is not None
        return {
            "baseline_id": baseline.id, "checkpoint_id": checkpoint.id,
            "last_entry_id": run_after.last_entry_id,
            "message_node_count": len(snapshot.message_nodes),
            "pending_kept": sorted(pending_ids),
            "post_delta": after,
        }
    finally:
        await database.close()


async def seed_running(service, business, text):
    session_id = uid()
    run = await start_run(service, session_id, text)
    user_id = run.request_entry_id
    leaf = await append_delta(service, business, session_id, run.id, user_id, user_id)
    for _ in range(3):
        leaf = await append(service, session_id, run.id, leaf, assistant([], "stop", "长文" * 400))
    path = (await service.get_projection(session_id, leaf)).path
    return session_id, run, user_id, path


async def scenario_atomic_failure_and_identity(root: Path) -> dict:
    database, service, _repo, business = await build_services(root / "fail.db")
    secret = "sk-leak-into-baseline"
    try:
        # 会话一：非法保留起点与凭据命中都在提交前失败，运行末节点与叶不动、不落库。
        session_id, run, user_id, path = await seed_running(service, business, "失败原子性" * 40)
        before = {e.id for e in await service.list_entries(session_id)}
        checkpoint, baseline = await prepare_checkpoint(
            service, business, session_id, run.id, path, user_id
        )

        illegal = checkpoint.model_copy(update={"first_kept_entry_id": "ghost"})
        try:
            await service.commit_compaction(illegal, baseline, prepared_position=illegal.parent_id)
        except ValueError:
            pass
        else:
            raise AssertionError("非法保留起点未拒绝")
        assert {e.id for e in await service.list_entries(session_id)} == before
        assert (await service.get_run(session_id, run.id)).last_entry_id == path[-1].id

        cred_checkpoint, cred_baseline = await prepare_checkpoint(
            service, business, session_id, run.id, path, user_id, summary=f"命中 {secret}"
        )
        outcome = await service.commit_compaction(
            cred_checkpoint, cred_baseline,
            prepared_position=cred_checkpoint.parent_id, credentials=(secret,),
        )
        assert outcome.credential_detected is True and outcome.created is False
        assert outcome.entry is None and outcome.baseline is None
        assert {e.id for e in await service.list_entries(session_id)} == before
        assert (await service.get_run(session_id, run.id)).status == "failed"

        # 会话二：同身份重试复用已提交检查点与其基线，不产生第二个基线；字段冲突就地拒绝。
        session_b, run_b, user_b, path_b = await seed_running(service, business, "幂等重试" * 40)
        cp_b, bl_b = await prepare_checkpoint(
            service, business, session_b, run_b.id, path_b, user_b
        )
        first = await service.commit_compaction(cp_b, bl_b, prepared_position=cp_b.parent_id)
        assert first.created is True
        retry = await service.commit_compaction(cp_b, bl_b, prepared_position=cp_b.parent_id)
        assert retry.created is False and retry.credential_detected is False
        assert retry.entry.id == cp_b.id and retry.baseline.id == bl_b.id
        compactions = [e for e in await service.list_entries(session_b) if isinstance(e, CompactionEntry)]
        assert len(compactions) == 1
        assert (await service.get_session(session_b)).active_leaf_id == bl_b.id

        conflict_checkpoint = cp_b.model_copy(update={"summary": "不同的摘要"})
        try:
            await service.commit_compaction(
                conflict_checkpoint, bl_b, prepared_position=conflict_checkpoint.parent_id
            )
        except SessionConflict:
            pass
        else:
            raise AssertionError("同身份不同检查点内容未拒绝")

        conflict_baseline = build_business_context_entry(
            session_id=session_b, id=bl_b.id, parent_id=cp_b.id, run_id=run_b.id,
            details=BusinessContextSnapshot(
                mode="snapshot",
                request=RequestContext(timezone="Asia/Shanghai", business_date="2026-01-01", request_entry_id=user_b),
                message_nodes=[], pending_proposals=[],
            ),
            created_at=bl_b.created_at,
        )
        try:
            await service.commit_compaction(
                cp_b, conflict_baseline, prepared_position=cp_b.parent_id
            )
        except SessionConflict:
            pass
        else:
            raise AssertionError("同身份不同基线内容未拒绝")

        return {
            "illegal_rollback": True,
            "credential_rejected": outcome.credential_detected,
            "retry_reuses_baseline": retry.baseline.id == bl_b.id,
            "checkpoint_count": len(compactions),
        }
    finally:
        await database.close()


async def scenario_notification_order(root: Path) -> dict:
    database, service, _repo, business = await build_services(root / "notify.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "通知顺序" * 40)
        user_id = run.request_entry_id
        leaf = await append_delta(service, business, session_id, run.id, user_id, user_id)
        for _ in range(3):
            leaf = await append(service, session_id, run.id, leaf, assistant([], "stop", "长" * 400))
        position = {"id": leaf}

        events: list[tuple[str, dict]] = []

        async def emit(event):
            events.append((event["type"], dict(event)))

        async def compact(compaction_id, signal):
            projection = await service.get_projection(session_id, position["id"])
            preparation = C.prepare_compaction(projection.path, 128000, 16384, SPEC)
            system_state = get_current_system_message(projection.context.messages)
            checkpoint = CompactionEntry(
                session_id=session_id, id=compaction_id, parent_id=position["id"], run_id=run.id,
                type="compaction", summary="固定摘要",
                first_kept_entry_id=preparation.first_kept_entry_id, tokens_before=preparation.tokens_before,
                usage=USAGE, system_message=system_state, details=None, created_at=now_ms(),
            )
            baseline = await build_baseline(
                service, business, session_id, run.id, projection.path, checkpoint, user_id
            )
            outcome = await service.commit_compaction(
                checkpoint, baseline, prepared_position=checkpoint.parent_id
            )
            position["id"] = outcome.baseline.id
            refreshed = await service.get_projection(session_id, outcome.baseline.id)
            return LoopCompactionResult(
                entry_id=outcome.entry.id, parent_id=outcome.entry.parent_id,
                messages=refreshed.context.messages, baseline=outcome.baseline,
            )

        async def estimate_request():
            raise AssertionError("run_compaction 不应触发请求估算")

        hooks = CompactionHooks(
            estimate_request=estimate_request, compact=compact,
            context_window=128000, reserve_tokens=16384,
        )
        await run_compaction({"messages": [], "tools": {}}, hooks, None, emit, "threshold")
        kinds = [name for name, _ in events]
        assert kinds == ["compaction_start", "compaction_end", "custom_message"], kinds
        end = events[1][1]
        assert end["entry_id"] == end["compaction_id"]
        baseline_event = events[2][1]
        assert set(baseline_event) == {"type", "entry_id", "parent_id", "run_id", "created_at"}
        assert baseline_event["parent_id"] == end["entry_id"]
        raw = json.dumps(baseline_event, ensure_ascii=False)
        for leaked in ("<business_context>", "details", "display", "增肌", "content"):
            assert leaked not in raw, leaked
        return {"kinds": kinds, "baseline_parent": baseline_event["parent_id"]}
    finally:
        await database.close()


async def scenario_restart_and_edit(root: Path) -> dict:
    path = root / "restart.db"
    session_id = uid()
    database, service, _repo, business = await build_services(path)
    checkpoint_id = None
    baseline_id = None
    try:
        run = await start_run(service, session_id, "重启编辑" * 40)
        user_id = run.request_entry_id
        leaf = await append_delta(service, business, session_id, run.id, user_id, user_id)
        for _ in range(3):
            leaf = await append(service, session_id, run.id, leaf, assistant([], "stop", "旧长文" * 400))
        projection = await service.get_projection(session_id, leaf)
        checkpoint, baseline, _ = await commit_checkpoint(
            service, business, session_id, run.id, projection.path, user_id
        )
        checkpoint_id, baseline_id = checkpoint.id, baseline.id
        fixed_content = baseline.message.content
        # 结束运行：编辑入口要求空闲；重启读取不受运行状态影响。
        await service.finish_run(session_id, run.id, "completed")
    finally:
        await database.close()

    reopened = await open_database(path)
    restored = SessionService(SqliteSessionRepository(reopened))
    restored_business = BusinessService(
        SqliteBusinessRepository(reopened), Catalog.load(), restored, ReplacementCoordinator()
    )
    restored.attach_snapshots(restored_business)
    try:
        projection = await restored.get_projection(session_id, baseline_id)
        stored_baseline = next(e for e in projection.path if e.id == baseline_id)
        assert stored_baseline.message.content == fixed_content
        assert projection.context.source_entry_ids[-1] == baseline_id
        # 不补写旧历史：重启后节点集合稳定。
        all_entries = await restored.list_entries(session_id)
        assert sum(1 for e in all_entries if isinstance(e, CompactionEntry)) == 1
    finally:
        await reopened.close()

    # 编辑被摘要区间内的真实用户节点：删除目标子树连同检查点与新基线，旧业务引用不复活。
    database2, service2, _repo2, _b2 = await build_services(path)
    reopened2 = database2
    try:
        entries = await service2.list_entries(session_id)
        first_user = next(
            e for e in entries if isinstance(e, SessionMessageEntry) and e.messages[0].role == "user"
        )
        await service2.accept_edit(
            EditCommand(operation_id=uid(), session_id=session_id, request=EditRequest(target_entry_id=first_user.id, text="编辑后")),
        )
        after = {e.id for e in await service2.list_entries(session_id)}
        assert checkpoint_id not in after and baseline_id not in after
    finally:
        await reopened2.close()
    return {"checkpoint": checkpoint_id, "baseline": baseline_id}


async def scenario_consecutive_compaction(root: Path) -> dict:
    database, service, _repo, business = await build_services(root / "consecutive.db")
    try:
        session_id = uid()
        run, user_id, _assistant_id, leaf, proposal, _display = await stage_profile_branch(
            service, business, session_id, "连续压缩"
        )
        for _ in range(3):
            mid = await append(service, session_id, run.id, leaf, assistant([], "stop", "首轮长文" * 400))
            leaf = await append_delta(service, business, session_id, run.id, mid, user_id)

        first_path = (await service.get_projection(session_id, leaf)).path
        first_checkpoint, first_baseline, first = await commit_checkpoint(
            service, business, session_id, run.id, first_path, user_id
        )
        assert first.created and first.baseline.id == first_baseline.id
        assert (await service.get_run(session_id, run.id)).last_entry_id == first_baseline.id

        # 第一次压缩后继续追加真实消息与资料，触发第二次压缩。
        leaf2 = first_baseline.id
        for _ in range(3):
            mid = await append(service, session_id, run.id, leaf2, assistant([], "stop", "次轮长文" * 400))
            leaf2 = await append_delta(service, business, session_id, run.id, mid, user_id)

        second_path = (await service.get_projection(session_id, leaf2)).path
        second_checkpoint, second_baseline, second = await commit_checkpoint(
            service, business, session_id, run.id, second_path, user_id
        )
        assert second.created and second.baseline.id == second_baseline.id

        stored = {e.id: e for e in await service.list_entries(session_id)}
        compactions = [e for e in stored.values() if isinstance(e, CompactionEntry)]
        assert len(compactions) == 2
        assert isinstance(stored[second_baseline.id], BusinessContextEntry)
        assert stored[second_baseline.id].parent_id == second_checkpoint.id
        snapshot = stored[second_baseline.id].message.details
        assert isinstance(snapshot, BusinessContextSnapshot)
        node_ids = {n.entry_id for n in snapshot.message_nodes}
        # 每次压缩都生成新的完整基线：压缩节点、旧基线与摘要身份不进入消息来源。
        assert not ({first_checkpoint.id, second_checkpoint.id, first_baseline.id, second_baseline.id} & node_ids)
        assert all(isinstance(stored[i], SessionMessageEntry) for i in node_ids)
        # 被摘要的真实来源仍受控保留在待确认提案集合。
        assert proposal.proposal_id in {p.proposal_id for p in snapshot.pending_proposals}
        assert (await service.get_run(session_id, run.id)).last_entry_id == second_baseline.id
        assert (await service.get_session(session_id)).active_leaf_id == second_baseline.id
        leaf_projection = await service.get_projection(session_id, second_baseline.id)
        assert leaf_projection.context.source_entry_ids[-1] == second_baseline.id
        return {
            "first_baseline": first_baseline.id,
            "second_baseline": second_baseline.id,
            "compactions": [e.id for e in compactions],
        }
    finally:
        await database.close()


async def scenario_post_compaction_confirmation(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "confirm.db")
    confirmations = ConfirmationService(repository, service, business)
    try:
        session_id = uid()
        run, user_id, _assistant_id, leaf, proposal, display = await stage_profile_branch(
            service, business, session_id, "压缩后独立确认"
        )
        for _ in range(3):
            mid = await append(service, session_id, run.id, leaf, assistant([], "stop", "确认前长文" * 400))
            leaf = await append_delta(service, business, session_id, run.id, mid, user_id)

        # 压缩：请求、来源与展示节点进入被摘要范围，新基线只作受控读取与恢复依据。
        path = (await service.get_projection(session_id, leaf)).path
        _, baseline, outcome = await commit_checkpoint(service, business, session_id, run.id, path, user_id)
        assert outcome.created
        # 压缩后才结束运行；用户随后点击独立确认按钮。
        await service.finish_run(session_id, run.id, "completed")

        # 越权：错误展示节点在受理事务内被拒绝，业务数据零提交。
        try:
            await confirmations.confirm(
                session_id,
                SaveConfirmationRequest(
                    operation_id=uid(), business_kind="profile",
                    proposal_id=proposal.proposal_id, display_entry_id=uid(),
                ),
            )
        except BusinessError:
            pass
        else:
            raise AssertionError("压缩后错误展示节点未拒绝")
        assert (await business.get_profile()).version is None

        # 独立按钮确认：压缩与新基线不创建授权，被摘要来源仍可核验，真实确认保存一次。
        saved = await confirmations.confirm(
            session_id,
            SaveConfirmationRequest(
                operation_id=uid(), business_kind="profile",
                proposal_id=proposal.proposal_id, display_entry_id=display,
            ),
        )
        assert saved.status == "saved", saved.status
        profile = await business.get_profile()
        assert profile.version == 1
        return {"status": saved.status, "version": profile.version, "baseline": baseline.id}
    finally:
        await database.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        result = {
            "summary_input": asyncio.run(scenario_summary_input_and_cut(root)),
            "atomic_baseline": asyncio.run(scenario_atomic_baseline(root)),
            "atomic_failure": asyncio.run(scenario_atomic_failure_and_identity(root)),
            "notification": asyncio.run(scenario_notification_order(root)),
            "restart_edit": asyncio.run(scenario_restart_and_edit(root)),
            "consecutive": asyncio.run(scenario_consecutive_compaction(root)),
            "confirmation": asyncio.run(scenario_post_compaction_confirmation(root)),
        }
    (EVIDENCE / "business-context-compaction.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        "PASS: 摘要输入与保留预算纳入资料、资料非切点、压缩新基线完整快照与受控读取、"
        "检查点与基线同事务原子提交/失败回滚/凭据拒绝/幂等、运行与叶指向新基线、"
        "通知顺序 compaction_end→基线 custom_message 与五字段隐私、重启固定内容一致、编辑清除旧引用、"
        "连续压缩强制第二次完整基线与受控提案保留、压缩后独立按钮确认成功且越权零提交"
    )


if __name__ == "__main__":
    check()
