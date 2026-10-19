import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.application.business.catalog import Catalog  # noqa: E402
from app.model_config import load_model_config  # noqa: E402
from evaluation.contracts import (  # noqa: E402
    AnswerEvidence,
    CheckpointResult,
    ContextItem,
    EvaluationRun,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationReference,
    MetricApplicability,
    ModelInputContext,
    RunCoverage,
    RunIdentity,
    ScenarioResult,
    UserInputEvidence,
)

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

from evaluation.generation_scoring import (  # noqa: E402
    GenerationScoringSession,
    JudgeConfig,
    failed_outcome,
    load_judge_model,
    score_checkpoint_with_evidence,
)

ENABLE_VARIABLE = "FIT_AGENT_GENERATION_SCORING_INTEGRATION"
OUTPUT_ROOT = BACKEND / "temp" / "generation-scoring"
CATALOG_EXERCISE_ID = "0001"


def _catalog_exercise():
    exercise = Catalog.load().get(CATALOG_EXERCISE_ID)
    if exercise is None:
        raise RuntimeError(f"真实动作目录缺少样例 {CATALOG_EXERCISE_ID}")
    return exercise


def build_checkpoint():
    exercise = _catalog_exercise()
    message = f"动作目录里的 {exercise.name} 使用什么器械？"
    answer = f"{exercise.name} 使用 {exercise.equipment}。"
    tool_fact = (
        f"动作 {exercise.id} 是 {exercise.name}，"
        f"器械：{exercise.equipment}，目标肌群：{exercise.target}。"
    )
    return GenerationCheckpoint(
        checkpoint=EvidenceLink(
            run_id="generation-scoring-integration",
            scenario_id="s-generation-integration",
            event_id="msg-1",
            checkpoint_id="cp-1",
        ),
        user_input=UserInputEvidence(message=message),
        answer=AnswerEvidence(visible_text=answer),
        model_input=ModelInputContext(
            items=[
                ContextItem(source="system_prompt", content="你是训练助手。", fact_origin="system"),
                ContextItem(source="user_message", content=message, fact_origin="user"),
                ContextItem(
                    source="tool_result",
                    content=tool_fact,
                    fact_origin="tool",
                    tool_call_id="call-1",
                    tool_name="search_exercises",
                ),
            ]
        ),
        reference=GenerationReference(answer=answer),
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=True),
            MetricApplicability(metric="factual_correctness", applicable=True),
        ],
    )


def verify_catalog_sample(checkpoint: GenerationCheckpoint) -> dict[str, str]:
    exercise = _catalog_exercise()
    tool_items = [
        item for item in checkpoint.model_input.items if item.fact_origin == "tool"
    ]
    if len(tool_items) != 1:
        raise RuntimeError("集成样例必须包含一条工具事实")
    fact = tool_items[0].content
    for value in (exercise.id, exercise.name, exercise.equipment, exercise.target):
        if value not in fact:
            raise RuntimeError(f"工具事实缺少真实目录字段: {value}")
    if exercise.equipment not in checkpoint.answer.visible_text:
        raise RuntimeError("回答与真实目录器械不一致")
    if exercise.equipment not in checkpoint.reference.answer:
        raise RuntimeError("reference 与真实目录器械不一致")
    return {
        "id": exercise.id,
        "name": exercise.name,
        "equipment": exercise.equipment,
        "target": exercise.target,
    }


def load_judge_config() -> JudgeConfig:
    load_dotenv(BACKEND / ".env", override=False)
    model = load_judge_model(os.environ)
    # 受保护模型配置提供当前模型服务的地址与凭据；裁判模型名由 FIT_AGENT_EVAL_JUDGE_MODEL 指定。
    # 是否支持 step-5-preview 由本次真实调用验证，不在此处猜测或改写服务地址。
    config = load_model_config()
    return JudgeConfig(
        model=model,
        base_url=config.base_url,
        api_key=config.api_key,
    )


def run_identity(checkpoint_id: str) -> RunIdentity:
    return RunIdentity(
        run_id="generation-scoring-integration",
        dataset={
            "version": "integration",
            "manifest_sha256": "0" * 64,
            "content_sha256": "0" * 64,
            "corpus_sha256": "0" * 64,
        },
        code={"commit": "integration", "lock_sha256": "0" * 64, "dirty": True},
        retrieval={
            "strategy": "integration",
            "model": "integration",
            "index": "integration",
            "encoding_rule_version": "integration",
            "cache_identity": "integration",
        },
        generation={"model": "integration", "prompt_version": "integration"},
        judge={
            "model": "step-5-preview",
            "prompt_version": "integration",
            "metric_version": "integration",
        },
    )


def assert_failure_blocks_release(checkpoint: GenerationCheckpoint) -> None:
    failed = failed_outcome("faithfulness", RuntimeError("integration failure"))
    checkpoint_result = CheckpointResult(
        checkpoint=checkpoint.checkpoint,
        outcomes=[failed],
    )
    if checkpoint_result.outcomes[0].score is not None:
        raise RuntimeError("失败结果不得携带分数")
    run = EvaluationRun(
        identity=run_identity(checkpoint.checkpoint.checkpoint_id),
        coverage=RunCoverage(
            expected_scenarios=1,
            observed_scenarios=1,
            expected_checkpoints=1,
            observed_checkpoints=1,
        ),
        scenarios=[
            ScenarioResult(
                scenario_id=checkpoint.checkpoint.scenario_id,
                checkpoints=[checkpoint_result],
            )
        ],
    )
    issues = run.blocking_issues()
    if not any("指标执行失败" in issue for issue in issues):
        raise RuntimeError("失败指标未阻止完整成功发布")


async def verify_timeout_and_cancel(checkpoint: GenerationCheckpoint, judge: JudgeConfig) -> None:
    async with GenerationScoringSession(judge, timeout=0.001) as session:
        try:
            await session.score(checkpoint)
        except TimeoutError:
            pass
        else:
            raise RuntimeError("极小 timeout 未触发超时")
    if not session.closed:
        raise RuntimeError("超时后评分会话未释放客户端")

    async with GenerationScoringSession(judge, timeout=30.0) as session:
        task = asyncio.create_task(session.score(checkpoint))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise RuntimeError("取消未传播到调用者")
    if not session.closed:
        raise RuntimeError("取消后评分会话未释放客户端")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        raise SystemExit("timeout 必须为有限正数")
    if os.environ.get(ENABLE_VARIABLE, "").strip() != "1":
        print(f"设置 {ENABLE_VARIABLE}=1 后执行真实裁判集成检查")
        return 0

    judge = load_judge_config()
    checkpoint = build_checkpoint()
    catalog_identity = verify_catalog_sample(checkpoint)
    scored = await score_checkpoint_with_evidence(
        checkpoint,
        judge,
        timeout=args.timeout,
    )
    assert_failure_blocks_release(checkpoint)
    statuses = {outcome.metric: outcome.status for outcome in scored.result.outcomes}
    if statuses != {"faithfulness": "succeeded", "factual_correctness": "succeeded"}:
        raise RuntimeError(f"生成指标未全部成功: {statuses}")
    if len(scored.evidence) != 2:
        raise RuntimeError("成功指标证据数量不一致")
    for evidence in scored.evidence:
        if evidence.score is None or not 0 <= evidence.score <= 1:
            raise RuntimeError("评分不在 0 到 1 之间")
        if len(evidence.prompt_digest) != 64:
            raise RuntimeError("prompt 摘要非法")
        if not evidence.judge_calls:
            raise RuntimeError("未记录真实裁判调用")
        if not evidence.chain_steps:
            raise RuntimeError("未记录 Ragas 回调证据")

    await verify_timeout_and_cancel(checkpoint, judge)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = OUTPUT_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "evidence.json").write_text(
        scored.model_dump_json(indent=2),
        encoding="utf-8",
    )
    receipt = {
        "enabled": True,
        "timeout_seconds": args.timeout,
        "catalog": catalog_identity,
        "statuses": statuses,
        "scores": {
            outcome.metric: outcome.score for outcome in scored.result.outcomes
        },
        "prompt_digests": {
            evidence.metric: evidence.prompt_digest for evidence in scored.evidence
        },
        "judge_calls": {
            evidence.metric: len(evidence.judge_calls) for evidence in scored.evidence
        },
        "timeout_and_cancel_released": True,
        "evidence": str(directory / "evidence.json"),
    }
    (directory / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
