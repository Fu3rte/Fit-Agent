import json

from app.agent.tools.workouts import workout_tool_declarations
from app.domain.business.models import WorkoutProposalArguments
from app.interfaces.http import app
from test.check_workout_agent_http import (
    ROOT,
    confirm,
    listing,
    pending,
    session,
    successful,
    turn,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)


def prepared(outputs):
    proposals = successful(outputs, "prepare_workout")
    assert proposals, outputs
    return proposals[-1]


def main():
    # Schema 检查独立于后续真实模型行为验证。
    schema = WorkoutProposalArguments.model_json_schema()
    notes = schema["$defs"]["WorkoutContent"]["properties"]["notes"]
    assert "notes" in schema["$defs"]["WorkoutContent"]["required"]
    assert {item["type"] for item in notes["anyOf"]} == {"string", "null"}
    description = notes["description"]
    tool = next(item for item in workout_tool_declarations() if item.name == "prepare_workout")
    assert description in tool.description

    install_test_model_config()
    database = patch_default_database("workout-notes-live")
    evidence = {"database": str(database), "schema": notes, "proposals": []}
    with Server(app) as server, client(server.base_url) as http:
        identity = session(http)
        _, outputs = turn(
            http, identity,
            "记录昨天的实际训练：俯卧撑两组，每组10次。请整理训练记录给我确认。",
        )
        initial = prepared(outputs)
        assert initial["payload"]["notes"] is None, initial
        assert listing(http)["total"] == 0
        evidence["proposals"].append(initial)
        saved = confirm(http, identity, pending(http, identity))
        assert saved.status_code == 200 and saved.json()["status"] == "saved", saved.text
        first = saved.json()["result"]
        assert first["content"]["notes"] is None
        print("PASS: real model omitted feedback -> JSON null; HTTP confirmation preserves null", flush=True)

        feedback = "最后一组很吃力，右肩有点不适"
        _, outputs = turn(
            http, identity,
            f"补充{first['performed_on']}那次训练的感受：{feedback}。动作和组次保持原样，请整理给我确认。",
        )
        with_feedback = prepared(outputs)
        assert with_feedback["base_workout_id"] == first["id"]
        assert with_feedback["base_workout_version"] == 1
        assert with_feedback["payload"]["notes"] in {feedback, feedback + "。"}, with_feedback
        assert with_feedback["payload"]["exercises"] == first["content"]["exercises"]
        evidence["proposals"].append(with_feedback)
        saved = confirm(http, identity, pending(http, identity))
        assert saved.status_code == 200 and saved.json()["status"] == "saved", saved.text
        second = saved.json()["result"]
        assert second["content"]["notes"] == with_feedback["payload"]["notes"]
        print("PASS: real model explicit effort/discomfort -> user feedback only", flush=True)

        other = session(http)
        _, outputs = turn(
            http, other,
            f"更正{first['performed_on']}的实际训练：俯卧撑是两组，每组12次。请整理更新记录给我确认。",
        )
        updated = prepared(outputs)
        assert updated["base_workout_id"] == first["id"]
        assert updated["base_workout_version"] == 2
        assert updated["payload"]["notes"] == second["content"]["notes"], updated
        assert [item["reps"] for item in updated["payload"]["exercises"][0]["sets"]] == [12, 12]
        evidence["proposals"].append(updated)
        saved = confirm(http, other, pending(http, other))
        assert saved.status_code == 200 and saved.json()["status"] == "saved", saved.text
        final = saved.json()["result"]
        assert final["version"] == 3 and final["content"]["notes"] == second["content"]["notes"]
        evidence["saved"] = final
        print("PASS: real model same-day cross-session update retains untouched feedback", flush=True)
    path = ROOT / "notes-evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: WorkoutContent.notes schema/tool and real model Agent/HTTP regression:", path)


if __name__ == "__main__":
    main()
