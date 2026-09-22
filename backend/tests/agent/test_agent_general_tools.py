# ST-03 General 路径：五项会话 Intent 的白名单、统一 ``message ＋ ui_actions`` 与零写入边界。
# 依据：ST-03 subtask 的范围 3／4／5 与验收标准；白名单外的工具不可见，未确认的打卡候选不写库。
# 事实用 tmp_path 下的真实迁移库与真实 ToolNode，模型是可脚本化的固定替身。

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

from app.application.agent.contracts import (
    LoadedSkill,
    SkillMetadata,
    SkillReference,
)
from app.application.agent.harness.tools.general import (
    GENERAL_INTENT_SKILLS,
    GENERAL_INTENT_TOOLS,
    GENERAL_SKILL_NAMES,
    WORKOUT_FORM_FIELDS,
    general_skill_bundle,
)
from app.application.agent.prompts import (
    FORM_RECORD_GUIDE,
    NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
)
from app.application.agent.router import NON_PLAN_INTENTS, FitnessIntent
from app.application.agent.run_service import _general_branch
from tests.agent.test_agent_run_branches import (
    ANSWER,
    SCHEDULED_ON,
    SUMMARY,
    _extraction_scripts,
    _harness,
    _route,
    _row_counts,
    final_answer,
    tool_call,
)
from tests.agent.test_agent_tool_branches import (
    _route_schedule,
)

REQUEST_TEXTS: Mapping[str, str] = {
    "form_record": "打开打卡表单",
    "natural_language_record": "记录今天做8个引体",
    "view_schedule": "今天训练什么？",
    "view_progress": "我的进展怎么样？",
    "general": "减载是什么？",
}


def test_the_five_conversation_intents_have_exactly_their_approved_tools() -> None:
    """白名单表与设计矩阵逐项一致：每项 Intent 只见它批准的只读 Tool。"""
    mounted = {
        intent: tuple(tool.name for tool in tools)
        for intent, tools in GENERAL_INTENT_TOOLS.items()
    }

    assert mounted == {
        "form_record": ("get_workout_record_form",),
        "natural_language_record": ("prepare_workout_record", "search_exercises"),
        "view_schedule": ("read_active_plan", "read_training_calendar"),
        "view_progress": ("read_progress", "read_training_history"),
        "general": ("search_exercises", "read_training_history", "read_active_plan"),
    }
    assert set(mounted) == set(NON_PLAN_INTENTS)
    assert "create_workout_record" not in {name for names in mounted.values() for name in names}


async def test_a_tool_outside_the_intent_whitelist_is_not_mounted(tmp_path: Path) -> None:
    """``view_schedule`` 下模型点名打卡 Tool：未挂载即参数错误，业务库行数不变。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[
            tool_call("prepare_workout_record", {"request": "今天深蹲 60 公斤"}),
            final_answer(ANSWER),
        ],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke(REQUEST_TEXTS["view_schedule"])
        tool_messages = [
            message
            for message in h.model.harness_calls[1].messages
            if isinstance(message, ToolMessage)
        ]

        assert result.messages == (ANSWER,)
        assert [message.status for message in tool_messages] == ["error"]
        assert "prepare_workout_record" in str(tool_messages[0].content)
        assert await _row_counts(h.db) == before


async def test_form_record_answers_with_the_typed_workout_form_action(
    tmp_path: Path,
) -> None:
    """``form_record``：经 general_tools 取表单，给出 ``workout_form`` 动作，不写库。"""
    async with _harness(
        tmp_path,
        structured=_route(
            domain="workout_execution", action="create", execution_type="form_record"
        ),
        harness=[tool_call("get_workout_record_form"), final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        outcome = await _general_outcome(h, "form_record", REQUEST_TEXTS["form_record"])

        assert outcome.message == ANSWER
        action = _single_action(outcome)
        assert action["type"] == "workout_form"
        assert action["form"] == "workout_record"
        assert action["fields"] == list(WORKOUT_FORM_FIELDS)
        assert action["guide"] == FORM_RECORD_GUIDE
        assert h.model.harness_calls[0].offered == ("get_workout_record_form",)
        assert h.model.text_calls() == []
        assert await _row_counts(h.db) == before


async def test_view_progress_answers_with_the_typed_progress_view_action(
    tmp_path: Path,
) -> None:
    """``view_progress``：只读事实按 ``progress_view`` 动作下行，业务库行数不变。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="analytics", action="query"),
        harness=[tool_call("read_progress"), final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        outcome = await _general_outcome(h, "view_progress", REQUEST_TEXTS["view_progress"])
        action = _single_action(outcome)

        assert outcome.message == ANSWER
        assert action["type"] == "progress_view"
        assert [fact["tool"] for fact in action["facts"]] == ["read_progress"]
        assert action["facts"][0]["result"]["business_day"] == SCHEDULED_ON
        assert await _row_counts(h.db) == before


async def test_view_schedule_answers_with_the_typed_schedule_view_action(
    tmp_path: Path,
) -> None:
    """``view_schedule``：读到的计划事实按 ``schedule_view`` 动作下行。"""
    async with _harness(
        tmp_path,
        structured=_route_schedule(),
        harness=[tool_call("read_active_plan"), final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        outcome = await _general_outcome(h, "view_schedule", REQUEST_TEXTS["view_schedule"])
        action = _single_action(outcome)

        assert outcome.message == ANSWER
        assert action["type"] == "schedule_view"
        assert [fact["tool"] for fact in action["facts"]] == ["read_active_plan"]
        assert await _row_counts(h.db) == before


async def test_general_chat_answers_without_ui_actions_or_writes(tmp_path: Path) -> None:
    """``general``：一次 harness 答复，只读白名单已挂载但不强制调用，不发 ``ui_actions``。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="general", action="chat"),
        harness=[final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        events = await h.stream(REQUEST_TEXTS["general"])

        assert [event.event for event in events] == [
            "node",
            "node",
            "node",
            "message",
            "done",
        ]
        assert h.model.harness_calls[0].offered == (
            "search_exercises",
            "read_training_history",
            "read_active_plan",
        )
        assert await _row_counts(h.db) == before


async def test_natural_language_record_waits_for_confirmation_before_any_write(
    tmp_path: Path,
) -> None:
    """自然语言打卡：候选与确认等待事件齐备，确认前业务训练表零写入。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [
                {
                    "domain": "workout_execution",
                    "action": "create",
                    "execution_type": "natural_language_record",
                }
            ],
            **_extraction_scripts(),
        },
        text={NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [SUMMARY]},
    ) as h:
        before = await _row_counts(h.db)
        events = await h.stream(REQUEST_TEXTS["natural_language_record"])
        action = _waiting_action(events)

        assert action["type"] == "workout_confirmation"
        assert action["workout"]["performed_on"] == SCHEDULED_ON
        assert action["workout"]["sets"][0]["exercise_id"] == "pull-up"
        assert action["candidate_plan_sessions"] == []
        assert await _row_counts(h.db) == before
        assert before["workout_sessions"] == 0


async def test_safety_hit_produces_no_ui_actions_and_no_tool_calls(tmp_path: Path) -> None:
    """红旗命中：General 与 Tool 调用次数为 0，且不发任何 ``ui_actions``。"""
    async with _harness(tmp_path) as h:
        before = await _row_counts(h.db)
        events = await h.stream("训练时胸部异常不适，我还能继续吗？")

        assert [event.event for event in events] == [
            "node",
            "node",
            "message",
            "done",
        ]
        assert h.model.text_calls() == []
        assert h.model.harness_calls == []
        assert await _row_counts(h.db) == before


def test_general_skill_bundle_is_scoped_by_intent() -> None:
    """General 只装载本次 Intent 需要的 Skill 正文与 references。"""
    source = _FakeSkillSource()

    bundle = general_skill_bundle(source, "general")
    assert bundle.names == GENERAL_INTENT_SKILLS["general"]
    assert bundle.references == tuple(f"{name}-reference" for name in bundle.names)
    assert all(f"{name}-body" in bundle.system_instructions for name in bundle.names)

    logging = general_skill_bundle(source, "natural_language_record")
    assert logging.names == ("workout-logging",)
    assert GENERAL_SKILL_NAMES == (
        "workout-logging",
        "strength-training",
        "fitness-knowledge",
        "exercise-guidance",
    )


class _FakeSkillSource:
    """Skill 来源替身：按名给出正文与一条 reference。"""

    def load(self, name: str) -> LoadedSkill:
        return LoadedSkill(
            metadata=SkillMetadata(name=name, description=f"{name}-description"),
            body=f"{name}-body",
            references=(
                SkillReference(path="references/x.md", text=f"{name}-reference"),
            ),
        )


def _single_action(outcome: Any) -> dict:
    """通用结果里的唯一 ``ui_actions`` 项。"""
    actions = list(outcome.actions)
    assert len(actions) == 1, actions
    return actions[0]


async def _general_outcome(h: Any, intent: str, request: str) -> Any:
    """直接取 General 分支的输出契约（``message ＋ ui_actions``），不重复跑一遍事件流。"""
    return await _general_branch(
        intent, {"request": request}, h.run_context(), deps=h.run_deps
    )


def _waiting_action(events: Sequence) -> dict:
    """等待类事件里唯一的 ``ui_actions`` 动作；数量不为 1 即断言失败。"""
    actions = [event.data for event in events if event.event == "waiting"]
    assert len(actions) == 1, actions
    return actions[0]
