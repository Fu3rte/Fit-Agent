from collections.abc import Callable
from threading import Event

from app.ai.types import check_cancelled
from app.application.business import retrieval_frozen, retrieval_text
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.plans import PlanService
from app.application.business.profile import ProfileService
from app.application.business.proposals import PendingProposalService
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.application.business.retrieval_response import (
    SearchObservation,
    build_details_result,
    build_search_stage,
)
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.workouts import WorkoutService
from app.application.session.queries import SessionQueries
from app.domain.business.models import (
    CatalogSearchArguments,
    ExerciseDetailResult,
    ExerciseDetailsArguments,
    ExerciseSearchResult,
)
from app.domain.business.repository import BusinessRepository


class BusinessService:
    def __init__(
        self,
        repository: BusinessRepository,
        catalog: Catalog,
        sessions: SessionQueries,
        replacements: ReplacementCoordinator,
        retrieval: ActionRetrievalRuntime | None = None,
        search_observer: Callable[[SearchObservation], None] | None = None,
    ) -> None:
        self._catalog = catalog
        # 未显式注入时使用惰性空的检索运行时：缺省配置或 Milvus 不可用只影响检索请求。
        self._retrieval = retrieval if retrieval is not None else ActionRetrievalRuntime()
        # 只读阶段观测：默认关闭，生产响应与执行结果不受影响。
        self._search_observer = search_observer
        self._mappings = retrieval_text.load_mappings()
        self.profile = ProfileService(repository, catalog, sessions, replacements)
        self.plans = PlanService(repository, catalog, sessions, replacements)
        self.workouts = WorkoutService(repository, catalog, sessions, replacements)
        self.proposals = PendingProposalService(
            repository, sessions, self.profile, self.plans, self.workouts
        )

    # 动作查询：画像条件、检索运行时与目录响应共用同一入口。

    async def search_exercises(
        self,
        arguments: CatalogSearchArguments,
        signal: Event | None = None,
        tool_call_id: str | None = None,
    ) -> ExerciseSearchResult:
        # 先以短事务读取真实画像并立即释放，不持有事务等待 Embedding 或 Milvus 网络调用。
        check_cancelled(signal)
        profile = (await self.profile.get_profile()).content
        check_cancelled(signal)
        evaluated = evaluate_search_conditions(
            arguments, profile, self._catalog, self._mappings
        )
        execution = await self._retrieval.search(
            arguments.name,
            conditions=evaluated.conditions,
            limit=retrieval_frozen.RECALL_LIMIT,
            signal=signal,
        )
        stage = build_search_stage(
            execution.candidates,
            evaluated,
            rerank=execution.rerank,
            rerank_identity=execution.rerank_identity,
            threshold=retrieval_frozen.THRESHOLD,
        )
        if self._search_observer is not None:
            self._search_observer(SearchObservation(
                tool_call_id=tool_call_id,
                arguments=arguments,
                stage=stage,
                collection=execution.collection,
                manifest_json=execution.manifest_json,
            ))
        return stage.response

    def exercise_details(
        self, arguments: ExerciseDetailsArguments
    ) -> ExerciseDetailResult:
        return build_details_result(arguments.ids, self._catalog)

    def exercise_equipment(self) -> dict[str, str]:
        return {exercise.id: exercise.equipment for exercise in self._catalog.all()}
