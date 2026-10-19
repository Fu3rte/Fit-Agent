import asyncio
import hashlib
import json
import math
import numbers
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import version
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator
from ragas import SingleTurnSample as _RagasSingleTurnSample
from ragas import __version__ as RAGAS_VERSION
from ragas import _analytics as ragas_analytics
from ragas.callbacks import ChainRun, RagasTracer
from ragas.llms.base import BaseRagasLLM, LangchainLLMWrapper
from ragas.metrics import FactualCorrectness, Faithfulness
from ragas.run_config import RunConfig

from app.agent.cache_observation import sanitize_endpoint
from evaluation.contracts import (
    GENERATION_METRICS,
    JUDGE_MODEL,
    JUDGE_MODELS,
    CheckpointResult,
    ContextItem,
    EvaluationContract,
    EvidenceLink,
    GenerationCheckpoint,
    GenerationMetric,
    JudgeIdentity,
    JudgeModel,
    MetricApplicability,
    MetricName,
    MetricOutcome,
    NonEmptyText,
    Sha256,
)

JUDGE_MODEL_VARIABLE = "FIT_AGENT_EVAL_JUDGE_MODEL"


class GenerationScoringError(Exception):
    pass


class JudgeConfigError(GenerationScoringError, ValueError):
    pass


class InvalidJudgeConfig(JudgeConfigError):
    pass


class InvalidTimeout(GenerationScoringError, ValueError):
    pass


class InvalidGenerationScore(GenerationScoringError, ValueError):
    pass


class MissingScoringInput(GenerationScoringError, ValueError):
    pass


class RagasTelemetryError(GenerationScoringError):
    pass


RAGAS_TELEMETRY_VARIABLE = "RAGAS_DO_NOT_TRACK"


def ensure_ragas_tracking_disabled() -> None:
    # Ragas 会缓存 do_not_track 的首次读取结果；覆盖环境值后必须清除缓存再校验。
    os.environ[RAGAS_TELEMETRY_VARIABLE] = "true"
    ragas_analytics.do_not_track.cache_clear()
    if ragas_analytics.do_not_track() is not True:
        raise RagasTelemetryError("无法关闭 Ragas 使用统计上报，拒绝执行生成评分")


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.model not in JUDGE_MODELS:
            raise InvalidJudgeConfig(
                f"裁判模型必须为 {JUDGE_MODELS} 之一，实际为 {self.model}"
            )
        if not self.base_url or self.base_url != self.base_url.strip():
            raise InvalidJudgeConfig("裁判服务地址必须为非空白内容")
        if not self.api_key or self.api_key != self.api_key.strip():
            raise InvalidJudgeConfig("裁判 API Key 必须为非空白内容")
        try:
            parts = urlsplit(self.base_url)
        except ValueError as error:
            raise InvalidJudgeConfig("裁判服务地址不是合法 URL") from error
        if parts.username is not None or parts.password is not None:
            raise InvalidJudgeConfig("裁判服务地址不得包含 userinfo 凭据")
        if parts.query or parts.fragment:
            raise InvalidJudgeConfig("裁判服务地址不得包含 query 或 fragment")
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL as error:
            raise InvalidJudgeConfig("裁判服务地址不是合法 URL") from error
        if url.scheme not in ("http", "https") or not url.host:
            raise InvalidJudgeConfig("裁判服务地址必须为 HTTP(S) URL")

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str,
        api_key: str,
        environ: Mapping[str, str] | None = None,
    ) -> "JudgeConfig":
        # 当前源码没有既定的裁判服务地址与凭据环境变量来源；
        # 地址与密钥由调用者从受保护配置注入，不猜测变量名，也不借用其他服务凭据。
        return cls(
            model=load_judge_model(environ),
            base_url=base_url,
            api_key=api_key,
        )


def load_judge_model(environ: Mapping[str, str] | None = None) -> str:
    source: Mapping[str, str] = os.environ if environ is None else environ
    value = (source.get(JUDGE_MODEL_VARIABLE) or "").strip()
    if not value:
        raise JudgeConfigError(f"缺少裁判模型配置: {JUDGE_MODEL_VARIABLE}")
    if value not in JUDGE_MODELS:
        raise JudgeConfigError(
            f"{JUDGE_MODEL_VARIABLE} 必须为 {JUDGE_MODELS} 之一，实际为 {value}"
        )
    return value


def validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise InvalidTimeout("timeout 必须是有限正数")
    try:
        number = float(value)
    except OverflowError as error:
        raise InvalidTimeout("timeout 必须是有限正数") from error
    if not math.isfinite(number) or number <= 0:
        raise InvalidTimeout("timeout 必须是有限正数")
    return number


def validate_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise InvalidGenerationScore(
            f"评分必须为实数，实际类型为 {type(value).__name__}"
        )
    try:
        number = float(value)
    except OverflowError as error:
        raise InvalidGenerationScore("评分必须为有限数值") from error
    if not math.isfinite(number):
        raise InvalidGenerationScore("评分必须为有限数值")
    if number < 0.0 or number > 1.0:
        raise InvalidGenerationScore(f"评分必须在 0 到 1 之间，实际为 {number}")
    return number


def failure_text(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        message = str(error)
        text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    else:
        text = error.strip()
    if not text:
        raise ValueError("失败依据不得为空")
    return text


def failed_outcome(metric: MetricName, error: BaseException | str) -> MetricOutcome:
    return MetricOutcome(metric=metric, status="failed", error=failure_text(error))


class ScoringInput(EvaluationContract):
    response: NonEmptyText
    user_input: NonEmptyText | None = None
    retrieved_contexts: list[NonEmptyText] | None = None
    reference: NonEmptyText | None = None
    contexts_truncated: bool = False
    contexts_omitted_chars: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "ScoringInput":
        if self.reference is not None:
            if (
                self.user_input is not None
                or self.retrieved_contexts is not None
                or self.contexts_truncated
                or self.contexts_omitted_chars is not None
            ):
                raise ValueError("事实正确性输入不得携带上下文、用户输入或裁剪信息")
        else:
            if self.user_input is None or self.retrieved_contexts is None:
                raise ValueError("忠实性输入必须携带用户输入与实际上下文")
            if self.contexts_truncated and self.contexts_omitted_chars is None:
                raise ValueError("上下文整体裁剪必须记录省略字符数")
            if not self.contexts_truncated and self.contexts_omitted_chars is not None:
                raise ValueError("上下文未整体裁剪时不得记录省略字符数")
        return self


class FaithfulnessParameters(EvaluationContract):
    kind: Literal["faithfulness"] = "faithfulness"
    name: NonEmptyText
    max_retries: int = Field(ge=0)


class FactualCorrectnessParameters(EvaluationContract):
    kind: Literal["factual_correctness"] = "factual_correctness"
    name: NonEmptyText
    mode: Literal["precision", "recall", "f1"]
    beta: float
    atomicity: Literal["low", "high"]
    coverage: Literal["low", "high"]
    language: NonEmptyText


MetricParameters: TypeAlias = Annotated[
    FaithfulnessParameters | FactualCorrectnessParameters,
    Field(discriminator="kind"),
]


class PromptFingerprint(EvaluationContract):
    name: NonEmptyText
    sha256: Sha256
    instruction: NonEmptyText
    language: NonEmptyText
    input_model: NonEmptyText
    output_model: NonEmptyText
    example_count: int = Field(ge=0)


class RagasRunConfigEvidence(EvaluationContract):
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_wait_seconds: float = Field(ge=0)
    max_workers: int = Field(ge=1)
    exception_types: list[NonEmptyText] = Field(min_length=1)


class JudgeCallEvidence(EvaluationContract):
    sequence: int = Field(ge=0)
    parent_run_id: NonEmptyText | None = None
    prompt_name: NonEmptyText | None = None
    prompt: NonEmptyText
    prompt_sha256: Sha256
    output: NonEmptyText | None = None
    error: NonEmptyText | None = None


class ChainStepEvidence(EvaluationContract):
    run_id: NonEmptyText
    parent_run_id: NonEmptyText | None = None
    name: NonEmptyText
    inputs_json: NonEmptyText
    outputs_json: NonEmptyText | None = None
    error: NonEmptyText | None = None


class GenerationMetricEvidence(EvaluationContract):
    checkpoint: EvidenceLink
    metric: GenerationMetric
    metric_class: NonEmptyText
    metric_parameters: MetricParameters
    metric_version: Sha256
    ragas_version: NonEmptyText
    dependency_versions: dict[str, NonEmptyText]
    judge_model: JudgeModel = JUDGE_MODEL
    judge_base_url: NonEmptyText
    judge_timeout_seconds: float = Field(gt=0)
    judge_sdk_max_retries: int = Field(ge=0)
    judge_temperature: float
    ragas_run_config: RagasRunConfigEvidence
    prompt_fingerprints: list[PromptFingerprint] = Field(min_length=1)
    prompt_digest: Sha256
    scoring_input: ScoringInput
    contexts: list[ContextItem] = Field(default_factory=list)
    model_input_truncated: bool = False
    model_input_omitted_chars: int | None = Field(default=None, ge=0)
    judge_calls: list[JudgeCallEvidence] = Field(default_factory=list)
    chain_steps: list[ChainStepEvidence] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0, le=1)
    error: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "GenerationMetricEvidence":
        if (self.score is None) == (self.error is None):
            raise ValueError("指标证据必须且只能包含评分或错误依据")
        if self.metric_parameters.kind != self.metric:
            raise ValueError("指标参数类型必须与指标名称一致")
        if self.metric == "faithfulness":
            if self.scoring_input.reference is not None:
                raise ValueError("忠实性证据不得把 reference 作为评分输入")
            if not self.contexts:
                raise ValueError("忠实性证据必须保留实际模型输入上下文")
        else:
            if self.scoring_input.reference is None:
                raise ValueError("事实正确性证据必须保留 reference 输入")
            if self.contexts:
                raise ValueError("事实正确性证据不得携带实际模型输入上下文")
            if self.model_input_truncated or self.model_input_omitted_chars is not None:
                raise ValueError("事实正确性证据不得携带上下文裁剪信息")
        if self.model_input_truncated and self.model_input_omitted_chars is None:
            raise ValueError("模型输入整体裁剪必须记录省略字符数")
        if not self.model_input_truncated and self.model_input_omitted_chars is not None:
            raise ValueError("模型输入未整体裁剪时不得记录省略字符数")
        return self


class GenerationScoringResult(EvaluationContract):
    result: CheckpointResult
    evidence: list[GenerationMetricEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_correspondence(self) -> "GenerationScoringResult":
        metrics = [item.metric for item in self.evidence]
        if len(set(metrics)) != len(metrics):
            raise ValueError("同一检查点的指标证据不得重复")
        succeeded = {
            outcome.metric: outcome.score
            for outcome in self.result.outcomes
            if outcome.status == "succeeded"
        }
        evidence_scores = {}
        for item in self.evidence:
            if item.checkpoint != self.result.checkpoint:
                raise ValueError("证据检查点身份必须与结果一致")
            if item.error is not None:
                raise ValueError("成功结果不得关联带 error 的证据")
            if item.score is None:
                raise ValueError("成功结果必须关联包含评分的证据")
            evidence_scores[item.metric] = item.score
        if set(evidence_scores) != set(succeeded):
            raise ValueError("证据必须与成功指标一一对应")
        for metric, score in succeeded.items():
            if evidence_scores[metric] != score:
                raise ValueError(f"{metric} 的结果分数与证据分数不一致")
        return self


class ScoringAttemptSnapshot(EvaluationContract):
    checkpoint: EvidenceLink
    metric: GenerationMetric
    status: Literal["in_progress", "succeeded", "failed"]
    metric_class: NonEmptyText
    metric_parameters: MetricParameters
    metric_version: Sha256
    ragas_version: NonEmptyText
    dependency_versions: dict[str, NonEmptyText]
    judge_model: JudgeModel = JUDGE_MODEL
    judge_base_url: NonEmptyText
    judge_timeout_seconds: float = Field(gt=0)
    judge_sdk_max_retries: int = Field(ge=0)
    judge_temperature: float
    ragas_run_config: RagasRunConfigEvidence
    prompt_fingerprints: list[PromptFingerprint] = Field(min_length=1)
    prompt_digest: Sha256
    scoring_input: ScoringInput
    contexts: list[ContextItem] = Field(default_factory=list)
    model_input_truncated: bool = False
    model_input_omitted_chars: int | None = Field(default=None, ge=0)
    judge_calls: list[JudgeCallEvidence] = Field(default_factory=list)
    chain_steps: list[ChainStepEvidence] = Field(default_factory=list)
    score: float | None = Field(default=None, ge=0, le=1)
    error: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> "ScoringAttemptSnapshot":
        if self.metric_parameters.kind != self.metric:
            raise ValueError("指标参数类型必须与指标名称一致")
        if self.metric == "faithfulness":
            if self.scoring_input.reference is not None:
                raise ValueError("忠实性尝试不得把 reference 作为评分输入")
            if not self.contexts:
                raise ValueError("忠实性尝试必须保留实际模型输入上下文")
        else:
            if self.scoring_input.reference is None:
                raise ValueError("事实正确性尝试必须保留 reference 输入")
            if self.contexts:
                raise ValueError("事实正确性尝试不得携带实际模型输入上下文")
            if self.model_input_truncated or self.model_input_omitted_chars is not None:
                raise ValueError("事实正确性尝试不得携带上下文裁剪信息")
        if self.model_input_truncated and self.model_input_omitted_chars is None:
            raise ValueError("模型输入整体裁剪必须记录省略字符数")
        if not self.model_input_truncated and self.model_input_omitted_chars is not None:
            raise ValueError("模型输入未整体裁剪时不得记录省略字符数")
        if self.status == "in_progress":
            if self.score is not None or self.error is not None:
                raise ValueError("进行中的尝试不得包含评分或错误依据")
        elif self.status == "succeeded":
            if self.score is None or self.error is not None:
                raise ValueError("成功尝试必须包含评分且不得包含错误依据")
        elif self.error is None:
            raise ValueError("失败尝试必须包含错误依据")
        return self


@dataclass
class MetricPlan:
    metric: GenerationMetric
    applicability: MetricApplicability
    sample: _RagasSingleTurnSample | None
    scoring_input: ScoringInput | None


@dataclass
class JudgeCall:
    run_id: str
    parent_run_id: str | None
    prompt: str
    output: str | None = None
    error: str | None = None
    prompt_name: str | None = None


class JudgeCallRecorder(BaseCallbackHandler):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[JudgeCall] = []
        self._by_run: dict[str, JudgeCall] = {}

    def _start(
        self, run_id: str, parent_run_id: str | None, prompt: str
    ) -> None:
        call = JudgeCall(
            run_id=run_id,
            parent_run_id=parent_run_id,
            prompt=prompt,
        )
        self.calls.append(call)
        self._by_run[run_id] = call

    def _end(self, run_id: str, response: Any) -> None:
        call = self._by_run.get(run_id)
        if call is None:
            return
        texts = []
        for generation_list in response.generations:
            for generation in generation_list:
                text = getattr(generation, "text", None)
                if text is not None:
                    texts.append(text)
        call.output = "\n".join(texts) if texts else None

    def _fail(self, run_id: str, error: BaseException) -> None:
        call = self._by_run.get(run_id)
        if call is not None:
            call.error = failure_text(error)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(
            str(run_id),
            str(parent_run_id) if parent_run_id is not None else None,
            "\n".join(prompts),
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        text = "\n".join(
            message.text for batch in messages for message in batch
        )
        self._start(
            str(run_id),
            str(parent_run_id) if parent_run_id is not None else None,
            text,
        )

    def on_llm_end(self, response: Any, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._end(str(run_id), response)

    def on_chat_model_end(
        self, response: Any, *, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        self._end(str(run_id), response)

    def on_llm_error(
        self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        self._fail(str(run_id), error)

    def on_chat_model_error(
        self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        self._fail(str(run_id), error)


@dataclass
class ScoringAttempt:
    checkpoint: GenerationCheckpoint
    plan: MetricPlan
    metric: Faithfulness | FactualCorrectness
    judge: JudgeConfig
    run_config: RunConfig
    timeout: float
    recorder: JudgeCallRecorder
    tracer: RagasTracer
    status: Literal["in_progress", "succeeded", "failed"] = "in_progress"
    score: float | None = None
    error: str | None = None

    def _sync_prompt_names(self) -> None:
        for call in self.recorder.calls:
            if call.parent_run_id is None:
                continue
            chain = self.tracer.traces.get(call.parent_run_id)
            if chain is not None:
                call.prompt_name = chain.name

    @property
    def judge_calls(self) -> list[JudgeCallEvidence]:
        self._sync_prompt_names()
        return _judge_call_evidence(self.recorder.calls)

    @property
    def chain_steps(self) -> list[ChainStepEvidence]:
        return _chain_steps(self.tracer.traces)

    def succeed(self, score: float) -> None:
        if self.status != "in_progress":
            raise RuntimeError("评分尝试已结束")
        self.score = validate_score(score)
        self.error = None
        self.status = "succeeded"

    def fail(
        self,
        error: BaseException | str,
        *,
        preserve_score: bool = False,
    ) -> None:
        self.error = failure_text(error)
        if not preserve_score:
            self.score = None
        self.status = "failed"

    def snapshot(self) -> ScoringAttemptSnapshot:
        if self.plan.scoring_input is None:
            raise MissingScoringInput(f"指标未准备评分输入: {self.plan.metric}")
        parameters = metric_parameters(self.metric)
        fingerprints = prompt_fingerprints(self.metric)
        if self.plan.metric == "faithfulness":
            contexts = list(self.checkpoint.model_input.items)
            truncated = self.checkpoint.model_input.truncated
            omitted_chars = self.checkpoint.model_input.omitted_chars
        else:
            contexts = []
            truncated = False
            omitted_chars = None
        return ScoringAttemptSnapshot(
            checkpoint=self.checkpoint.checkpoint,
            metric=self.plan.metric,
            status=self.status,
            metric_class=type(self.metric).__name__,
            metric_parameters=parameters,
            metric_version=metric_version(self.metric, parameters),
            ragas_version=RAGAS_VERSION,
            dependency_versions=dependency_versions(),
            judge_model=self.judge.model,
            judge_base_url=sanitize_endpoint(self.judge.base_url),
            judge_timeout_seconds=validate_timeout(self.timeout),
            judge_sdk_max_retries=0,
            judge_temperature=1e-8,
            ragas_run_config=ragas_run_config_evidence(self.run_config),
            prompt_fingerprints=fingerprints,
            prompt_digest=prompt_digest(fingerprints),
            scoring_input=self.plan.scoring_input,
            contexts=contexts,
            model_input_truncated=truncated,
            model_input_omitted_chars=omitted_chars,
            judge_calls=self.judge_calls,
            chain_steps=self.chain_steps,
            score=self.score,
            error=self.error,
        )

    def to_evidence(self) -> GenerationMetricEvidence:
        if self.status == "in_progress":
            raise RuntimeError("评分尝试尚未结束")
        return build_metric_evidence(
            self.checkpoint,
            self.plan,
            self.metric,
            self.judge,
            self.run_config,
            timeout=self.timeout,
            score=self.score if self.status == "succeeded" else None,
            error=self.error if self.status == "failed" else None,
            judge_calls=self.recorder.calls,
            trace=self.tracer.traces,
        )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("证据不允许非有限数值")
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("证据不允许非有限数值")
        return number
    if hasattr(value, "item"):
        return _json_safe(value.item())
    raise TypeError(f"无法序列化证据类型: {type(value).__name__}")


def dependency_versions() -> dict[str, str]:
    return {
        "ragas": version("ragas"),
        "langchain-core": version("langchain-core"),
        "langchain-openai": version("langchain-openai"),
        "openai": version("openai"),
    }


def prompt_fingerprints(metric: Faithfulness | FactualCorrectness) -> list[PromptFingerprint]:
    fingerprints = []
    prompts = metric.get_prompts()
    for name in sorted(prompts):
        prompt = prompts[name]
        payload = {
            "name": prompt.name,
            "language": prompt.language,
            "instruction": prompt.instruction,
            "input_model": prompt.input_model.__name__,
            "output_model": prompt.output_model.__name__,
            "input_schema": prompt.input_model.model_json_schema(),
            "output_schema": prompt.output_model.model_json_schema(),
            "examples": [
                {
                    "input": input_model.model_dump(mode="json"),
                    "output": output_model.model_dump(mode="json"),
                }
                for input_model, output_model in prompt.examples
            ],
        }
        fingerprints.append(
            PromptFingerprint(
                name=prompt.name,
                sha256=_sha256_text(_canonical_json(payload)),
                instruction=prompt.instruction,
                language=prompt.language,
                input_model=prompt.input_model.__name__,
                output_model=prompt.output_model.__name__,
                example_count=len(prompt.examples),
            )
        )
    return fingerprints


def prompt_digest(fingerprints: Sequence[PromptFingerprint]) -> str:
    payload = [
        {"name": item.name, "sha256": item.sha256}
        for item in sorted(fingerprints, key=lambda item: item.name)
    ]
    return _sha256_text(_canonical_json(payload))


def metric_parameters(
    metric: Faithfulness | FactualCorrectness,
) -> FaithfulnessParameters | FactualCorrectnessParameters:
    if isinstance(metric, Faithfulness):
        return FaithfulnessParameters(
            name=metric.name,
            max_retries=metric.max_retries,
        )
    if isinstance(metric, FactualCorrectness):
        return FactualCorrectnessParameters(
            name=metric.name,
            mode=metric.mode,
            beta=metric.beta,
            atomicity=metric.atomicity,
            coverage=metric.coverage,
            language=metric.language,
        )
    raise TypeError(f"不支持的 Ragas 指标类型: {type(metric).__name__}")


def metric_version(
    metric: Faithfulness | FactualCorrectness,
    parameters: FaithfulnessParameters | FactualCorrectnessParameters,
) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "metric": parameters.kind,
                "metric_class": type(metric).__name__,
                "parameters": parameters.model_dump(mode="json"),
                "ragas_version": dependency_versions()["ragas"],
            }
        )
    )


def _exception_type_names(value: Any) -> list[str]:
    if isinstance(value, tuple):
        return [
            item.__name__ if isinstance(item, type) else type(item).__name__
            for item in value
        ]
    return [value.__name__ if isinstance(value, type) else type(value).__name__]


def ragas_run_config_evidence(run_config: RunConfig) -> RagasRunConfigEvidence:
    return RagasRunConfigEvidence(
        timeout_seconds=float(run_config.timeout),
        max_retries=run_config.max_retries,
        max_wait_seconds=float(run_config.max_wait),
        max_workers=run_config.max_workers,
        exception_types=_exception_type_names(run_config.exception_types),
    )


def build_metric_plan(checkpoint: GenerationCheckpoint) -> tuple[MetricPlan, ...]:
    applicability = {row.metric: row for row in checkpoint.applicability}
    if set(applicability) != set(GENERATION_METRICS):
        raise MissingScoringInput("生成检查点必须逐项声明 Faithfulness 与 FactualCorrectness")
    ragas_input = (
        checkpoint.ragas_input()
        if any(row.applicable for row in applicability.values())
        else None
    )
    plans = []
    for metric in GENERATION_METRICS:
        row = applicability[metric]
        if not row.applicable:
            plans.append(MetricPlan(metric, row, None, None))
            continue
        if ragas_input is None:
            raise MissingScoringInput(f"适用指标缺少 Ragas 输入: {metric}")
        if metric == "faithfulness":
            contexts = list(ragas_input.contexts)
            sample = _RagasSingleTurnSample(
                user_input=checkpoint.user_input.message,
                response=ragas_input.answer,
                retrieved_contexts=contexts,
            )
            scoring_input = ScoringInput(
                response=ragas_input.answer,
                user_input=checkpoint.user_input.message,
                retrieved_contexts=contexts,
                contexts_truncated=checkpoint.model_input.truncated,
                contexts_omitted_chars=checkpoint.model_input.omitted_chars,
            )
        elif metric == "factual_correctness":
            sample = _RagasSingleTurnSample(
                response=ragas_input.answer,
                reference=ragas_input.reference,
            )
            scoring_input = ScoringInput(
                response=ragas_input.answer,
                reference=ragas_input.reference,
            )
        else:
            raise ValueError(f"未知生成指标: {metric}")
        plans.append(MetricPlan(metric, row, sample, scoring_input))
    return tuple(plans)


def _build_metric(metric: GenerationMetric) -> Faithfulness | FactualCorrectness:
    if metric == "faithfulness":
        return Faithfulness()
    if metric == "factual_correctness":
        return FactualCorrectness()
    raise ValueError(f"未知生成指标: {metric}")


def _chain_steps(trace: Mapping[str, ChainRun] | None) -> list[ChainStepEvidence]:
    steps = []
    for chain in (trace or {}).values():
        if not isinstance(chain, ChainRun):
            raise TypeError("trace 必须由 Ragas ChainRun 组成")
        steps.append(
            ChainStepEvidence(
                run_id=chain.run_id,
                parent_run_id=chain.parent_run_id,
                name=chain.name,
                inputs_json=_canonical_json(_json_safe(chain.inputs)),
                outputs_json=(
                    _canonical_json(_json_safe(chain.outputs))
                    if chain.outputs
                    else None
                ),
            )
        )
    return steps


def _judge_call_evidence(calls: Sequence[JudgeCall]) -> list[JudgeCallEvidence]:
    return [
        JudgeCallEvidence(
            sequence=index,
            parent_run_id=call.parent_run_id,
            prompt_name=call.prompt_name,
            prompt=call.prompt,
            prompt_sha256=_sha256_text(call.prompt),
            output=call.output,
            error=call.error,
        )
        for index, call in enumerate(calls)
    ]


def build_metric_evidence(
    checkpoint: GenerationCheckpoint,
    plan: MetricPlan,
    metric: Faithfulness | FactualCorrectness,
    judge: JudgeConfig,
    run_config: RunConfig,
    *,
    timeout: float,
    score: float | None = None,
    error: str | None = None,
    judge_calls: Sequence[JudgeCall] = (),
    trace: Mapping[str, ChainRun] | None = None,
) -> GenerationMetricEvidence:
    if plan.sample is None or plan.scoring_input is None:
        raise MissingScoringInput(f"指标未准备评分输入: {plan.metric}")
    effective_timeout = validate_timeout(timeout)
    if score is None and error is None:
        raise ValueError("指标证据必须包含评分或错误依据")
    if score is not None and error is not None:
        raise ValueError("指标证据不得同时包含评分与错误依据")
    if score is not None:
        score = validate_score(score)
    if error is not None:
        error = failure_text(error)
    parameters = metric_parameters(metric)
    fingerprints = prompt_fingerprints(metric)
    contexts = (
        list(checkpoint.model_input.items)
        if plan.metric == "faithfulness"
        else []
    )
    return GenerationMetricEvidence(
        checkpoint=checkpoint.checkpoint,
        metric=plan.metric,
        metric_class=type(metric).__name__,
        metric_parameters=parameters,
        metric_version=metric_version(metric, parameters),
        ragas_version=RAGAS_VERSION,
        dependency_versions=dependency_versions(),
        judge_model=judge.model,
        judge_base_url=sanitize_endpoint(judge.base_url),
        judge_timeout_seconds=effective_timeout,
        judge_sdk_max_retries=0,
        judge_temperature=1e-8,
        ragas_run_config=ragas_run_config_evidence(run_config),
        prompt_fingerprints=fingerprints,
        prompt_digest=prompt_digest(fingerprints),
        scoring_input=plan.scoring_input,
        contexts=contexts,
        model_input_truncated=(
            checkpoint.model_input.truncated
            if plan.metric == "faithfulness"
            else False
        ),
        model_input_omitted_chars=(
            checkpoint.model_input.omitted_chars
            if plan.metric == "faithfulness"
            else None
        ),
        judge_calls=_judge_call_evidence(judge_calls),
        chain_steps=_chain_steps(trace),
        score=score,
        error=error,
    )


def run_judge_identity(
    evidence: Sequence[GenerationMetricEvidence],
) -> JudgeIdentity:
    items = list(evidence)
    if not items:
        raise ValueError("生成评分身份至少需要一个指标证据")
    models = {item.judge_model for item in items}
    if len(models) != 1:
        raise ValueError(f"同一运行存在多个裁判模型身份: {sorted(models)}")
    model = next(iter(models))
    prompt_version = _sha256_text(
        _canonical_json(sorted({item.prompt_digest for item in items}))
    )
    by_metric: dict[GenerationMetric, dict[str, set[str]]] = {}
    for item in items:
        versions = by_metric.setdefault(
            item.metric,
            {"metric": set(), "prompt": set()},
        )
        versions["metric"].add(item.metric_version)
        versions["prompt"].add(item.prompt_digest)
    pairs = []
    for metric in sorted(by_metric):
        versions = by_metric[metric]
        if len(versions["metric"]) != 1:
            raise ValueError(f"同一指标的配置版本冲突: {metric}")
        if len(versions["prompt"]) != 1:
            raise ValueError(f"同一指标的 prompt 版本冲突: {metric}")
        pairs.append([metric, next(iter(versions["metric"]))])
    metric_version_text = _sha256_text(_canonical_json(pairs))
    return JudgeIdentity(
        model=model,
        prompt_version=prompt_version,
        metric_version=metric_version_text,
    )


class GenerationScoringSession:
    def __init__(
        self,
        judge: JudgeConfig | None = None,
        *,
        timeout: float | None = None,
        on_outcome: Callable[[MetricOutcome], None] | None = None,
    ) -> None:
        self._judge = judge
        self._timeout = timeout
        self._on_outcome = on_outcome
        self._http: httpx.AsyncClient | None = None
        self._chat: ChatOpenAI | None = None
        self._llm: BaseRagasLLM | None = None
        self._run_config: RunConfig | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._outcomes: list[MetricOutcome] = []
        self._evidence: list[GenerationMetricEvidence] = []
        self._attempts: list[ScoringAttempt] = []
        self._current_attempt: ScoringAttempt | None = None

    @property
    def outcomes(self) -> tuple[MetricOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def evidence(self) -> tuple[GenerationMetricEvidence, ...]:
        return tuple(self._evidence)

    @property
    def attempts(self) -> tuple[ScoringAttempt, ...]:
        return tuple(self._attempts)

    @property
    def current_attempt(self) -> ScoringAttempt | None:
        return self._current_attempt

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> "GenerationScoringSession":
        if self._closed:
            raise RuntimeError("生成评分会话已关闭")
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        chat = self._chat
        self._chat = None
        self._llm = None
        self._run_config = None
        if chat is not None:
            root_async_client = getattr(chat, "root_async_client", None)
            if root_async_client is not None:
                await root_async_client.close()
        http = self._http
        self._http = None
        if http is not None:
            await http.aclose()
        self._loop = None

    async def _ensure_llm(self) -> BaseRagasLLM:
        if self._closed:
            raise RuntimeError("生成评分会话已关闭")
        if self._judge is None:
            raise JudgeConfigError("适用生成指标时必须提供裁判配置")
        if self._timeout is None:
            raise InvalidTimeout("适用生成指标时必须显式提供有限正数 timeout")
        timeout = validate_timeout(self._timeout)
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("生成评分会话必须在创建客户端的事件循环中使用")
        if self._llm is not None:
            return self._llm
        # 关闭 Ragas 使用统计上报；评分证据只写入本地运行目录。
        ensure_ragas_tracking_disabled()
        self._loop = loop
        http = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._http = http
        chat = ChatOpenAI(
            model=self._judge.model,
            api_key=self._judge.api_key,
            base_url=self._judge.base_url,
            timeout=timeout,
            max_retries=0,
            temperature=0.0,
            http_async_client=http,
        )
        run_config = RunConfig(timeout=timeout)
        self._chat = chat
        self._run_config = run_config
        self._llm = LangchainLLMWrapper(chat, run_config=run_config)
        return self._llm

    async def _score_item(
        self, checkpoint: GenerationCheckpoint, plan: MetricPlan
    ) -> tuple[MetricOutcome, GenerationMetricEvidence]:
        if plan.sample is None or plan.scoring_input is None:
            raise MissingScoringInput(f"指标未准备评分输入: {plan.metric}")
        llm = await self._ensure_llm()
        judge = self._judge
        if judge is None:
            raise JudgeConfigError("适用生成指标时必须提供裁判配置")
        metric = _build_metric(plan.metric)
        metric.llm = llm
        if self._run_config is None:
            raise RuntimeError("评分会话缺少 RunConfig")
        metric.init(self._run_config)
        tracer = RagasTracer()
        recorder = JudgeCallRecorder()
        timeout = validate_timeout(self._timeout)
        attempt = ScoringAttempt(
            checkpoint=checkpoint,
            plan=plan,
            metric=metric,
            judge=judge,
            run_config=self._run_config,
            timeout=timeout,
            recorder=recorder,
            tracer=tracer,
        )
        self._current_attempt = attempt
        self._attempts.append(attempt)
        try:
            raw_score = await metric.single_turn_ascore(
                plan.sample,
                callbacks=[tracer, recorder],
                timeout=timeout,
            )
            score = validate_score(raw_score)
        except BaseException as error:
            attempt.fail(error)
            raise
        attempt.succeed(score)
        try:
            evidence = attempt.to_evidence()
        except BaseException as error:
            attempt.fail(error, preserve_score=True)
            raise
        outcome = MetricOutcome(metric=plan.metric, status="succeeded", score=score)
        return outcome, evidence

    async def score(self, checkpoint: GenerationCheckpoint) -> CheckpointResult:
        if self._closed:
            raise RuntimeError("生成评分会话已关闭")
        plans = build_metric_plan(checkpoint)
        outcomes = []
        for plan in plans:
            if not plan.applicability.applicable:
                outcome = MetricOutcome(
                    metric=plan.metric,
                    status="not_applicable",
                    reason=plan.applicability.reason,
                )
                outcomes.append(outcome)
                self._outcomes.append(outcome)
                continue
            outcome, evidence = await self._score_item(checkpoint, plan)
            outcomes.append(outcome)
            self._outcomes.append(outcome)
            self._evidence.append(evidence)
            if self._on_outcome is not None:
                self._on_outcome(outcome)
        return CheckpointResult(
            checkpoint=checkpoint.checkpoint,
            outcomes=outcomes,
        )


def _not_applicable_result(
    checkpoint: GenerationCheckpoint,
    plans: Sequence[MetricPlan],
) -> CheckpointResult:
    return CheckpointResult(
        checkpoint=checkpoint.checkpoint,
        outcomes=[
            MetricOutcome(
                metric=plan.metric,
                status="not_applicable",
                reason=plan.applicability.reason,
            )
            for plan in plans
        ],
    )


async def _score_impl(
    checkpoint: GenerationCheckpoint,
    judge: JudgeConfig | None,
    timeout: float | None,
) -> tuple[CheckpointResult, list[GenerationMetricEvidence]]:
    plans = build_metric_plan(checkpoint)
    if not any(plan.applicability.applicable for plan in plans):
        return _not_applicable_result(checkpoint, plans), []
    async with GenerationScoringSession(judge, timeout=timeout) as session:
        result = await session.score(checkpoint)
        return result, list(session.evidence)


async def score_checkpoint(
    checkpoint: GenerationCheckpoint,
    judge: JudgeConfig | None = None,
    *,
    timeout: float | None = None,
) -> CheckpointResult:
    result, _ = await _score_impl(checkpoint, judge, timeout)
    return result


async def score_checkpoint_with_evidence(
    checkpoint: GenerationCheckpoint,
    judge: JudgeConfig | None = None,
    *,
    timeout: float | None = None,
) -> GenerationScoringResult:
    result, evidence = await _score_impl(checkpoint, judge, timeout)
    return GenerationScoringResult(result=result, evidence=evidence)
