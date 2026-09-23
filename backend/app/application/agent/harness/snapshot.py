from collections.abc import Sequence
from datetime import date

from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.tools.common import ToolCallRecord
from app.application.ports import ToolCacheRevisions
from app.domain.plans.schema import ToolEvidence

#: 每个计划 Intent 在模型出候选前必须读到的事实；缺项即明确失败，不由模型自行省略。
PLAN_REQUIRED_FACTS: dict[str, tuple[str, ...]] = {
    "generate_plan": (
        "read_user_profile",
        "read_training_history",
        "read_progress",
        "search_exercises",
    ),
    "adjust_plan": (
        "read_user_profile",
        "read_training_history",
        "read_progress",
        "search_exercises",
        "read_active_plan",
        "read_training_calendar",
    ),
}

#: 工具名 → 它读取的事实域 revision：每条 ToolEvidence 都出自这张表。
PLAN_TOOL_REVISION_DOMAINS: dict[str, tuple[str, ...]] = {
    "read_user_profile": ("profile",),
    "read_training_history": ("workouts",),
    "read_progress": ("workouts",),
    "search_exercises": ("catalog",),
    "read_active_plan": ("plans", "catalog"),
    "read_training_calendar": ("plans", "workouts"),
}

#: ToolExecutionContext 的四个 revision 域；计划路径读候选前要求全部在位。
PLAN_REVISION_DOMAINS: tuple[str, ...] = ("profile", "workouts", "plans", "catalog")

#: 目录域 revision 由 ``PRAGMA user_version`` 承担，不读 tool_cache_revisions。
CATALOG_REVISION_DOMAIN: str = "catalog"

#: 事实域 → ToolExecutionContext 上的 revision 字段。
TOOL_CONTEXT_REVISION_FIELDS: dict[str, str] = {
    "profile": "profile_revision",
    "workouts": "workouts_revision",
    "plans": "plans_revision",
    "catalog": "catalog_revision",
}


class MissingPlanFacts(ValueError):
    """计划 Intent 的必需事实未全部读到：明确失败，不把缺项的候选交给评审。"""


def plan_fact_evidence(
    intent: str | None,
    *,
    snapshot: ToolExecutionContext,
    tool_calls: Sequence[ToolCallRecord],
) -> tuple[ToolEvidence, ...]:
    """候选的事实证据：必需事实按真实调用轨迹校验，每条证据带读取时的事实域 revision。"""
    required = PLAN_REQUIRED_FACTS.get(intent or "")
    if required is None:
        raise ValueError(f"未登记的计划 Intent：{intent!r}")
    called = [record.tool_name for record in tool_calls]
    missing = [name for name in required if name not in set(called)]
    if missing:
        raise MissingPlanFacts(
            f"{intent} 缺少必需事实，候选不得交给评审：{missing}"
        )
    return tuple(
        ToolEvidence(
            tool_name=name,
            revision_domain=domain,
            revision=getattr(snapshot, TOOL_CONTEXT_REVISION_FIELDS[domain]),
        )
        for name in dict.fromkeys(called)
        for domain in PLAN_TOOL_REVISION_DOMAINS[name]
    )


async def run_fact_snapshot(
    revisions: ToolCacheRevisions,
    *,
    schema_version: int,
    user_id: str,
    run_id: str,
    business_day: date,
) -> ToolExecutionContext:
    """本次读取的事实快照键：身份来自 State，三个 revision 从只读端口读，catalog 用迁移版本。"""
    reads = await revisions.read_all()
    missing = [
        domain
        for domain in PLAN_REVISION_DOMAINS
        if domain != CATALOG_REVISION_DOMAIN and domain not in reads
    ]
    if missing:
        raise ValueError(f"计划路径缺少事实域 revision：{missing}")
    return ToolExecutionContext(
        user_id=user_id,
        run_id=run_id,
        business_day=business_day,
        profile_revision=reads["profile"],
        workouts_revision=reads["workouts"],
        plans_revision=reads["plans"],
        catalog_revision=schema_version,
    )
