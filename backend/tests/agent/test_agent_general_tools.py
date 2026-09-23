# ST-03 General 路径：五项会话 Intent 的白名单、统一 ``message ＋ ui_actions`` 与零写入边界。
# 依据：ST-03 subtask 的范围 3／4／5 与验收标准；白名单外的工具不可见，未确认的打卡候选不写库。
# 事实用 tmp_path 下的真实迁移库与真实 ToolNode，模型是可脚本化的固定替身。

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from app.application.agent.contracts import (
    LOCAL_USER_ID,
)
from app.application.agent.harness.registry import (
    GENERAL_INTENT_SKILLS,
    GENERAL_INTENT_TOOLS,
    general_skill_metadata,
)
from app.application.agent.harness.tools.common import WORKOUT_FORM_FIELDS
from app.application.agent.harness.tools.prepare_workout_record import (
    ExtractedWorkout,
    MissingWorkoutRecordCandidate,
)
from app.application.agent.prompts import (
    FORM_RECORD_GUIDE,
    NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT,
)
from app.application.agent.router import NON_PLAN_INTENTS
from app.application.agent.run_service import (
    MISSING_WORKOUT_RECORD_CANDIDATE_MESSAGE,
    _general_branch,
    agent_run_error_message,
)
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir
from tests.agent.test_agent_run_branches import (
    ANSWER,
    PULL_UP,
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
        call = h.model.harness_calls[0]
        assert call.offered[:3] == (
            "search_exercises",
            "read_training_history",
            "read_active_plan",
        )
        prompt = str(call.messages[0].content)
        assert "fitness-knowledge:" in prompt
        assert "exercise-guidance:" in prompt
        assert "strength-training:" in prompt
        assert "# fitness-knowledge：一般训练知识 Skill" not in prompt
        assert "## 职责" not in prompt
        assert await _row_counts(h.db) == before


def _record_route() -> dict[type, list[Mapping[str, Any]]]:
    """一次自然语言打卡请求的路由响应。"""
    return _route(
        domain="workout_execution",
        action="create",
        execution_type="natural_language_record",
    )


async def test_general_reads_skill_body_and_requested_reference_on_demand(
    tmp_path: Path,
) -> None:
    body = SkillLoader(skills_dir()).read_skill("fitness-knowledge")
    reference = SkillLoader(skills_dir()).read_reference(
        "fitness-knowledge", "references/knowledge-boundaries.md"
    ).text
    async with _harness(
        tmp_path,
        structured=_route(domain="general", action="chat"),
        harness=[
            tool_call("read_skill", {"skill_name": "fitness-knowledge"}),
            tool_call(
                "read_skill_reference",
                {
                    "skill_name": "fitness-knowledge",
                    "relative_path": "references/knowledge-boundaries.md",
                },
                call_id="call-2",
            ),
            final_answer(ANSWER),
        ],
    ) as h:
        result = await h.invoke(REQUEST_TEXTS["general"])

        assert result.messages == (ANSWER,)
        assert len(h.model.harness_calls) == 3
        for index, expected in ((1, body), (2, reference)):
            messages = h.model.harness_calls[index].messages
            assert expected not in str(messages[0].content)
            assert sum(expected in str(message.content) for message in messages) == 1
        final_history = h.model.harness_calls[-1].messages
        assert sum(body in str(message.content) for message in final_history) == 1
        assert sum(reference in str(message.content) for message in final_history) == 1


async def test_natural_language_record_waits_for_confirmation_before_any_write(
    tmp_path: Path,
) -> None:
    """自然语言打卡：唯一一次成功 prepare_workout_record 出候选与等待事件，确认前业务训练表零写入。"""
    async with _harness(
        tmp_path,
        structured={**_record_route(), **_extraction_scripts()},
        harness=[
            tool_call(
                "prepare_workout_record",
                {"request": REQUEST_TEXTS["natural_language_record"]},
            ),
            final_answer(SUMMARY),
        ],
    ) as h:
        before = await _row_counts(h.db)
        events = await h.stream(REQUEST_TEXTS["natural_language_record"])
        action = _waiting_action(events)
        call = h.model.harness_calls[0]

        assert [event.event for event in events] == [
            "node",
            "node",
            "node",
            "message",
            "waiting",
            "done",
        ]
        assert [
            event.data["text"] for event in events if event.event == "message"
        ] == [SUMMARY]
        assert call.offered[:2] == ("prepare_workout_record", "search_exercises")
        assert call.offered[2:] == ("read_skill", "read_skill_reference")
        system_prompt = str(call.messages[0].content)
        assert system_prompt.startswith(
            NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT.format(business_day=SCHEDULED_ON)
        )
        assert "workout-logging" in system_prompt
        assert action["type"] == "workout_confirmation"
        assert action["workout"]["performed_on"] == SCHEDULED_ON
        assert action["workout"]["sets"][0]["exercise_id"] == PULL_UP
        assert action["candidate_plan_sessions"] == []
        assert await _row_counts(h.db) == before
        assert before["workout_sessions"] == 0


async def test_natural_language_record_domain_error_keeps_its_text_without_actions(
    tmp_path: Path,
) -> None:
    """领域校验失败：Tool 内的领域错误沿错误边界回到可见文本，不发确认动作、不写库。"""
    async with _harness(
        tmp_path,
        structured={
            **_record_route(),
            ExtractedWorkout: [
                {
                    "performed_on": SCHEDULED_ON,
                    "sets": [
                        {
                            "exercise_id": PULL_UP,
                            "set_no": 0,
                            "set_type": "work",
                            "reps": 8,
                        }
                    ],
                }
            ],
        },
        harness=[
            tool_call(
                "prepare_workout_record",
                {"request": REQUEST_TEXTS["natural_language_record"]},
            ),
            final_answer(ANSWER),
        ],
    ) as h:
        before = await _row_counts(h.db)
        events = await h.stream(REQUEST_TEXTS["natural_language_record"])

        assert [event.event for event in events] == [
            "node",
            "node",
            "node",
            "message",
            "done",
        ]
        assert events[3].data["text"].startswith("自然语言打卡未通过校验：")
        assert await _row_counts(h.db) == before


async def test_natural_language_record_without_a_successful_call_fails_in_place(
    tmp_path: Path,
) -> None:
    """模型没成功调用 prepare_workout_record：本地明确失败，不发任何误导性确认动作。"""
    async with _harness(
        tmp_path,
        structured=_record_route(),
        harness=[final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)

        with pytest.raises(MissingWorkoutRecordCandidate):
            await h.stream(REQUEST_TEXTS["natural_language_record"])

        assert await _row_counts(h.db) == before


def test_missing_workout_record_candidate_has_its_fixed_visible_text() -> None:
    """缺打卡候选沿 Run 错误边界给出固定可见文案。"""
    assert agent_run_error_message(MissingWorkoutRecordCandidate("没有成功调用")) == (
        MISSING_WORKOUT_RECORD_CANDIDATE_MESSAGE
    )


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


def test_general_skill_metadata_is_scoped_by_intent() -> None:
    """General 每次只呈现当前 Intent 授权的 Skill 元数据。"""
    source = SkillLoader(skills_dir())

    general = general_skill_metadata(source, "general")
    assert tuple(item.name for item in general) == GENERAL_INTENT_SKILLS["general"]
    assert all(item.description for item in general)
    logging = general_skill_metadata(source, "natural_language_record")
    assert tuple(item.name for item in logging) == ("workout-logging",)
    names = {name for names in GENERAL_INTENT_SKILLS.values() for name in names}
    assert names == {
        "workout-logging",
        "strength-training",
        "fitness-knowledge",
        "exercise-guidance",
    }


def _single_action(outcome: Any) -> dict:
    """通用结果里的唯一 ``ui_actions`` 项。"""
    actions = list(outcome.actions)
    assert len(actions) == 1, actions
    return actions[0]


async def _general_outcome(h: Any, intent: str, request: str) -> Any:
    """直接取 General 分支的输出契约（``message ＋ ui_actions``），不重复跑一遍事件流。"""
    return await _general_branch(
        intent,
        {"request": request, "user_id": LOCAL_USER_ID, "run_id": "general-branch-run"},
        h.run_context(),
        deps=h.run_deps,
    )


def _waiting_action(events: Sequence) -> dict:
    """等待类事件里唯一的 ``ui_actions`` 动作；数量不为 1 即断言失败。"""
    actions = [event.data for event in events if event.event == "waiting"]
    assert len(actions) == 1, actions
    return actions[0]
