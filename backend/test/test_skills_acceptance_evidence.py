from pathlib import Path

from test.check_skills_acceptance import (
    banned_user_terms,
    continuation_offset,
    endpoint_defined,
    evidence_answer_digest,
    evidence_run_ids,
    finish,
    last_file_line,
    numeric_prescription_terms,
    plan_payload_issues,
    read_claim,
    read_failure_stated,
    read_records,
    seeded_plan_matches,
    skill_path_is_normalized,
    system_load_boundary,
    tool_records,
    uncertainty_stated,
    workout_sets_match,
)


def test_skill_path_normalization_rejects_unresolved_traversal():
    assert skill_path_is_normalized("skills/workout-planning/SKILL.md")
    assert skill_path_is_normalized("skills\\workout-planning\\SKILL.md")
    assert not skill_path_is_normalized("../training-theory/references/methodology.md")
    assert not skill_path_is_normalized(
        "skills/../training-theory/references/methodology.md"
    )
    assert not skill_path_is_normalized("sessions/example/workspace/note.md")
    assert not skill_path_is_normalized(None)


def test_continuation_offset_reads_tool_hint():
    text = (
        "1: ---\n\n[仅显示到第 10 行，文件仍有后续内容；"
        "继续读取请使用 offset=11]"
    )
    assert continuation_offset(text) == 11
    assert continuation_offset("完整内容") is None
    assert continuation_offset(None) is None


def test_banned_user_terms_match_only_biological_names():
    assert banned_user_terms("ATP 与糖酵解") == ["ATP", "糖酵解"]
    assert banned_user_terms("乳酸不是训练效果的读数") == []


def test_numeric_prescription_detects_dose_contexts():
    assert numeric_prescription_terms("保留 1-3 次的余力")
    assert numeric_prescription_terms("先加 1–2 次")
    assert numeric_prescription_terms("留 1–2 次的余量")
    assert numeric_prescription_terms("建议每周练2次，每次3组，每组8次。")
    assert numeric_prescription_terms("每周 2 次")
    assert numeric_prescription_terms("酸痛高峰在 24—72 小时") == []
    assert numeric_prescription_terms("都是做 40 次，分成 5×8") == []
    assert numeric_prescription_terms("如果你还能完成 1–3 次") == []
    assert numeric_prescription_terms("连续测 2–3 次") == []


def test_system_load_boundary_rejects_multi_factor_only():
    assert system_load_boundary("系统不会用这个信号单独判定你的训练负荷。")
    assert system_load_boundary(
        "是否需要接近力竭，应该由长期终点变化来调整，而不是由当次的疲劳感来决定。"
    )
    assert not system_load_boundary(
        "它并不是唯一影响进展的变量；动作选择、总组数、每次重复的负荷、休息时长同样重要。"
    )
    assert not system_load_boundary("它们不能单独用来判断是否有效。")
    assert not system_load_boundary("这个信号很好用，但训练负荷由计划流程决定。")


def _evidence_with_checks(answer: str = "回答") -> dict:
    return {
        "checks": [{"name": "示例检查", "passed": True}],
        "turns": [{"run_id": "run-1", "final_answer": answer}],
        "failure": None,
    }


def test_finish_never_passes_with_failure_or_missing_review():
    evidence = _evidence_with_checks()
    finish(evidence)
    assert evidence["status"] == "pending_review"
    assert evidence["manual_review_error"] == "missing"

    evidence["manual_review"] = {
        "status": "pass",
        "run_ids": evidence_run_ids(evidence),
        "answer_sha256": evidence_answer_digest(evidence),
    }
    finish(evidence)
    assert evidence["status"] == "pass"

    evidence["failure"] = {"type": "RuntimeError", "message": "boom"}
    finish(evidence)
    assert evidence["status"] == "fail"


def test_finish_rejects_review_bound_to_other_run_or_answer():
    evidence = _evidence_with_checks()
    evidence["manual_review"] = {
        "status": "pass",
        "run_ids": ["other-run"],
        "answer_sha256": evidence_answer_digest(evidence),
    }
    finish(evidence)
    assert evidence["status"] == "fail"
    assert evidence["manual_review_error"] == "run_ids_mismatch"

    evidence["manual_review"] = {
        "status": "pass",
        "run_ids": evidence_run_ids(evidence),
        "answer_sha256": "wrong",
    }
    finish(evidence)
    assert evidence["status"] == "fail"
    assert evidence["manual_review_error"] == "answer_sha256_mismatch"


def test_endpoint_and_uncertainty_statements_are_required():
    assert endpoint_defined("终点是同一动作、同一标准下能重复测到的次数。")
    assert not endpoint_defined("提高次数本质上是增加训练量。")
    assert uncertainty_stated("现在还无法确认你的恢复能力是否跟得上。")
    assert uncertainty_stated("但不能保证一定涨次数。")
    assert not uncertainty_stated("按这个顺序做就行。")


def test_read_claim_and_failure_markers_are_exclusive():
    assert read_claim("已成功读取该文件")
    assert not read_claim("读取失败，无法读取该文件")
    assert read_failure_stated("该文件不存在，读取失败")
    assert not read_failure_stated("文件内容如下")


def test_last_file_line_skips_trailing_blank_lines(tmp_path: Path):
    target = tmp_path / "sample.md"
    target.write_text("第一行\n\n最后一行\n\n", encoding="utf-8")
    assert last_file_line(target) == "最后一行"


def test_tool_records_pairs_start_and_execution_end():
    wire = [
        {
            "event": "tool_start",
            "data": {"tool_call_id": "call-1", "name": "read", "arguments": {"path": "skills/a/SKILL.md"}},
        },
        {
            "event": "tool_execution_end",
            "data": {
                "tool_call_id": "call-1",
                "name": "read",
                "content": "1: ---",
                "is_error": False,
            },
        },
    ]
    records = tool_records(wire)
    assert records == [
        {
            "call_id": "call-1",
            "name": "read",
            "arguments": {"path": "skills/a/SKILL.md"},
            "is_error": False,
            "content": "1: ---",
        }
    ]


def test_read_records_extracts_continuation_and_arguments():
    tools = [
        {
            "name": "read",
            "arguments": {"path": "skills/a/SKILL.md", "offset": 1, "limit": 10},
            "is_error": False,
            "content": "1: ---\n继续读取请使用 offset=11",
        },
        {
            "name": "write",
            "arguments": {"path": "sessions/x/workspace/a.md"},
            "is_error": False,
            "content": "已写入",
        },
    ]
    records = read_records(tools)
    assert len(records) == 1
    assert records[0]["path"] == "skills/a/SKILL.md"
    assert records[0]["offset"] == 1
    assert records[0]["limit"] == 10
    assert records[0]["continuation_offset"] == 11


def test_plan_payload_issues_detects_incomplete_training_days():
    valid = {
        "days": [
            {
                "kind": "training",
                "exercises": [
                    {
                        "sets": 3,
                        "reps": 8,
                        "duration_seconds": None,
                        "weight_kg": 20.0,
                        "load_convention": "barbell_total",
                    }
                ],
            },
            {"kind": "rest", "exercises": []},
        ]
    }
    assert plan_payload_issues(valid) == []

    rest_with_exercise = {
        "days": [{"kind": "rest", "exercises": [{"sets": 3, "reps": 8}]}]
    }
    assert any("休息日" in item for item in plan_payload_issues(rest_with_exercise))

    missing_sets = {
        "days": [
            {
                "kind": "training",
                "exercises": [{"sets": None, "reps": None, "duration_seconds": None}],
            }
        ]
    }
    issues = plan_payload_issues(missing_sets)
    assert any("sets" in item for item in issues)
    assert any("reps" in item for item in issues)

    missing_convention = {
        "days": [
            {
                "kind": "training",
                "exercises": [
                    {"sets": 3, "reps": 8, "duration_seconds": None, "weight_kg": 20.0}
                ],
            }
        ]
    }
    assert any("口径" in item for item in plan_payload_issues(missing_convention))


def test_workout_sets_match_preserves_actual_reps_and_weight():
    exercise = {
        "sets": [
            {"reps": 8, "weight_kg": 60.0},
            {"reps": 8, "weight_kg": 62.5},
            {"reps": 6, "weight_kg": 65.0},
        ]
    }
    assert workout_sets_match(exercise, [(8, 60.0), (8, 62.5), (6, 65.0)])
    assert not workout_sets_match(exercise, [(8, 60.0), (8, 62.5), (6, 70.0)])
    assert not workout_sets_match(exercise, [(8, 60.0)])


def test_seeded_plan_matches_ignores_target_sets_but_rejects_other_changes():
    from test.check_skills_acceptance import seeded_plan

    content = seeded_plan()["content"]
    assert seeded_plan_matches(content, content) == []

    modified = {
        **content,
        "days": [dict(day) for day in content["days"]],
    }
    modified["days"][0] = {
        **modified["days"][0],
        "exercises": [dict(item) for item in modified["days"][0]["exercises"]],
    }
    modified["days"][0]["exercises"][0]["sets"] = 5
    assert seeded_plan_matches(modified, content) == []

    modified["days"][0]["exercises"][0]["reps"] = 12
    assert seeded_plan_matches(modified, content)
