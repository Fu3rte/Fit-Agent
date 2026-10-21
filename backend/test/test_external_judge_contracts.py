from typing import get_args

import pytest
from pydantic import ValidationError

from evaluation.contracts import (
    EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
    EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
    EXTERNAL_JUDGE_METRICS,
    GENERATION_METRICS,
    AnswerEvidence,
    ContextItem,
    DatasetIdentity,
    EvidenceLink,
    ExternalJudgeApplicability,
    ExternalJudgeCheckpointInputs,
    ExternalJudgeCheckpointOutputs,
    ExternalJudgeCheckpointSchemaVersion,
    ExternalJudgeCheckpointUpload,
    ExternalJudgeCloudFeedbackIdentity,
    ExternalJudgeContext,
    ExternalJudgeDefinitionIdentity,
    ExternalJudgeExperimentIdentity,
    ExternalJudgeFeedback,
    ExternalJudgeFeedbackSchemaVersion,
    ExternalJudgeFeedbackSet,
    ExternalJudgeFeedbackSource,
    ExternalJudgeMetadata,
    ExternalJudgeModelIdentity,
    ExternalJudgeOutboundProjection,
    ExternalJudgeReferenceOutputs,
    ExternalJudgeRetrievalEvidence,
    ExternalJudgeScoringAttempt,
    ExternalJudgeSource,
    ExternalJudgeUserRequest,
    GenerationCheckpoint,
    GenerationReference,
    MessageNode,
    MetricApplicability,
    MetricOutcome,
    ModelInputContext,
    ModelVisibleSearch,
    PriorMessage,
    ReferenceFact,
    ReportedModelVersion,
    RequestBoundaryEvidence,
    RerankEvidence,
    RerankIdentityEvidence,
    RerankScoredEvidence,
    RerankUsageEvidence,
    SearchResponseEvidence,
    ToolNode,
    UnreportedModelVersion,
    UserInputEvidence,
)

LINK = EvidenceLink(
    run_id="run-1",
    scenario_id="s-action-verification-01",
    event_id="e01",
    checkpoint_id="cp1",
)
OTHER_LINK = EvidenceLink(
    run_id="run-1",
    scenario_id="s-action-verification-01",
    event_id="e01",
    checkpoint_id="cp2",
)
DATASET = DatasetIdentity(
    version="rebuild-v1",
    manifest_sha256="a" * 64,
    content_sha256="b" * 64,
    corpus_sha256="c" * 64,
)
EXECUTION_SHA = "d" * 64
TOOL_CONTENT = '{"items":[],"has_more":false}'


def boundary() -> RequestBoundaryEvidence:
    return RequestBoundaryEvidence(
        api="openai-chat",
        session_id="session-1",
        run_id="business-run-1",
        request_entry_id="entry-user-1",
        nodes=[
            MessageNode(
                entry_id="entry-user-1",
                session_id="session-1",
                role="user",
                content="给我训练计划",
                kind="message",
                run_id="business-run-1",
            )
        ],
        tool_nodes=[
            ToolNode(
                tool_call_id="call-1",
                tool_name="search_exercises",
                content=TOOL_CONTENT,
                entry_id="entry-tool-1",
                session_id="session-1",
                run_id="business-run-1",
                is_error=False,
            )
        ],
    )


def search_evidence() -> SearchResponseEvidence:
    return SearchResponseEvidence(
        query_id="tool-call:call-1",
        response_json={"items": [], "has_more": False},
        model_visible=ModelVisibleSearch(items=[], has_more=False),
    )


def context(retrieval=None, truncated=False, omitted_chars=None):
    return ExternalJudgeContext(
        items=[
            ContextItem(
                source="user_message",
                content="给我训练计划",
                fact_origin="user",
                message_entry_id="entry-user-1",
                message_role="user",
            ),
            ContextItem(
                source="tool_result",
                content=TOOL_CONTENT,
                fact_origin="tool",
                tool_call_id="call-1",
                tool_name="search_exercises",
            ),
        ],
        truncated=truncated,
        omitted_chars=omitted_chars,
        request_boundary=boundary(),
        retrieval=(
            [
                ExternalJudgeRetrievalEvidence(
                    tool_call_id="call-1",
                    tool_name="search_exercises",
                    search=search_evidence(),
                )
            ]
            if retrieval is None
            else retrieval
        ),
    )


def reference() -> GenerationReference:
    return GenerationReference(
        answer="每周训练三次",
        facts=[ReferenceFact(fact_id="f1", text="每周训练三次")],
        must_include=["训练频率"],
        allowed_variation=["措辞"],
        forbidden_claims=["每周训练七次"],
    )


def applicability() -> list[ExternalJudgeApplicability]:
    return [
        ExternalJudgeApplicability(metric="evidence_supported_v1", applicable=True),
        ExternalJudgeApplicability(
            metric="reference_requirements_met_v1", applicable=True
        ),
    ]


def user_request(link=LINK) -> ExternalJudgeUserRequest:
    return ExternalJudgeUserRequest(
        message="给我训练计划",
        prior_messages=[PriorMessage(role="user", content="我想增肌")],
        checkpoint=link,
    )


def upload_parts() -> dict:
    return {
        "schema_version": EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
        "metadata": ExternalJudgeMetadata(
            dataset=DATASET,
            checkpoint=LINK,
            execution_sha256=EXECUTION_SHA,
        ),
        "inputs": ExternalJudgeCheckpointInputs(user_request=user_request()),
        "outputs": ExternalJudgeCheckpointOutputs(
            answer=AnswerEvidence(
                visible_text="每周训练三次",
                structured_snapshot={"stop_reason": "stop", "content": []},
            ),
            context=context(),
        ),
        "reference_outputs": ExternalJudgeReferenceOutputs(
            reference=reference(),
            applicability=applicability(),
        ),
    }


def make_upload(**overrides) -> ExternalJudgeCheckpointUpload:
    parts = upload_parts()
    parts.update(overrides)
    return ExternalJudgeCheckpointUpload(**parts)


def feedback(metric="evidence_supported_v1", status="succeeded", **overrides):
    values = {"metric": metric, "status": status}
    if status == "succeeded" and "score" not in overrides:
        values["score"] = 1.0
    values.update(overrides)
    return ExternalJudgeFeedback(**values)


def feedback_set(feedback_rows=None):
    return ExternalJudgeFeedbackSet(
        schema_version=EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
        checkpoint=LINK,
        execution_sha256=EXECUTION_SHA,
        feedback=(
            [
                feedback(),
                feedback(metric="reference_requirements_met_v1"),
            ]
            if feedback_rows is None
            else feedback_rows
        ),
    )


def attempt_parts() -> dict:
    return {
        "schema_version": EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
        "attempt_id": "attempt-1",
        "checkpoint": LINK,
        "execution_sha256": EXECUTION_SHA,
        "source": ExternalJudgeSource(
            experiment=ExternalJudgeExperimentIdentity(
                experiment_id="experiment-1",
                experiment_name="fit-agent-external-judge",
            ),
            run_id="langsmith-run-1",
            example_id="example-1",
            feedback_source=ExternalJudgeFeedbackSource(
                kind="evaluator", identity="evaluator-1"
            ),
        ),
        "model": ExternalJudgeModelIdentity(
            provider="provider-x",
            evaluator="evaluator-1",
            model_alias="judge-latest",
            model_version=ReportedModelVersion(
                kind="reported", version="2026-09-01"
            ),
            config_sha256="e" * 64,
        ),
        "definition": ExternalJudgeDefinitionIdentity(
            question_id="question-1",
            question_sha256="f" * 64,
            rubric_id="rubric-1",
            rubric_sha256="0" * 64,
        ),
        "outbound": ExternalJudgeOutboundProjection(
            row_id="row-1",
            projection_sha256="1" * 64,
        ),
        "feedback": feedback(),
        "cloud_feedback": ExternalJudgeCloudFeedbackIdentity(
            experiment_id="experiment-1",
            run_id="langsmith-run-1",
            feedback_id="cloud-feedback-1",
        ),
    }


def make_attempt(**overrides) -> ExternalJudgeScoringAttempt:
    parts = attempt_parts()
    parts.update(overrides)
    return ExternalJudgeScoringAttempt(**parts)


def test_external_metric_identity_is_independent_from_generation_metrics():
    assert EXTERNAL_JUDGE_METRICS == (
        "evidence_supported_v1",
        "reference_requirements_met_v1",
    )
    assert not set(EXTERNAL_JUDGE_METRICS) & set(GENERATION_METRICS)
    with pytest.raises(ValidationError):
        MetricApplicability(metric="evidence_supported_v1", applicable=True)
    with pytest.raises(ValidationError):
        ExternalJudgeApplicability(metric="faithfulness", applicable=True)


def test_reference_outputs_require_complete_unique_external_metrics():
    outputs = ExternalJudgeReferenceOutputs(
        reference=reference(), applicability=applicability()
    )
    assert [row.metric for row in outputs.applicability] == list(
        EXTERNAL_JUDGE_METRICS
    )
    with pytest.raises(ValidationError):
        ExternalJudgeReferenceOutputs(
            reference=reference(), applicability=applicability()[:1]
        )
    with pytest.raises(ValidationError):
        ExternalJudgeReferenceOutputs(
            reference=reference(),
            applicability=[applicability()[0], applicability()[0]],
        )
    with pytest.raises(ValidationError):
        ExternalJudgeReferenceOutputs(reference=reference(), applicability=[])
    with pytest.raises(ValidationError):
        ExternalJudgeReferenceOutputs(
            reference=reference(),
            applicability=[
                *applicability(),
                ExternalJudgeApplicability(
                    metric="evidence_supported_v1", applicable=True
                ),
            ],
        )
    with pytest.raises(ValidationError):
        ExternalJudgeApplicability(metric="evidence_supported_v1", applicable=False)
    reasoned = ExternalJudgeApplicability(
        metric="evidence_supported_v1", applicable=False, reason="纯确认回合"
    )
    assert reasoned.reason == "纯确认回合"


def test_checkpoint_upload_round_trip_keeps_context_and_reference_separate():
    upload = make_upload()
    assert upload.schema_version == EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION
    assert upload.inputs.user_request.message == "给我训练计划"
    assert upload.inputs.user_request.prior_messages[0].content == "我想增肌"
    assert upload.outputs.answer.structured_snapshot == {
        "stop_reason": "stop",
        "content": [],
    }
    assert upload.outputs.context.request_boundary.tool_nodes[0].tool_call_id == (
        "call-1"
    )
    assert upload.outputs.context.retrieval[0].search.query_id == "tool-call:call-1"
    assert upload.outputs.context.retrieval[0].rerank is None
    assert upload.reference_outputs.reference.answer == "每周训练三次"
    assert upload.reference_outputs.reference.must_include == ["训练频率"]
    assert upload.reference_outputs.reference.allowed_variation == ["措辞"]
    assert upload.reference_outputs.reference.forbidden_claims == ["每周训练七次"]
    context_texts = [item.content for item in upload.outputs.context.items]
    assert upload.reference_outputs.reference.answer not in context_texts
    restored = ExternalJudgeCheckpointUpload.model_validate_json(
        upload.model_dump_json()
    )
    assert restored == upload


def test_checkpoint_upload_rejects_identity_conflicts():
    with pytest.raises(ValidationError):
        make_upload(
            metadata=ExternalJudgeMetadata(
                dataset=DATASET,
                checkpoint=OTHER_LINK,
                execution_sha256=EXECUTION_SHA,
            )
        )
    with pytest.raises(ValidationError):
        make_upload(
            inputs=ExternalJudgeCheckpointInputs(
                user_request=user_request(OTHER_LINK)
            )
        )
    with pytest.raises(ValidationError):
        make_upload(
            outputs=ExternalJudgeCheckpointOutputs(
                answer=AnswerEvidence(visible_text="回答"),
                context=context(
                    retrieval=[
                        ExternalJudgeRetrievalEvidence(
                            tool_call_id="call-2",
                            tool_name="search_exercises",
                            search=search_evidence(),
                        )
                    ]
                ),
            )
        )
    with pytest.raises(ValidationError):
        make_upload(
            outputs=ExternalJudgeCheckpointOutputs(
                answer=AnswerEvidence(visible_text="回答"),
                context=context(
                    retrieval=[
                        ExternalJudgeRetrievalEvidence(
                            tool_call_id="call-1",
                            tool_name="other_tool",
                            search=search_evidence(),
                        )
                    ]
                ),
            )
        )
    with pytest.raises(ValidationError):
        make_upload(
            outputs=ExternalJudgeCheckpointOutputs(
                answer=AnswerEvidence(visible_text="回答"),
                context=context(
                    retrieval=[
                        ExternalJudgeRetrievalEvidence(
                            tool_call_id="call-1",
                            tool_name="search_exercises",
                            search=search_evidence(),
                        ),
                        ExternalJudgeRetrievalEvidence(
                            tool_call_id="call-1",
                            tool_name="search_exercises",
                            search=search_evidence(),
                        ),
                    ]
                ),
            )
        )
    with pytest.raises(ValidationError):
        context(truncated=True)
    with pytest.raises(ValidationError):
        context(truncated=False, omitted_chars=3)


def test_upload_contract_field_partitions_match_outbound_sections():
    assert set(ExternalJudgeCheckpointUpload.model_fields) == {
        "schema_version",
        "metadata",
        "inputs",
        "outputs",
        "reference_outputs",
    }
    assert set(ExternalJudgeMetadata.model_fields) == {
        "dataset",
        "checkpoint",
        "execution_sha256",
    }
    assert set(ExternalJudgeCheckpointInputs.model_fields) == {"user_request"}
    assert set(ExternalJudgeCheckpointOutputs.model_fields) == {"answer", "context"}
    assert set(ExternalJudgeReferenceOutputs.model_fields) == {
        "reference",
        "applicability",
    }
    assert set(ExternalJudgeFeedback.model_fields) == {
        "metric",
        "status",
        "score",
        "applicability_reason",
        "failure_evidence",
    }
    assert set(ExternalJudgeScoringAttempt.model_fields) == {
        "schema_version",
        "attempt_id",
        "checkpoint",
        "execution_sha256",
        "source",
        "model",
        "definition",
        "outbound",
        "feedback",
        "cloud_feedback",
    }
    forbidden = {
        "api_key",
        "authorization",
        "cookies",
        "headers",
        "judge_calls",
        "model_calls",
        "request_path",
        "scoring_input",
        "secret",
    }
    for model in (
        ExternalJudgeCheckpointUpload,
        ExternalJudgeCheckpointInputs,
        ExternalJudgeCheckpointOutputs,
        ExternalJudgeReferenceOutputs,
        ExternalJudgeFeedback,
        ExternalJudgeScoringAttempt,
    ):
        assert not set(model.model_fields) & forbidden


def test_retrieval_evidence_reuses_rerank_identity():
    evidence = ExternalJudgeRetrievalEvidence(
        tool_call_id="call-1",
        tool_name="search_exercises",
        search=search_evidence(),
        rerank=RerankEvidence(
            mode="rerank",
            identity=RerankIdentityEvidence(
                model="reranker-1",
                endpoint="2" * 64,
                text_version="text-1",
            ),
            scored=[
                RerankScoredEvidence(id="0001", rerank_score=0.9, rrf_rank=0)
            ],
            usage=RerankUsageEvidence(
                requests=1, prompt_tokens=12, total_tokens=12
            ),
            request_id="request-1",
        ),
    )
    assert evidence.rerank.identity.text_version == "text-1"
    assert evidence.search.model_visible.has_more is False
    with pytest.raises(ValidationError):
        ExternalJudgeRetrievalEvidence(
            tool_call_id="call-1",
            tool_name="search_exercises",
            search=search_evidence(),
            rerank=RerankEvidence(mode="rrf_baseline"),
        )
    with pytest.raises(ValidationError):
        ExternalJudgeRetrievalEvidence(
            tool_call_id="call-1",
            tool_name="search_exercises",
            search=search_evidence(),
            rerank=RerankEvidence(
                mode="rerank",
                identity=RerankIdentityEvidence(
                    model="reranker-1",
                    endpoint="https://rerank.example/v1",
                    text_version="text-1",
                ),
                scored=[],
                usage=RerankUsageEvidence(
                    requests=0, prompt_tokens=0, total_tokens=0
                ),
                request_id=None,
            ),
        )


def test_feedback_status_combinations_are_explicit():
    succeeded_zero = feedback(score=0.0)
    succeeded_one = feedback(score=1.0)
    failed = feedback(status="failed", failure_evidence="TimeoutError: judge timeout")
    not_applicable = feedback(
        status="not_applicable",
        applicability_reason="纯确认回合",
    )
    pending = feedback(status="pending")
    assert succeeded_zero.score == 0.0
    assert succeeded_one.score == 1.0
    assert failed.score is None
    assert failed.failure_evidence == "TimeoutError: judge timeout"
    assert not_applicable.score is None
    assert not_applicable.applicability_reason == "纯确认回合"
    assert pending.score is None
    assert pending.status == "pending"
    with pytest.raises(ValidationError):
        feedback(score=None)
    with pytest.raises(ValidationError):
        feedback(score=0.5, failure_evidence="失败")
    with pytest.raises(ValidationError):
        feedback(status="failed")
    with pytest.raises(ValidationError):
        feedback(status="failed", score=0.0, failure_evidence="失败")
    with pytest.raises(ValidationError):
        feedback(status="not_applicable")
    with pytest.raises(ValidationError):
        feedback(status="not_applicable", applicability_reason="纯确认", score=0.0)
    with pytest.raises(ValidationError):
        feedback(status="pending", score=0.0)
    with pytest.raises(ValidationError):
        feedback(status="pending", failure_evidence="失败")


@pytest.mark.parametrize(
    "score",
    [
        True,
        False,
        "0.5",
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        1.01,
        {"score": 0.5},
        [0.5],
    ],
)
def test_feedback_rejects_invalid_score_values(score):
    with pytest.raises(ValidationError):
        feedback(score=score)
    with pytest.raises(ValidationError):
        ExternalJudgeFeedback(
            metric="evidence_supported_v1",
            status="succeeded",
            score=score,
        )


def test_feedback_set_requires_complete_unique_metrics():
    valid = feedback_set()
    assert {row.metric for row in valid.feedback} == set(EXTERNAL_JUDGE_METRICS)
    assert all(row.status == "succeeded" for row in valid.feedback)
    pending_set = feedback_set(
        [feedback(status="pending"), feedback(
            metric="reference_requirements_met_v1", status="pending"
        )]
    )
    assert all(row.status == "pending" for row in pending_set.feedback)
    assert ExternalJudgeFeedbackSet.model_validate_json(
        valid.model_dump_json()
    ) == valid
    with pytest.raises(ValidationError):
        feedback_set([feedback()])
    with pytest.raises(ValidationError):
        feedback_set([feedback(), feedback()])
    with pytest.raises(ValidationError):
        feedback_set(
            [feedback(), feedback(metric="reference_requirements_met_v1")]
            + [feedback()]
        )
    with pytest.raises(ValidationError):
        ExternalJudgeFeedbackSet(
            schema_version=EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
            checkpoint=LINK,
            execution_sha256=EXECUTION_SHA,
            feedback=[
                feedback(),
                ExternalJudgeFeedback(
                    metric="faithfulness", status="succeeded", score=1.0
                ),
            ],
        )


def test_model_alias_and_reported_version_are_distinct():
    identity = ExternalJudgeModelIdentity(
        provider="provider-x",
        evaluator="evaluator-1",
        model_alias="judge-latest",
        model_version=ReportedModelVersion(kind="reported", version="2026-09-01"),
        config_sha256="e" * 64,
    )
    assert identity.model_alias == "judge-latest"
    assert identity.model_version.kind == "reported"
    assert identity.model_version.version == "2026-09-01"
    assert identity.model_alias != identity.model_version.version
    unreported = ExternalJudgeModelIdentity(
        provider="provider-x",
        evaluator="evaluator-1",
        model_alias="judge-latest",
        model_version=UnreportedModelVersion(kind="unreported"),
        config_sha256="e" * 64,
    )
    assert unreported.model_version.kind == "unreported"
    assert "version" not in unreported.model_version.model_dump()
    with pytest.raises(ValidationError):
        UnreportedModelVersion(kind="unreported", version="2026-09-01")
    with pytest.raises(ValidationError):
        ReportedModelVersion(kind="reported")
    with pytest.raises(ValidationError):
        ReportedModelVersion(kind="assumed", version="2026-09-01")
    with pytest.raises(ValidationError):
        ExternalJudgeModelIdentity(
            provider="provider-x",
            evaluator="evaluator-1",
            model_alias="judge-latest",
            model_version="2026-09-01",
            config_sha256="e" * 64,
        )


def test_scoring_attempt_binds_local_and_cloud_identity():
    attempt = make_attempt()
    assert attempt.attempt_id == "attempt-1"
    assert attempt.checkpoint == LINK
    assert attempt.execution_sha256 == EXECUTION_SHA
    assert attempt.source.experiment.experiment_id == "experiment-1"
    assert attempt.source.run_id == "langsmith-run-1"
    assert attempt.source.feedback_source.kind == "evaluator"
    assert attempt.cloud_feedback.feedback_id == "cloud-feedback-1"
    assert attempt.cloud_feedback.feedback_id != attempt.attempt_id
    assert attempt.outbound.projection_sha256 == "1" * 64
    assert attempt.definition.question_sha256 == "f" * 64
    assert attempt.definition.rubric_sha256 == "0" * 64
    assert ExternalJudgeScoringAttempt.model_validate_json(
        attempt.model_dump_json()
    ) == attempt
    with pytest.raises(ValidationError):
        make_attempt(
            cloud_feedback=ExternalJudgeCloudFeedbackIdentity(
                experiment_id="experiment-2",
                run_id="langsmith-run-1",
                feedback_id="cloud-feedback-1",
            )
        )
    with pytest.raises(ValidationError):
        make_attempt(
            cloud_feedback=ExternalJudgeCloudFeedbackIdentity(
                experiment_id="experiment-1",
                run_id="langsmith-run-2",
                feedback_id="cloud-feedback-1",
            )
        )
    with pytest.raises(ValidationError):
        make_attempt(cloud_feedback=None)
    with pytest.raises(ValidationError):
        make_attempt(
            feedback=feedback(status="pending"),
        )


def test_scoring_attempt_accepts_unreported_model_version():
    attempt = make_attempt(
        model=ExternalJudgeModelIdentity(
            provider="provider-x",
            evaluator="evaluator-1",
            model_alias="judge-latest",
            model_version=UnreportedModelVersion(kind="unreported"),
            config_sha256="e" * 64,
        ),
        feedback=feedback(status="pending"),
        cloud_feedback=None,
    )
    assert attempt.model.model_version.kind == "unreported"
    assert attempt.feedback.score is None


def test_external_and_legacy_contracts_use_independent_versions():
    assert EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION != (
        EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION
    )
    assert get_args(ExternalJudgeCheckpointSchemaVersion) == (
        EXTERNAL_JUDGE_CHECKPOINT_SCHEMA_VERSION,
    )
    assert get_args(ExternalJudgeFeedbackSchemaVersion) == (
        EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION,
    )
    assert GENERATION_METRICS == ("faithfulness", "factual_correctness")
    legacy = GenerationCheckpoint(
        checkpoint=LINK,
        user_input=UserInputEvidence(message="问题"),
        answer=AnswerEvidence(visible_text="回答"),
        model_input=ModelInputContext(
            items=[
                ContextItem(
                    source="user_message", content="问题", fact_origin="user"
                )
            ]
        ),
        reference=reference(),
        applicability=[
            MetricApplicability(metric="faithfulness", applicable=True),
            MetricApplicability(metric="factual_correctness", applicable=True),
        ],
    )
    assert GenerationCheckpoint.model_validate_json(
        legacy.model_dump_json()
    ) == legacy
    assert MetricOutcome(
        metric="faithfulness", status="succeeded", score=0.0
    ).score == 0.0
    with pytest.raises(ValidationError):
        make_upload(schema_version="fit-agent-generation-upload-payload-1")
    with pytest.raises(ValidationError):
        make_upload(schema_version=EXTERNAL_JUDGE_FEEDBACK_SCHEMA_VERSION)
    with pytest.raises(ValidationError):
        ExternalJudgeFeedbackSet(
            checkpoint=LINK,
            execution_sha256=EXECUTION_SHA,
            feedback=[feedback(), feedback(metric="reference_requirements_met_v1")],
        )
    with pytest.raises(ValidationError):
        GenerationCheckpoint(**legacy.model_dump(), schema_version="external")


def test_request_boundary_type_is_shared_across_evidence_modules():
    from evaluation import contracts as contract_module
    from evaluation import scenario_evidence as evidence_module
    from evaluation import scenarios as scenario_module

    assert not hasattr(evidence_module, "RequestBoundaryEvidence")
    assert (
        scenario_module.RequestBoundaryEvidence
        is contract_module.RequestBoundaryEvidence
    )
    assert MessageNode.__module__ == "evaluation.contracts"
    assert ToolNode.__module__ == "evaluation.contracts"
    assert (
        RequestBoundaryEvidence.model_config
        == contract_module.EvaluationContract.model_config
    )


def test_new_contracts_reject_unknown_fields_and_ragas_labels():
    with pytest.raises(ValidationError):
        ExternalJudgeFeedback(
            metric="evidence_supported_v1",
            status="succeeded",
            score=1.0,
            ragas_metric="faithfulness",
        )
    with pytest.raises(ValidationError):
        make_upload(unknown=True)
    schemas = " ".join(
        str(model.model_json_schema())
        for model in (
            ExternalJudgeFeedback,
            ExternalJudgeFeedbackSet,
            ExternalJudgeScoringAttempt,
            ExternalJudgeCheckpointUpload,
        )
    )
    assert "ragas" not in schemas.lower()
