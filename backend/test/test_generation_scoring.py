import asyncio
import math
import os
import socket
from contextlib import asynccontextmanager, suppress

import numpy as np
import openai
import pytest
from pydantic import ValidationError
from ragas import _analytics as ragas_analytics
from ragas.callbacks import RagasTracer
from ragas.metrics import FactualCorrectness, Faithfulness
from ragas.run_config import RunConfig

from evaluation.contracts import (
    AnswerEvidence,
    CheckpointResult,
    ContextItem,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationReference,
    MetricApplicability,
    MetricOutcome,
    ModelInputContext,
    PriorMessage,
    ReferenceFact,
    UserInputEvidence,
)
from evaluation.generation_scoring import (
    FactualCorrectnessParameters,
    FaithfulnessParameters,
    GenerationScoringResult,
    GenerationScoringSession,
    InvalidGenerationScore,
    InvalidJudgeConfig,
    InvalidTimeout,
    JudgeCall,
    JudgeCallRecorder,
    JudgeConfig,
    JudgeConfigError,
    ScoringAttempt,
    ScoringInput,
    build_metric_evidence,
    build_metric_plan,
    dependency_versions,
    ensure_ragas_tracking_disabled,
    failed_outcome,
    load_judge_model,
    metric_parameters,
    prompt_digest,
    prompt_fingerprints,
    run_judge_identity,
    sanitize_endpoint,
    score_checkpoint,
    validate_score,
    validate_timeout,
)

JUDGE = JudgeConfig(
    model="step-5-preview",
    base_url="https://example.invalid/v1",
    api_key="sk-sentinel-value",
)


def evidence_link(checkpoint_id="cp-1", scenario_id="s-generation-01"):
    return EvidenceLink(
        run_id="run-1",
        scenario_id=scenario_id,
        event_id="msg-1",
        checkpoint_id=checkpoint_id,
    )


def context_items(message="给我一个训练计划", truncated=False, omitted=0):
    tool_text = "动作目录结果"
    return [
        ContextItem(source="system_prompt", content="系统提示", fact_origin="system"),
        ContextItem(source="user_message", content=message, fact_origin="user"),
        ContextItem(
            source="tool_result",
            content="动作目录" if truncated else tool_text,
            fact_origin="tool",
            tool_call_id="call-1",
            tool_name="search_exercises",
            truncated=truncated,
            omitted_chars=omitted if truncated else None,
            original_chars=len(tool_text) if truncated else None,
        ),
    ]


def checkpoint(
    *,
    faithfulness=True,
    factual=True,
    reference=True,
    truncated=False,
    omitted=0,
    message="给我一个训练计划",
):
    applicable = faithfulness or factual
    return GenerationCheckpoint(
        checkpoint=evidence_link(),
        user_input=UserInputEvidence(
            message=message,
            prior_messages=[PriorMessage(role="user", content="我想增肌")],
        ),
        answer=AnswerEvidence(
            visible_text="建议安排推拉腿循环。",
            structured_snapshot={"kind": "plan"},
        ),
        model_input=ModelInputContext(
            items=context_items(
                message=message,
                truncated=truncated,
                omitted=omitted,
            ),
            truncated=truncated,
            omitted_chars=omitted if truncated else None,
        ),
        reference=GenerationReference(
            answer="参考回答",
            facts=[ReferenceFact(fact_id="f1", text="参考事实")],
        )
        if (applicable and reference)
        else None,
        applicability=[
            MetricApplicability(
                metric="faithfulness",
                applicable=faithfulness,
                reason=None if faithfulness else "纯确认",
            ),
            MetricApplicability(
                metric="factual_correctness",
                applicable=factual,
                reason=None if factual else "无参考",
            ),
        ],
    )


def test_faithfulness_sample_maps_real_answer_contexts_and_user_input():
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    assert plan.metric == "faithfulness"
    assert plan.sample.user_input == "给我一个训练计划"
    assert plan.sample.response == item.answer.visible_text
    assert plan.sample.retrieved_contexts == [
        "系统提示",
        "给我一个训练计划",
        "动作目录结果",
    ]
    assert plan.sample.reference is None
    assert plan.scoring_input.response == item.answer.visible_text
    assert plan.scoring_input.user_input == "给我一个训练计划"
    assert plan.scoring_input.retrieved_contexts == plan.sample.retrieved_contexts
    assert plan.scoring_input.reference is None


def test_factual_sample_uses_only_answer_and_reviewed_reference():
    item = checkpoint()
    plan = build_metric_plan(item)[1]
    assert plan.metric == "factual_correctness"
    assert plan.sample.response == item.answer.visible_text
    assert plan.sample.reference == item.reference.answer
    assert plan.sample.user_input is None
    assert plan.sample.retrieved_contexts is None
    assert plan.scoring_input.reference == item.reference.answer
    assert plan.scoring_input.user_input is None
    assert plan.scoring_input.retrieved_contexts is None


def test_reference_and_hidden_facts_do_not_pollute_contexts():
    item = checkpoint()
    plans = build_metric_plan(item)
    faithfulness_sample = plans[0].sample
    factual_sample = plans[1].sample
    assert faithfulness_sample.reference is None
    assert "参考回答" not in faithfulness_sample.retrieved_contexts
    assert "参考事实" not in faithfulness_sample.retrieved_contexts
    assert factual_sample.reference == "参考回答"
    assert factual_sample.retrieved_contexts is None
    assert factual_sample.user_input is None


def test_truncated_context_keeps_sent_content_and_metadata():
    item = checkpoint(truncated=True, omitted=2)
    plans = build_metric_plan(item)
    sample = plans[0].sample
    assert sample.retrieved_contexts == ["系统提示", "给我一个训练计划", "动作目录"]
    assert plans[0].scoring_input.contexts_truncated is True
    assert plans[0].scoring_input.contexts_omitted_chars == 2
    assert plans[1].scoring_input.contexts_truncated is False
    assert plans[1].scoring_input.contexts_omitted_chars is None


async def test_both_inapplicable_needs_no_reference_judge_timeout_or_ragas_input():
    item = checkpoint(faithfulness=False, factual=False, reference=False)
    result = await score_checkpoint(item)
    assert result.checkpoint == item.checkpoint
    assert [outcome.metric for outcome in result.outcomes] == [
        "faithfulness",
        "factual_correctness",
    ]
    assert [outcome.status for outcome in result.outcomes] == [
        "not_applicable",
        "not_applicable",
    ]
    assert [outcome.reason for outcome in result.outcomes] == ["纯确认", "无参考"]
    assert all(outcome.score is None for outcome in result.outcomes)


async def test_single_applicable_keeps_inactive_reason_and_requires_real_config():
    item = checkpoint(faithfulness=True, factual=False)
    plans = build_metric_plan(item)
    assert plans[0].sample is not None
    assert plans[0].scoring_input is not None
    assert plans[1].sample is None
    assert plans[1].applicability.reason == "无参考"

    with pytest.raises(JudgeConfigError):
        await score_checkpoint(item, timeout=5.0)
    with pytest.raises(InvalidTimeout):
        await score_checkpoint(item, JUDGE, timeout=None)
    with pytest.raises(InvalidTimeout):
        await score_checkpoint(item, JUDGE, timeout=0)


async def test_frozen_applicability_is_not_overridden_by_answer_style():
    item = checkpoint(faithfulness=False, factual=False, reference=False)
    assert item.answer.visible_text
    result = await score_checkpoint(item)
    assert {outcome.status for outcome in result.outcomes} == {"not_applicable"}


def test_score_boundaries_accept_real_zero_and_one():
    assert validate_score(0) == 0.0
    assert validate_score(0.0) == 0.0
    assert validate_score(1) == 1.0
    assert validate_score(1.0) == 1.0
    assert validate_score(np.float64(0.5)) == 0.5


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        np.float64("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        1.01,
        True,
        False,
        "0.5",
        None,
        10**400,
    ],
)
def test_score_boundaries_reject_non_finite_boolean_and_out_of_range(value):
    with pytest.raises(InvalidGenerationScore):
        validate_score(value)


@pytest.mark.parametrize(
    "value",
    [0, -1, float("nan"), float("inf"), float("-inf"), True, "5", None, 10**400],
)
def test_timeout_must_be_explicit_finite_positive(value):
    with pytest.raises(InvalidTimeout):
        validate_timeout(value)


def test_timeout_accepts_finite_positive_values():
    assert validate_timeout(1) == 1.0
    assert validate_timeout(0.25) == 0.25


def test_judge_model_environment_value_supports_explicit_switch():
    assert (
        load_judge_model({"FIT_AGENT_EVAL_JUDGE_MODEL": "step-5-preview"})
        == "step-5-preview"
    )
    assert (
        load_judge_model({"FIT_AGENT_EVAL_JUDGE_MODEL": "step-3.7-flash"})
        == "step-3.7-flash"
    )
    assert (
        load_judge_model({"FIT_AGENT_EVAL_JUDGE_MODEL": "deepseek-v4.1-flash"})
        == "deepseek-v4.1-flash"
    )
    assert (
        load_judge_model({"FIT_AGENT_EVAL_JUDGE_MODEL": "qwen3.6-flash"})
        == "qwen3.6-flash"
    )
    for value in (None, "", "   ", "unknown-judge", "step-5-preview-extra"):
        source = (
            {}
            if value is None
            else {"FIT_AGENT_EVAL_JUDGE_MODEL": value}
        )
        with pytest.raises(JudgeConfigError):
            load_judge_model(source)


def test_judge_config_rejects_wrong_model_and_invalid_endpoint():
    JudgeConfig(
        model="step-3.7-flash",
        base_url="https://example.invalid/v1",
        api_key="x",
    )
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="unknown-judge",
            base_url="https://example.invalid/v1",
            api_key="x",
        )
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="step-5-preview",
            base_url="not-a-url",
            api_key="x",
        )
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="step-5-preview",
            base_url="ftp://example.invalid/v1",
            api_key="x",
        )
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="step-5-preview",
            base_url="https://example.invalid/v1",
            api_key="   ",
        )
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="step-5-preview",
            base_url=" https://example.invalid/v1",
            api_key="x",
        )


def test_judge_config_from_environment_requires_model_variable():
    config = JudgeConfig.from_environment(
        base_url="https://example.invalid/v1",
        api_key="x",
        environ={"FIT_AGENT_EVAL_JUDGE_MODEL": "step-5-preview"},
    )
    assert config.model == "step-5-preview"
    with pytest.raises(JudgeConfigError):
        JudgeConfig.from_environment(
            base_url="https://example.invalid/v1",
            api_key="x",
            environ={},
        )


def test_prompt_fingerprints_come_from_real_ragas_metric_prompts():
    metric = Faithfulness()
    fingerprints = prompt_fingerprints(metric)
    prompts = metric.get_prompts()
    assert [item.name for item in fingerprints] == sorted(prompts)
    for item in fingerprints:
        prompt = prompts[item.name]
        assert item.instruction == prompt.instruction
        assert item.language == prompt.language
        assert item.input_model == prompt.input_model.__name__
        assert item.output_model == prompt.output_model.__name__
        assert item.example_count == len(prompt.examples)
        assert len(item.sha256) == 64
    before = prompt_digest(fingerprints)
    prompts["n_l_i_statement_prompt"].instruction += " changed"
    after = prompt_digest(prompt_fingerprints(metric))
    assert before != after


def test_factual_correctness_uses_locked_default_parameters():
    metric = FactualCorrectness()
    parameters = metric_parameters(metric)
    assert isinstance(parameters, FactualCorrectnessParameters)
    assert parameters.mode == "f1"
    assert parameters.beta == 1.0
    assert parameters.atomicity == "low"
    assert parameters.coverage == "low"
    assert parameters.language == "english"

    faithfulness = metric_parameters(Faithfulness())
    assert isinstance(faithfulness, FaithfulnessParameters)
    assert faithfulness.max_retries == 1


def test_dependency_versions_pin_locked_ragas_identity():
    versions = dependency_versions()
    assert versions["ragas"] == "0.3.1"
    assert versions["openai"] == "3.13.0"
    assert versions["langchain-openai"]
    assert versions["langchain-core"]


@pytest.mark.parametrize(
    "model_name", ["step-5-preview", "step-3.7-flash", "deepseek-v4.1-flash"]
)
def test_evidence_records_prompt_metric_and_run_configuration(model_name):
    judge = JudgeConfig(model=model_name, base_url=JUDGE.base_url, api_key=JUDGE.api_key)
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    evidence = build_metric_evidence(
        item,
        plan,
        Faithfulness(),
        judge,
        RunConfig(timeout=5),
        timeout=5,
        score=0.0,
    )
    assert evidence.checkpoint == item.checkpoint
    assert evidence.metric == "faithfulness"
    assert evidence.metric_class == "Faithfulness"
    assert evidence.metric_parameters.kind == "faithfulness"
    assert evidence.judge_model == model_name
    restored = type(evidence).model_validate_json(evidence.model_dump_json())
    assert restored.judge_model == model_name
    assert run_judge_identity([evidence]).model == model_name
    assert evidence.judge_base_url == JUDGE.base_url
    assert evidence.judge_sdk_max_retries == 0
    assert evidence.judge_temperature == 1e-8
    assert evidence.score == 0.0
    assert evidence.error is None
    assert evidence.prompt_digest == prompt_digest(
        prompt_fingerprints(Faithfulness())
    )
    assert evidence.ragas_run_config.timeout_seconds == 5.0
    assert evidence.ragas_run_config.max_retries >= 1


def test_evidence_preserves_context_source_tool_binding_and_truncation():
    item = checkpoint(truncated=True, omitted=2)
    plan = build_metric_plan(item)[0]
    evidence = build_metric_evidence(
        item,
        plan,
        Faithfulness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    assert [entry.source for entry in evidence.contexts] == [
        "system_prompt",
        "user_message",
        "tool_result",
    ]
    assert [entry.fact_origin for entry in evidence.contexts] == [
        "system",
        "user",
        "tool",
    ]
    tool = evidence.contexts[-1]
    assert tool.tool_call_id == "call-1"
    assert tool.tool_name == "search_exercises"
    assert tool.truncated is True
    assert tool.omitted_chars == 2
    assert tool.original_chars == len("动作目录结果")
    assert evidence.scoring_input.retrieved_contexts == [
        "系统提示",
        "给我一个训练计划",
        "动作目录",
    ]
    assert evidence.model_input_truncated is True
    assert evidence.model_input_omitted_chars == 2


def test_factual_evidence_isolated_from_contexts():
    item = checkpoint()
    plan = build_metric_plan(item)[1]
    evidence = build_metric_evidence(
        item,
        plan,
        FactualCorrectness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.75,
    )
    assert evidence.metric == "factual_correctness"
    assert evidence.metric_parameters.kind == "factual_correctness"
    assert evidence.contexts == []
    assert evidence.scoring_input.reference == "参考回答"
    assert evidence.scoring_input.retrieved_contexts is None
    assert evidence.model_input_truncated is False
    assert evidence.model_input_omitted_chars is None


def test_evidence_serialization_never_contains_credentials():
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    evidence = build_metric_evidence(
        item,
        plan,
        Faithfulness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.0,
        judge_calls=[
            JudgeCall(
                run_id="llm-1",
                parent_run_id="prompt-1",
                prompt="actual prompt",
                output='{"statements": []}',
                prompt_name="statement_generator_prompt",
            )
        ],
    )
    payload = evidence.model_dump_json()
    assert "sk-sentinel-value" not in payload
    assert "api_key" not in payload
    assert "Authorization" not in payload
    assert evidence.judge_calls[0].prompt == "actual prompt"
    assert evidence.judge_calls[0].output == '{"statements": []}'


def test_scoring_input_rejects_mixed_metric_fields():
    with pytest.raises(ValidationError):
        ScoringInput(
            response="回答",
            user_input="问题",
            retrieved_contexts=["上下文"],
            reference="参考",
        )
    with pytest.raises(ValidationError):
        ScoringInput(response="回答", user_input="问题")
    with pytest.raises(ValidationError):
        ScoringInput(
            response="回答",
            user_input="问题",
            retrieved_contexts=["上下文"],
            contexts_truncated=True,
        )


def test_metric_evidence_requires_exactly_one_result():
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    with pytest.raises(ValueError):
        build_metric_evidence(
            item,
            plan,
            Faithfulness(),
            JUDGE,
            RunConfig(timeout=5),
            timeout=5,
        )
    with pytest.raises(ValueError):
        build_metric_evidence(
            item,
            plan,
            Faithfulness(),
            JUDGE,
            RunConfig(timeout=5),
            timeout=5,
            score=0.5,
            error="judge failed",
        )


def test_failed_outcome_keeps_error_type_without_score():
    outcome = failed_outcome("faithfulness", RuntimeError("judge failed"))
    assert outcome.metric == "faithfulness"
    assert outcome.status == "failed"
    assert outcome.score is None
    assert "RuntimeError" in outcome.error
    assert "judge failed" in outcome.error
    with pytest.raises(ValueError):
        failed_outcome("faithfulness", "   ")


def test_run_judge_identity_combines_real_prompt_and_metric_digests():
    item = checkpoint()
    plans = build_metric_plan(item)
    faithfulness = build_metric_evidence(
        item,
        plans[0],
        Faithfulness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    factual = build_metric_evidence(
        item,
        plans[1],
        FactualCorrectness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    identity = run_judge_identity([faithfulness, factual])
    assert identity.model == "step-5-preview"
    assert len(identity.prompt_version) == 64
    assert len(identity.metric_version) == 64
    assert identity.prompt_version != identity.metric_version
    with pytest.raises(ValueError):
        run_judge_identity([])


def test_metric_version_differs_between_metrics():
    item = checkpoint()
    plans = build_metric_plan(item)
    faithfulness = build_metric_evidence(
        item,
        plans[0],
        Faithfulness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    factual = build_metric_evidence(
        item,
        plans[1],
        FactualCorrectness(),
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    assert faithfulness.metric_version != factual.metric_version
    assert faithfulness.prompt_digest != factual.prompt_digest


def test_no_nan_or_infinity_can_become_success():
    with pytest.raises(InvalidGenerationScore):
        validate_score(math.nan)
    with pytest.raises(InvalidGenerationScore):
        validate_score(math.inf)


CREDENTIAL_ENDPOINT = (
    "https://sentineluser:sentinelpass@example.invalid/v1"
    "?api_key=sentinelquery#sentinel-fragment"
)


def metric_evidence(item, index=0, score=0.0, error=None):
    plan = build_metric_plan(item)[index]
    metric = Faithfulness() if plan.metric == "faithfulness" else FactualCorrectness()
    return build_metric_evidence(
        item,
        plan,
        metric,
        JUDGE,
        RunConfig(timeout=5),
        timeout=5,
        score=score,
        error=error,
    )


def test_endpoint_sanitizer_removes_userinfo_query_and_fragment():
    sanitized = sanitize_endpoint(CREDENTIAL_ENDPOINT)
    assert sanitized == "https://example.invalid/v1"
    for marker in (
        "sentineluser",
        "sentinelpass",
        "sentinelquery",
        "sentinel-fragment",
    ):
        assert marker not in sanitized


@pytest.mark.parametrize(
    "base_url",
    [
        "https://sentineluser:sentinelpass@example.invalid/v1",
        "https://sentineluser@example.invalid/v1",
        "https://example.invalid/v1?api_key=sentinelquery",
        "https://example.invalid/v1#sentinel-fragment",
    ],
)
def test_judge_config_rejects_credentials_in_endpoint(base_url):
    with pytest.raises(InvalidJudgeConfig):
        JudgeConfig(
            model="step-5-preview",
            base_url=base_url,
            api_key="x",
        )


def test_evidence_serialization_strips_embedded_endpoint_credentials():
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    # 绕过配置校验，验证证据层仍按统一规则脱敏。
    unsafe = object.__new__(JudgeConfig)
    object.__setattr__(unsafe, "model", "step-5-preview")
    object.__setattr__(unsafe, "base_url", CREDENTIAL_ENDPOINT)
    object.__setattr__(unsafe, "api_key", "sk-sentinel-value")
    evidence = build_metric_evidence(
        item,
        plan,
        Faithfulness(),
        unsafe,
        RunConfig(timeout=5),
        timeout=5,
        score=0.5,
    )
    payload = evidence.model_dump_json()
    assert evidence.judge_base_url == "https://example.invalid/v1"
    for marker in (
        "sentineluser",
        "sentinelpass",
        "sentinelquery",
        "sentinel-fragment",
        "sk-sentinel-value",
    ):
        assert marker not in payload


def test_result_accepts_matching_success_evidence():
    item = checkpoint()
    evidence = metric_evidence(item, 0, score=0.0)
    result = CheckpointResult(
        checkpoint=item.checkpoint,
        outcomes=[
            MetricOutcome(metric="faithfulness", status="succeeded", score=0.0)
        ],
    )
    scored = GenerationScoringResult(result=result, evidence=[evidence])
    assert scored.evidence[0].score == scored.result.outcomes[0].score


def test_result_rejects_conflicting_score_evidence():
    item = checkpoint()
    evidence = metric_evidence(item, 0, score=0.0)
    result = CheckpointResult(
        checkpoint=item.checkpoint,
        outcomes=[
            MetricOutcome(metric="faithfulness", status="succeeded", score=1.0)
        ],
    )
    with pytest.raises(ValidationError):
        GenerationScoringResult(result=result, evidence=[evidence])


def test_result_rejects_error_only_evidence_for_success():
    item = checkpoint()
    evidence = metric_evidence(item, 0, score=None, error="judge failed")
    result = CheckpointResult(
        checkpoint=item.checkpoint,
        outcomes=[
            MetricOutcome(metric="faithfulness", status="succeeded", score=0.0)
        ],
    )
    with pytest.raises(ValidationError):
        GenerationScoringResult(result=result, evidence=[evidence])


def test_run_judge_identity_is_stable_across_duplicate_evidence():
    item = checkpoint()
    faithfulness = metric_evidence(item, 0, score=0.5)
    factual = metric_evidence(item, 1, score=0.5)
    once = run_judge_identity([faithfulness, factual])
    twice = run_judge_identity([faithfulness, factual, faithfulness, factual])
    assert once == twice


def test_run_judge_identity_rejects_conflicting_metric_versions():
    item = checkpoint()
    faithfulness = metric_evidence(item, 0, score=0.5)
    conflict = faithfulness.model_copy(update={"metric_version": "f" * 64})
    with pytest.raises(ValueError):
        run_judge_identity([faithfulness, conflict])


def test_run_judge_identity_rejects_conflicting_prompt_versions():
    item = checkpoint()
    faithfulness = metric_evidence(item, 0, score=0.5)
    conflict = faithfulness.model_copy(update={"prompt_digest": "e" * 64})
    with pytest.raises(ValueError):
        run_judge_identity([faithfulness, conflict])


@pytest.mark.parametrize("preset", ["false", "0", "no"])
def test_ragas_tracking_is_forced_off_after_cached_false(monkeypatch, preset):
    monkeypatch.setenv("RAGAS_DO_NOT_TRACK", preset)
    ragas_analytics.do_not_track.cache_clear()
    try:
        assert ragas_analytics.do_not_track() is False
        ensure_ragas_tracking_disabled()
        assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"
        assert ragas_analytics.do_not_track() is True
    finally:
        ragas_analytics.do_not_track.cache_clear()


@pytest.mark.parametrize(
    "model_name", ["step-5-preview", "step-3.7-flash", "deepseek-v4.1-flash"]
)
def test_attempt_snapshot_is_available_before_scoring(model_name):
    judge = JudgeConfig(model=model_name, base_url=JUDGE.base_url, api_key=JUDGE.api_key)
    item = checkpoint()
    plan = build_metric_plan(item)[0]
    attempt = ScoringAttempt(
        checkpoint=item,
        plan=plan,
        metric=Faithfulness(),
        judge=judge,
        run_config=RunConfig(timeout=5),
        timeout=5,
        recorder=JudgeCallRecorder(),
        tracer=RagasTracer(),
    )
    snapshot = attempt.snapshot()
    assert snapshot.judge_model == model_name
    restored = type(snapshot).model_validate_json(snapshot.model_dump_json())
    assert restored.judge_model == model_name
    assert snapshot.status == "in_progress"
    assert snapshot.metric == "faithfulness"
    assert snapshot.scoring_input == plan.scoring_input
    assert snapshot.prompt_fingerprints
    assert snapshot.judge_calls == []
    with pytest.raises(RuntimeError):
        attempt.to_evidence()
    attempt.fail(RuntimeError("transport failed"))
    failed_snapshot = attempt.snapshot()
    assert failed_snapshot.status == "failed"
    assert failed_snapshot.score is None
    assert "RuntimeError" in failed_snapshot.error
    failed_evidence = attempt.to_evidence()
    assert failed_evidence.error == failed_snapshot.error
    assert failed_evidence.score is None
    assert failed_evidence.judge_calls == []


def _closed_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@asynccontextmanager
async def _hanging_endpoint():
    stop = asyncio.Event()

    async def handler(_reader, writer):
        try:
            await stop.wait()
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        stop.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.05)


async def test_local_transport_failure_preserves_attempt_evidence(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    judge = JudgeConfig(
        model="step-5-preview",
        base_url=_closed_endpoint(),
        api_key="test-key",
    )
    item = checkpoint()
    async with GenerationScoringSession(judge, timeout=2.0) as session:
        with pytest.raises((openai.APIConnectionError, TimeoutError)) as exc_info:
            await session.score(item)
        attempt = session.current_attempt
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.score is None
        assert attempt.error
        assert type(exc_info.value).__name__ in attempt.error
        snapshot = attempt.snapshot()
        assert snapshot.status == "failed"
        assert snapshot.scoring_input.retrieved_contexts
        assert snapshot.prompt_fingerprints
        assert snapshot.judge_calls
        assert session.evidence == ()
        assert session.outcomes == ()
    assert session.closed


async def test_timeout_preserves_attempt_evidence(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    async with _hanging_endpoint() as base_url:
        judge = JudgeConfig(
            model="step-5-preview",
            base_url=base_url,
            api_key="test-key",
        )
        item = checkpoint()
        async with GenerationScoringSession(judge, timeout=0.5) as session:
            with pytest.raises(
                (openai.APIConnectionError, TimeoutError)
            ) as exc_info:
                await session.score(item)
            attempt = session.current_attempt
            assert attempt is not None
            assert attempt.status == "failed"
            assert attempt.error
            assert type(exc_info.value).__name__ in attempt.error
            snapshot = attempt.snapshot()
            assert snapshot.status == "failed"
            assert snapshot.scoring_input.retrieved_contexts
            assert snapshot.prompt_fingerprints
            assert snapshot.judge_calls
            assert session.evidence == ()
        assert session.closed


async def test_cancellation_preserves_attempt_evidence(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    async with _hanging_endpoint() as base_url:
        judge = JudgeConfig(
            model="step-5-preview",
            base_url=base_url,
            api_key="test-key",
        )
        item = checkpoint()
        async with GenerationScoringSession(judge, timeout=30.0) as session:
            task = asyncio.create_task(session.score(item))
            for _ in range(100):
                attempt = session.current_attempt
                if attempt is not None and attempt.recorder.calls:
                    break
                await asyncio.sleep(0.01)
            assert session.current_attempt is not None
            assert session.current_attempt.recorder.calls
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            attempt = session.current_attempt
            assert attempt is not None
            assert attempt.status == "failed"
            assert attempt.error
            assert "CancelledError" in attempt.error
            snapshot = attempt.snapshot()
            assert snapshot.status == "failed"
            assert snapshot.scoring_input.retrieved_contexts
            assert snapshot.prompt_fingerprints
            assert snapshot.judge_calls
            assert session.evidence == ()
        assert session.closed


def test_integration_sample_uses_real_catalog_identity():
    from test import check_generation_scoring_integration as integration

    item = integration.build_checkpoint()
    identity = integration.verify_catalog_sample(item)
    assert identity == {
        "id": "0001",
        "name": "3/4仰卧起坐",
        "equipment": "body weight",
        "target": "abs",
    }
    assert item.answer.visible_text == "3/4仰卧起坐 使用 body weight。"
    assert item.reference.answer == item.answer.visible_text
