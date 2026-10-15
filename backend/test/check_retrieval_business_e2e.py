import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import model_validator

from app.agent.tool import AgentTool
from app.agent.tools.common import envelope, unbound
from app.application.business.retrieval_embedding import EmbeddingClient
from app.domain.business.models import BusinessModel

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
BATCH = REPO / "tmp" / "action-retrieval-t8-business" / "batch-r1"

MANIFEST = BATCH / "manifest"
SELFCHECK = BATCH / "selfcheck"
SAMPLES = BATCH / "samples"
PRECONDITIONS = BATCH / "preconditions"
DATABASES = BATCH / "databases"
USAGE = BATCH / "usage"
ISOLATED = BATCH / "isolated"
CHAIN = BATCH / "chain"
RESTART = BATCH / "restart"
FAULTS = BATCH / "faults"

HEAD_COMMIT = "ebb1635ffcfc9a981f179cec102ff16c0169e92f"
LEGACY_BLOB_PATH = "backend/app/agent/tools/exercises.py"

# 本轮保护上限：尝试数在发送前登记，触顶由驱动在下一条命令前拦截。
# token 口径由用户裁决：聊天输入上限只统计非缓存输入 tokens；cache 读写单独记录，不计入上限。
CAPS = {
    "chat_attempts": 260,
    "embedding_attempts": 40,
    "summary_attempts": 6,
    "chat_prompt_tokens": 2_000_000,
    "chat_completion_tokens": 128_000,
    "embedding_tokens": 12_000,
}
PER_SAMPLE_CAPS = {"model_requests": 8, "search_calls": 4, "details_calls": 2}

SCENARIOS = ("verify", "generate", "replace")
GROUPS = ("baseline", "optimized")
REPEATS = (1, 2, 3)

USER_INPUTS = {
    "verify": "杠铃耸肩需要用到什么器械？具体怎么做？",
    "generate": "帮我安排一个练背的训练日。",
    "replace": "今天没有哑铃，把练背那个动作换掉。",
}

# 前置状态只能由真实模型整理并经真实确认入口保存；指令逐字固定，两组共用。
# 单项场景写成 ('…',)：括号内相邻字面量缺尾逗号会退化为 str，按字符迭代出单字回合。
GENERATE_PRECONDITION_PROMPT = (
    "我在家训练，只能用自重，长期用不了杠铃，没有需要长期禁用的具体动作。"
    "目标是增肌，中级训练经验，每周练三次。请按这些信息更新我的画像。"
)
REPLACE_PRECONDITION_PROMPTS = (
    "我在健身房训练，杠铃和哑铃等器械都能用，没有需要长期禁用的器械，"
    "也没有需要长期禁用的具体动作。目标是增肌，中级训练经验，每周练三次。"
    "请按这些信息更新我的画像。",
    "我练背的时候可以用哑铃，帮我安排一个练背的训练日。",
)
PRECONDITION_PROMPTS = {
    "generate": (GENERATE_PRECONDITION_PROMPT,),
    "replace": REPLACE_PRECONDITION_PROMPTS,
}
# 每场景前置完整回合数：以字面声明为准，展开后逐项核对，避免只比较两个同时错误的长度。
PRECONDITION_ROUNDS = {"generate": 1, "replace": 2}


def scenario_of(sample_id: str) -> str:
    return sample_id.rsplit("-", 1)[0].rsplit("-r", 1)[0]


def precondition_prompts(scenario: str) -> list[str]:
    # 展开前拒绝裸 str：先校验声明为 tuple，再逐项校验为长度大于 8 的完整非空字符串。
    declaration = PRECONDITION_PROMPTS[scenario]
    if not isinstance(declaration, tuple):
        raise RuntimeError(
            f"{scenario} 前置指令必须为 tuple[str, ...]，实际为 {type(declaration).__name__}"
        )
    prompts: list[str] = []
    for item in declaration:
        if not isinstance(item, str) or len(item) <= 8 or not item.strip():
            raise RuntimeError(f"{scenario} 前置指令必须为长度大于 8 的完整非空字符串")
        prompts.append(item)
    expected = PRECONDITION_ROUNDS[scenario]
    if len(prompts) != expected:
        raise RuntimeError(f"{scenario} 前置回合数应为 {expected}，实际 {len(prompts)}")
    return prompts

CATALOG_FACTS = {
    "0095": {"name": "杠铃耸肩", "equipment": "barbell", "body_part": "back", "target": "traps"},
    "0406": {"name": "哑铃耸肩", "equipment": "dumbbell", "body_part": "back", "target": "traps"},
}

PROFILE_FIXTURES = {
    "generate": {
        "goal": "增肌",
        "experience": "中级",
        "environment": "家庭",
        "availability": "每周三次",
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": ["barbell"],
        "forbidden_exercise_ids": [],
    },
    "replace": {
        "goal": "增肌",
        "experience": "中级",
        "environment": "健身房",
        "availability": "每周三次",
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": [],
        "forbidden_exercise_ids": [],
    },
}

# 旧口径的检索工具面：逐字取自 HEAD 提交，基线组只替换这一层，其余业务工具保持一致。
LEGACY_SEARCH_DESCRIPTION = (
    "按中文动作名称包含匹配检索只读动作目录，可用 equipment、body_part、"
    "target、muscle_group 精确筛选；返回匹配动作数组，每项包含九个目录字段。"
)


class LegacyCatalogSearchArguments(BusinessModel):
    name: str
    equipment: str | None = None
    body_part: str | None = None
    target: str | None = None
    muscle_group: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> "LegacyCatalogSearchArguments":
        if not self.name.strip():
            raise ValueError("name 必须包含非空白内容")
        return self


def sample_order() -> list[str]:
    return [
        f"{scenario}-r{repeat}-{group}"
        for repeat in REPEATS
        for scenario in SCENARIOS
        for group in GROUPS
    ]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_snapshot() -> dict:
    files = {
        path.relative_to(REPO).as_posix(): sha256_file(path)
        for base in (BACKEND / "app", BACKEND / "test")
        for path in sorted(base.rglob("*.py"))
    }
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    return {"head": head, "captured_at": now_iso(), "file_sha256": files}


def require_source_identity(stage: str) -> dict:
    # 冻结身份核对：任何漂移就地失败，不允许把漂移后的结果并入同一批证据。
    expected = read_json(MANIFEST / "source-before.json")
    current = source_snapshot()
    before_files = expected["file_sha256"]
    drift = {
        "changed": sorted(name for name, digest in before_files.items()
                          if name in current["file_sha256"] and current["file_sha256"][name] != digest),
        "added": sorted(set(current["file_sha256"]) - set(before_files)),
        "removed": sorted(set(before_files) - set(current["file_sha256"])),
        "head": {"frozen": expected["head"], "current": current["head"]},
    }
    if drift["changed"] or drift["added"] or drift["removed"] or expected["head"] != current["head"]:
        raise RuntimeError(f"源码身份漂移: {stage}: {json.dumps(drift, ensure_ascii=False)}")
    return {"frozen": expected, "drift": drift, "identity_unchanged": True}


def install_isolation(namespace: str, database_path: Path | None = None) -> dict:
    # 逐项把业务库、观测库、模型配置目录与缓存观测落点指向证据根，并回报实际落点。
    from app.infrastructure.persistence.sqlite import database as database_module
    from test import regression_support

    root = ISOLATED / namespace
    root.mkdir(parents=True, exist_ok=True)
    regression_support.TEMP_ROOT = root
    if database_path is None:
        path = regression_support.patch_default_database(f"db-{namespace}")
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        path = database_path
        database_module.default_database_path = lambda: path
    trace_path = regression_support.patch_trace_database(f"trace-{namespace}")
    from app.agent import cache_observation

    observation_root = root / "cache-observation"
    observation_root.mkdir(parents=True, exist_ok=True)
    # 观测根按命名空间复用：历史逐调用记录移入 previous 保留，判据只覆盖本次运行。
    stale = sorted(item for item in observation_root.glob("*.jsonl"))
    if stale:
        previous = observation_root / "previous"
        previous.mkdir(exist_ok=True)
        for item in stale:
            os.replace(item, previous / f"{now_ms()}-{item.name}")
    cache_observation.observation_directory = lambda: observation_root
    from app import model_config
    from app.domain.session import attachments

    model_root = root / "model-config"
    model_config.DATA_ROOT = model_root
    attachments.TMP_ROOT = root / "attachments"
    (root / "attachments").mkdir(parents=True, exist_ok=True)
    return {
        "namespace": namespace,
        "root": root.relative_to(BATCH).as_posix(),
        "database": path.relative_to(BATCH).as_posix(),
        "trace_database": trace_path.relative_to(BATCH).as_posix(),
        "model_config_root": model_root.relative_to(BATCH).as_posix(),
        "cache_observation_root": observation_root.relative_to(BATCH).as_posix(),
        "cache_observation_pruned": len(stale),
        "attachment_root": (root / "attachments").relative_to(BATCH).as_posix(),
    }


def new_counters() -> dict:
    def bucket(keys: tuple[str, ...]) -> dict:
        base = {"attempts": 0, "success": 0, "failure": 0, "calls": []}
        base.update({key: 0 for key in keys})
        base.update({f"{key}_unknown": 0 for key in keys})
        return base

    return {
        "chat": bucket(("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")),
        "summary": bucket(("prompt_tokens", "completion_tokens")),
        "embedding": bucket(("prompt_tokens", "total_tokens")),
    }


def record_usage(entry: dict, started_ms: int, fields: dict[str, int | None]) -> None:
    # usage 缺失记 unknown 并保留字符量线索，禁止补 0。
    for key, value in fields.items():
        if value is None:
            entry[f"{key}_unknown"] += 1
        else:
            entry[key] += value
    entry["calls"].append({"started_at_ms": started_ms, **fields})


def install_observation(counters: dict) -> None:
    # 三处透传观察：尝试在调用前登记，异常原样传播，绝不重试、绝不吞异常。
    import app.agent.agent_loop as agent_loop
    import app.agent.compaction as compaction

    stream_original = agent_loop.stream_assistant_response

    async def stream_counted(*args, **kwargs):
        entry = counters["chat"]
        entry["attempts"] += 1
        started = time.time_ns() // 1_000_000
        try:
            message = await stream_original(*args, **kwargs)
        except BaseException:
            entry["failure"] += 1
            record_usage(entry, started, {"prompt_tokens": None, "completion_tokens": None,
                                          "cache_read_tokens": None, "cache_write_tokens": None})
            raise
        entry["success"] += 1
        usage = getattr(message, "usage", None)
        record_usage(
            entry,
            started,
            {
                "prompt_tokens": getattr(usage, "input", None),
                "completion_tokens": getattr(usage, "output", None),
                "cache_read_tokens": getattr(usage, "cache_read", None),
                "cache_write_tokens": getattr(usage, "cache_write", None),
            },
        )
        return message

    agent_loop.stream_assistant_response = stream_counted
    complete_original = compaction.complete

    async def complete_counted(*args, **kwargs):
        entry = counters["summary"]
        entry["attempts"] += 1
        started = time.time_ns() // 1_000_000
        try:
            response = await complete_original(*args, **kwargs)
        except BaseException:
            entry["failure"] += 1
            record_usage(entry, started, {"prompt_tokens": None, "completion_tokens": None})
            raise
        entry["success"] += 1
        usage = getattr(response, "usage", None)
        record_usage(
            entry,
            started,
            {"prompt_tokens": getattr(usage, "input", None), "completion_tokens": getattr(usage, "output", None)},
        )
        return response

    compaction.complete = complete_counted
    embed_original = EmbeddingClient._embed

    async def embed_counted(self, texts):
        entry = counters["embedding"]
        entry["attempts"] += 1
        entry["texts"] = entry.get("texts", 0) + len(texts)
        started = time.time_ns() // 1_000_000
        try:
            response = await embed_original(self, texts)
        except BaseException:
            entry["failure"] += 1
            record_usage(entry, started, {"prompt_tokens": None, "total_tokens": None})
            raise
        entry["success"] += 1
        usage = getattr(response, "usage", None)
        record_usage(
            entry,
            started,
            {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        )
        return response

    EmbeddingClient._embed = embed_counted


def read_cache_observations(namespace: str) -> dict:
    from app.agent.cache_observation import read_observations, summarize_observations

    directory = ISOLATED / namespace / "cache-observation"
    records = read_observations(directory=directory)
    return {"records": len(records), "summary": summarize_observations(records)}


def read_trace_models(trace_path: Path) -> dict:
    # 第三方独立计数：隔离观测库中的 model Span 按 call_kind 分布；观测写入是故障隔离路径，缺失如实标注。
    if not trace_path.is_file():
        return {"model_spans": 0, "by_call_kind": {}, "trace_database_present": False}
    connection = sqlite3.connect(f"file:{trace_path}?mode=ro", uri=True)
    try:
        present = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='spans'"
        ).fetchone()[0]
        rows = connection.execute("SELECT attributes FROM spans WHERE name = 'model'").fetchall() if present else []
    finally:
        connection.close()
    kinds: dict[str, int] = {}
    for (attributes,) in rows:
        payload = json.loads(attributes) if isinstance(attributes, str) else attributes
        kind = str(payload.get("call_kind", "unknown"))
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"model_spans": len(rows), "by_call_kind": kinds, "trace_database_present": True}


def model_identity() -> dict:
    from app import model_config
    from app.model_config import _stored_settings, effective_provider

    environment = {
        "api": os.environ.get("FIT_AGENT_TEST_MODEL_API") or "openai-completions",
        "model": os.environ.get("FIT_AGENT_TEST_MODEL_ID"),
        "provider": os.environ.get("FIT_AGENT_TEST_MODEL_PROVIDER"),
        "base_url": os.environ.get("FIT_AGENT_TEST_MODEL_BASE_URL"),
        "api_key": os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY"),
    }
    missing = sorted(key for key in ("model", "base_url", "api_key") if not environment[key])
    if missing:
        raise RuntimeError("缺少测试模型环境变量: " + ", ".join(missing))
    # 与用户当前已选配置逐项比对：只读默认目录的选择文件，凭据文件保持不访问。
    original_root = model_config.DATA_ROOT
    model_config.DATA_ROOT = BACKEND / "data"
    try:
        saved = _stored_settings()
    finally:
        model_config.DATA_ROOT = original_root
    if saved is None:
        raise RuntimeError("默认模型配置缺失，无法核对身份")
    return {
        "api": environment["api"],
        "model": environment["model"],
        "provider": environment["provider"],
        "base_url_sha256": sha256_text(environment["base_url"]),
        "endpoint_fingerprint": sha256_text(
            "|".join((environment["api"], environment["base_url"], environment["model"]))
        ),
        "matches_saved_selection": {
            "api": environment["api"] == saved.api,
            "model": environment["model"] == saved.model,
            "provider": effective_provider(environment["api"], environment["provider"])
            == effective_provider(saved.api, saved.provider),
            "base_url": environment["base_url"] == saved.base_url,
        },
        "saved_base_url_sha256": sha256_text(saved.base_url),
        "credential_file_access": "not_required",
    }


def legacy_search_tool(catalog) -> AgentTool:
    def search_exercises(tool_call_id, params: LegacyCatalogSearchArguments, signal, on_update):
        # 旧口径直接落到同一 Catalog.search：名称子串 + 四项等值筛选，九字段完整数组，无预算。
        results = catalog.search(
            params.name,
            equipment=params.equipment,
            body_part=params.body_part,
            target=params.target,
            muscle_group=params.muscle_group,
        )
        return envelope([exercise.model_dump() for exercise in results])

    return AgentTool(
        "search_exercises",
        LEGACY_SEARCH_DESCRIPTION,
        LegacyCatalogSearchArguments,
        search_exercises,
        max_output_chars=None,
    )


def install_group(group: str) -> dict:
    # 基线组替换检索工具面；优化组保持生产接线原样。
    import app.interfaces.http as http

    if not hasattr(http, "_fit_agent_original_declarations"):
        http._fit_agent_original_declarations = http.business_tool_declarations
        http._fit_agent_original_bind = http.bind_business_tools
    original_declarations = http._fit_agent_original_declarations
    original_bind = http._fit_agent_original_bind

    if group == "optimized":
        http.business_tool_declarations = original_declarations
        http.bind_business_tools = original_bind
        return {"group": group, "surface": "production", "patched": False}

    def declarations():
        current = original_declarations()
        return [
            legacy_search_tool_placeholder().definition()
            if item.name == "search_exercises"
            else item
            for item in current
            if item.name != "get_exercise_details"
        ]

    def bind(service, context, call, prepared, workout_prepared, plan_prepared=None):
        tools = original_bind(service, context, call, prepared, workout_prepared, plan_prepared)
        tools.pop("get_exercise_details")
        tools["search_exercises"] = legacy_search_tool(service._catalog)
        return tools

    http.business_tool_declarations = declarations
    http.bind_business_tools = bind
    return {"group": group, "surface": "reconstructed-legacy-catalog-search", "patched": True}


def legacy_search_tool_placeholder():
    # 声明构造复用不可执行绑定实例，与生产声明入口同一口径。
    return AgentTool(
        "search_exercises",
        LEGACY_SEARCH_DESCRIPTION,
        LegacyCatalogSearchArguments,
        unbound,
        max_output_chars=None,
    )


def tool_surface_diff() -> dict:
    import app.interfaces.http as http

    production = {item.name: item for item in http._fit_agent_original_declarations()}
    legacy = legacy_search_tool_placeholder().definition()
    search = production["search_exercises"]
    return {
        "system_prompt_sha256": sha256_text(http.SYSTEM_PROMPT),
        "production_search_description": search.description,
        "legacy_search_description": LEGACY_SEARCH_DESCRIPTION,
        "legacy_only_parameters": sorted(set(legacy.parameters.get("properties", {})) -
                                         set(search.parameters.get("properties", {}))),
        "production_only_parameters": sorted(set(search.parameters.get("properties", {})) -
                                             set(legacy.parameters.get("properties", {}))),
        "legacy_returns": "九字段完整数组，无 items/has_more/remaining_ids 信封，无字符预算",
        "production_budget": {"search_chars": 4000, "details_chars": 7000, "recall_limit": 11},
        "details_tool_in_legacy": False,
        "legacy_binding_path": "测试内直接调用 Catalog.search；HEAD 旧实现经 BusinessService.search_exercises 透传同一方法",
    }


def ledger_append(namespace: str, counters: dict, extra: dict) -> None:
    USAGE.mkdir(parents=True, exist_ok=True)
    record = {
        "namespace": namespace,
        "recorded_at": now_iso(),
        "chat": {key: value for key, value in counters["chat"].items() if key != "calls"},
        "summary": {key: value for key, value in counters["summary"].items() if key != "calls"},
        "embedding": {key: value for key, value in counters["embedding"].items() if key != "calls"},
        **extra,
    }
    with (USAGE / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def legacy_blob_provenance() -> dict:
    blob = subprocess.run(
        ["git", "show", f"{HEAD_COMMIT}:{LEGACY_BLOB_PATH}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    # HEAD 把描述写成两段相邻字面量：去掉空白与字符串定界符后比对文本是否逐字来自该提交。
    compact = "".join(blob.split()).replace('"', "")
    return {
        "commit_matches_head": HEAD_COMMIT == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "description_verbatim": "".join(LEGACY_SEARCH_DESCRIPTION.split()) in compact,
        "arguments_verbatim": all(
            f"{field}:str|None=None" in compact
            for field in ("equipment", "body_part", "target", "muscle_group")
        ),
        "max_output_chars_none": "max_output_chars=None," in blob,
        "no_details_tool": "get_exercise_details" not in blob,
        "blob_sha256": sha256_text(blob),
    }


def guard_state() -> dict:
    root = os.environ.get("VALIDATION_ROOT")
    blocked = None
    if root:
        target = BACKEND / "data" / "trace.db"
        try:
            open(target, "rb").close()
        except BaseException as error:
            blocked = f"{type(error).__name__}: {error}"
    return {"validation_root": bool(root), "default_trace_open_blocked": blocked}


def discard_call(awaitable):
    awaitable.close()
    return None


def offline_service_gates(body_weight_plan: dict, barbell_plan: dict, profile_models: dict) -> dict:
    # 离线服务面核验：注册表与声明逐字段一致，真实保存校验对画像限制动作就地拒绝。
    import app.interfaces.http as http
    from app.application.business.service import BUSINESS_TIMEZONE, business_date
    from app.domain.business.errors import BusinessError
    from app.domain.business.models import BusinessContext
    from test.check_structured_confirmation import Fixture

    context = BusinessContext(
        timezone=BUSINESS_TIMEZONE,
        business_date=business_date(now_ms()),
        session_id="00000000-0000-4000-8000-000000000002",
        run_id="selfcheck",
        request_entry_id="selfcheck",
        source_entry_id="selfcheck",
    )
    declarations = http.business_tool_declarations()

    async def collect() -> dict:
        database = ISOLATED / "selfcheck-service" / "gate.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        database.unlink(missing_ok=True)
        fixture = Fixture(database)
        await fixture.seeded()
        try:
            registry = http.bind_business_tools(fixture.business, context, discard_call, {}, {}, None)
            accepted = fixture.business._validate_plan(
                body_weight_plan, profile_models["generate"], "generation"
            )
            try:
                fixture.business._validate_plan(
                    barbell_plan, profile_models["generate"], "generation"
                )
                rejected = None
            except BusinessError as error:
                rejected = error.code
            return {
                "declaration_names": [item.name for item in declarations],
                "declaration_definitions": {
                    item.name: {"description": item.description, "parameters": item.parameters}
                    for item in declarations
                },
                "registry_names": sorted(registry),
                "registry_definitions": {
                    name: {
                        "description": tool.definition().description,
                        "parameters": tool.definition().parameters,
                    }
                    for name, tool in registry.items()
                },
                "accepted_days": len(accepted.days),
                "rejected_code": rejected,
            }
        finally:
            await fixture.close()

    return asyncio.run(collect())


def selfcheck() -> dict:
    from app.application.business.catalog import Catalog
    from app.application.business.retrieval_conditions import evaluate_search_conditions
    from app.domain.business.models import (
        CatalogSearchArguments,
        PlanContent,
        ProfileContent,
    )
    from test.regression_support import run_tool

    # 先落隔离再导入服务面：模块级观测库连接必须指向隔离路径。
    landing = install_isolation("selfcheck")
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    details["isolation_landing"] = landing

    catalog = Catalog.load()
    legacy = legacy_search_tool(catalog)
    probes = [
        {"name": "杠铃耸肩"},
        {"name": "哑铃", "body_part": "back"},
        {"name": "耸肩", "equipment": "dumbbell"},
        {"name": "不存在的动作名"},
        {"name": "卧推"},
    ]
    mismatched = []
    for probe in probes:
        expected = catalog.search(
            probe["name"],
            equipment=probe.get("equipment"),
            body_part=probe.get("body_part"),
            target=probe.get("target"),
            muscle_group=probe.get("muscle_group"),
        )
        result = run_tool(legacy, dict(probe), declared={legacy.name: legacy.definition()})
        payload = json.loads("".join(block.text for block in result.content))
        if payload != [exercise.model_dump() for exercise in expected]:
            mismatched.append(probe["name"])
    checks["legacy_search_equals_catalog"] = not mismatched
    nine_field = json.loads(
        "".join(
            block.text
            for block in run_tool(legacy, {"name": "卧推"}, declared={legacy.name: legacy.definition()}).content
        )
    )
    checks["legacy_search_returns_nine_fields"] = bool(nine_field) and all(
        set(item) == {
            "id", "name", "body_part", "equipment", "target", "muscle_group",
            "secondary_muscles", "load_convention", "steps",
        }
        for item in nine_field
    )
    details["legacy_probe_candidates"] = {
        probe["name"]: len(
            catalog.search(
                probe["name"],
                equipment=probe.get("equipment"),
                body_part=probe.get("body_part"),
                target=probe.get("target"),
                muscle_group=probe.get("muscle_group"),
            )
        )
        for probe in probes
    }
    details["legacy_over_ten"] = sorted(
        name for name, size in details["legacy_probe_candidates"].items() if size > 10
    )

    blob_provenance = legacy_blob_provenance()
    checks["legacy_blob_provenance"] = all(
        value for value in blob_provenance.values() if isinstance(value, bool)
    )
    checks["legacy_blob_commit_is_head"] = blob_provenance["commit_matches_head"]
    details["legacy_blob"] = blob_provenance

    import app.interfaces.http as http

    production_declarations = http.business_tool_declarations()
    install_group("baseline")
    baseline_declarations = http.business_tool_declarations()
    checks["baseline_drops_details_tool"] = not any(
        item.name == "get_exercise_details" for item in baseline_declarations
    )
    # 其余工具面逐位一致：两侧同时剔除被替换的检索工具与基线不存在的详情工具。
    retrieval_surface = {"search_exercises", "get_exercise_details"}
    checks["baseline_keeps_other_tools"] = [
        item.name for item in baseline_declarations if item.name not in retrieval_surface
    ] == [
        item.name for item in production_declarations if item.name not in retrieval_surface
    ]
    checks["baseline_search_declaration_is_legacy"] = bool(
        [item for item in baseline_declarations if item.name == "search_exercises"][0].description
        == LEGACY_SEARCH_DESCRIPTION
    )

    profile_models = {
        scenario: ProfileContent.model_validate(values)
        for scenario, values in PROFILE_FIXTURES.items()
    }
    body_weight_plan = {
        "repeat": False,
        "days": [{
            "kind": "training", "focus": "背",
            "exercises": [{
                "exercise_id": "2327", "name": catalog.get("2327").name, "sets": 3, "reps": 12,
                "duration_seconds": None, "weight_kg": None,
                "load_convention": catalog.get("2327").load_convention, "rest_seconds": 90,
            }],
            "notes": None,
        }],
        "notes": None,
        "suggested_fields": [],
    }
    barbell_plan = json.loads(json.dumps(body_weight_plan))
    barbell_plan["days"][0]["exercises"][0].update(
        {"exercise_id": "0095", "name": catalog.get("0095").name, "load_convention": "barbell_total"}
    )

    # 声明与执行注册表一致性：正式运行在首个模型回合前逐项比对，不一致即运行失败并消耗请求。
    gates = offline_service_gates(body_weight_plan, barbell_plan, profile_models)
    mismatched_surface = [
        name for name in gates["declaration_names"]
        if name not in gates["registry_names"]
        or gates["registry_definitions"][name]["description"]
        != gates["declaration_definitions"][name]["description"]
        or gates["registry_definitions"][name]["parameters"]
        != gates["declaration_definitions"][name]["parameters"]
    ]
    checks["baseline_declarations_match_registry"] = not mismatched_surface
    checks["baseline_registry_excludes_details_tool"] = "get_exercise_details" not in gates["registry_names"]
    checks["plan_validator_accepts_body_weight"] = gates["accepted_days"] == 1
    checks["plan_validator_rejects_saved_restriction"] = gates["rejected_code"] == "invalid_business_payload"
    details["offline_service_gates"] = {
        key: value for key, value in gates.items() if key != "registry_definitions"
    }

    install_group("optimized")
    checks["optimized_restore_is_identity"] = (
        http.business_tool_declarations == http._fit_agent_original_declarations
        and http.bind_business_tools == http._fit_agent_original_bind
        and [item.name for item in http.business_tool_declarations()]
        == [item.name for item in production_declarations]
    )
    details["tool_surface_diff"] = tool_surface_diff()

    generate_conditions = evaluate_search_conditions(
        CatalogSearchArguments(purpose="generation", name="练背"),
        profile_models["generate"],
        catalog,
        catalog_purpose_mappings(),
    )
    checks["generate_profile_resolves_home_body_weight_only"] = (
        generate_conditions.conditions.available_equipment == ("body weight",)
    )
    replace_conditions = evaluate_search_conditions(
        CatalogSearchArguments(
            purpose="replacement", name="练背", unavailable_equipment=["dumbbell"]
        ),
        profile_models["replace"],
        catalog,
        catalog_purpose_mappings(),
    )
    checks["replace_temporary_dumbbell_excluded"] = (
        "dumbbell" not in replace_conditions.conditions.available_equipment
        and "barbell" in replace_conditions.conditions.available_equipment
    )
    checks["generate_profile_barbell_saved"] = (
        profile_models["generate"].unavailable_equipment == ["barbell"]
        and profile_models["generate"].forbidden_exercise_ids == []
    )

    dumbbell_back = [
        exercise for exercise in catalog.all()
        if exercise.equipment == "dumbbell" and exercise.body_part == "back"
    ]
    checks["catalog_has_dumbbell_back_candidates"] = bool(dumbbell_back)
    checks["catalog_verify_fact"] = (
        getattr(catalog.get("0095"), "equipment") == "barbell"
        and catalog.get("0095").name == CATALOG_FACTS["0095"]["name"]
    )
    checks["catalog_replace_fact"] = (
        catalog.get("0406").equipment == "dumbbell" and catalog.get("0406").body_part == "back"
    )

    plan_fixture = PlanContent.model_validate(
        {
            "repeat": False,
            "days": [
                {
                    "kind": "training",
                    "focus": "背",
                    "exercises": [
                        {
                            "exercise_id": "0406",
                            "name": catalog.get("0406").name,
                            "sets": 3,
                            "reps": 12,
                            "duration_seconds": None,
                            "weight_kg": 8,
                            "load_convention": catalog.get("0406").load_convention,
                            "rest_seconds": 90,
                        }
                    ],
                    "notes": None,
                }
            ],
            "notes": None,
            "suggested_fields": [],
        }
    )
    details["plan_fixture_days"] = len(plan_fixture.days)
    checks["plan_fixture_valid"] = bool(plan_fixture.days[0].exercises)

    from app.application.business.retrieval_frozen import frozen_values

    frozen = frozen_values()
    checks["frozen_parameters_unchanged"] = (
        frozen["alpha"] == 0.5 and frozen["bm25_depth"] == 100 and frozen["dense_depth"] == 100
        and frozen["threshold"] == 0.0 and frozen["recall_limit"] == 11
    )
    details["frozen_parameters"] = dict(frozen)

    counters = new_counters()
    install_observation(counters)
    failure = None
    try:
        asyncio.run(
            http_module_stream(None, None, None, None, None, None, None)
        )
    except BaseException as error:
        failure = f"{type(error).__name__}"
    checks["counter_records_attempt_before_failure"] = (
        counters["chat"]["attempts"] == 1 and counters["chat"]["failure"] == 1
        and counters["chat"]["success"] == 0 and failure is not None
    )
    checks["counter_keeps_original_exception"] = failure in {"AttributeError", "TypeError"}
    details["counter_probe_failure"] = failure

    identity = model_identity()
    checks["model_identity_matches_saved"] = all(identity["matches_saved_selection"].values())
    details["model_identity"] = identity

    checks["sample_matrix_complete"] = (
        len(sample_order()) == 18 and len(set(sample_order())) == 18
        and sample_order()[0] == "verify-r1-baseline"
        and sample_order()[1] == "verify-r1-optimized"
    )
    checks["user_inputs_fixed"] = len(USER_INPUTS) == 3 and all(USER_INPUTS.values())
    checks["caps_declared"] = (
        CAPS["chat_attempts"] == 260 and CAPS["embedding_attempts"] == 40 and CAPS["summary_attempts"] == 6
        and CAPS["chat_prompt_tokens"] == 2_000_000 and CAPS["chat_completion_tokens"] == 128_000
        and PER_SAMPLE_CAPS == {"model_requests": 8, "search_calls": 4, "details_calls": 2}
    )
    checks["token_cap_excludes_cache"] = "cache" not in json.dumps(CAPS)
    guard = guard_state()
    checks["audit_guard_blocks_default_trace"] = bool(
        guard["validation_root"] and guard["default_trace_open_blocked"]
    )
    details["guard"] = guard

    checks["isolation_lands_under_batch"] = all(
        not str(value).startswith(str(BACKEND / "data")) for value in landing.values() if isinstance(value, str)
    )
    details["isolation_landing"] = landing
    checks["attachment_root_isolated"] = (
        BATCH.resolve() in (BATCH / landing["attachment_root"]).resolve().parents
    )

    cross = cross_check("selfcheck", counters, landing["trace_database"])
    checks["cross_check_reads_sinks_without_runs"] = (
        cross["cache_observation_records"] == 0
        and cross["trace_model_spans"]["model_spans"] == 0
        and cross["agreement"]["chat_matches_observed_calls"] is False
        and cross["agreement"]["chat_and_summary_match_trace_spans"] is None
    )
    details["cross_check_without_runs"] = cross

    # 前置指令展开自检：生成场景 1 项、替换场景 2 项，逐项为完整字符串且与定义一致。
    generate_prompts = precondition_prompts("generate")
    replace_prompts = precondition_prompts("replace")
    checks["precondition_rounds_are_literal"] = (
        PRECONDITION_ROUNDS == {"generate": 1, "replace": 2}
        and all(isinstance(PRECONDITION_PROMPTS[item], tuple) for item in PRECONDITION_ROUNDS)
    )
    checks["precondition_generate_single_full_prompt"] = (
        generate_prompts == [GENERATE_PRECONDITION_PROMPT]
        and len(generate_prompts) == 1
        and all(isinstance(item, str) and len(item) > 8 for item in generate_prompts)
    )
    checks["precondition_replace_two_full_prompts"] = (
        replace_prompts == list(REPLACE_PRECONDITION_PROMPTS)
        and len(replace_prompts) == 2
        and all(isinstance(item, str) and len(item) > 8 for item in replace_prompts)
    )
    # 裸 str 退化形态（缺尾逗号）必须被拒绝，否则会按字符展开成单字回合。
    original_generate = PRECONDITION_PROMPTS["generate"]
    PRECONDITION_PROMPTS["generate"] = GENERATE_PRECONDITION_PROMPT
    try:
        try:
            precondition_prompts("generate")
            checks["precondition_rejects_bare_str"] = False
        except RuntimeError:
            checks["precondition_rejects_bare_str"] = True
    finally:
        PRECONDITION_PROMPTS["generate"] = original_generate

    # 共享读取函数只消费 pending_proposals 白名单字段：history 无 run_id 时不得失败。
    class _FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def get(self, _url: str) -> _FakeResponse:
            return _FakeResponse(self._payload)

    reference_fields = {
        "business_kind": "plan",
        "proposal_id": "11111111-1111-4111-8111-111111111111",
        "status": "pending",
        "request_entry_id": "22222222-2222-4222-8222-222222222222",
        "source_entry_id": "33333333-3333-4333-8333-333333333333",
        "display_entry_id": "44444444-4444-4444-8444-444444444444",
        "confirmation_entry_id": None,
    }
    try:
        read_back = read_pending(
            _FakeClient({"pending_proposals": [reference_fields]}), "session", "plan"
        )
        checks["read_pending_whitelist_only"] = (
            set(read_back) == {
                "proposal_id", "status", "request_entry_id", "source_entry_id",
                "display_entry_id", "confirmation_entry_id", "pending_from_history",
            }
            and read_back["display_entry_id"] == reference_fields["display_entry_id"]
            and "run_id" not in read_back
        )
    except BaseException:
        checks["read_pending_whitelist_only"] = False
    details["precondition_prompts"] = {"generate": generate_prompts, "replace": replace_prompts}

    report = {
        "mode": "selfcheck",
        "started_at": now_iso(),
        "checks": checks,
        "failed": sorted(name for name, passed in checks.items() if not passed),
        "details": details,
        "catalog_size": len(catalog),
    }
    report["finished_at"] = now_iso()
    write_json(SELFCHECK / "selfcheck.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failed"]:
        raise SystemExit(1)
    return report


def catalog_purpose_mappings() -> dict:
    from app.application.business.retrieval_text import load_mappings

    return load_mappings()


def http_module_stream(*args, **kwargs):
    import app.agent.agent_loop as agent_loop

    return agent_loop.stream_assistant_response(*args, **kwargs)


def build_manifest() -> dict:
    identity = model_identity()
    manifest = {
        "version": "t8-business-batch-r1",
        "prepared_at": now_iso(),
        "authorization": {
            "granted_statement": "最后一次直接把真实业务也测试了吧，我允许用真实模型。",
            "granted_at": "2026-10-15",
            "caps": CAPS,
            "per_sample_caps": PER_SAMPLE_CAPS,
            "amount": "unspecified",
            "cost": "unknown",
        },
        "frozen_reference": {
            "holdout_requalification_run": "20261015T081832Z-9a806e7e",
            "parameters": {"alpha": 0.5, "bm25_depth": 100, "dense_depth": 100, "threshold": 0.0,
                           "recall_limit": 11},
        },
        "model_identity": identity,
        "samples": [
            {
                "sample_id": sample_id,
                "position": index,
                "scenario": scenario_of(sample_id),
                "group": sample_id.rsplit("-", 1)[1],
                "user_input": USER_INPUTS[scenario_of(sample_id)],
                "precondition_prompts": (
                    precondition_prompts(scenario_of(sample_id))
                    if scenario_of(sample_id) in PRECONDITION_PROMPTS else []
                ),
                "caps": PER_SAMPLE_CAPS,
                "command": (
                    "uv run --no-sync --frozen --env-file .env python -m "
                    f"test.check_retrieval_business_e2e sample --id {sample_id}"
                ),
            }
            for index, sample_id in enumerate(sample_order())
        ],
        "auxiliary": [
            {"id": "probe", "purpose": "三处计数互证与单请求成本标定"},
            {"id": "prebuild-generate", "purpose": "真实模型+真实确认建立练背画像前置"},
            {"id": "prebuild-replace", "purpose": "真实模型+真实确认建立哑铃背部计划前置"},
            {"id": "chain", "purpose": "真实模型长期器械限制保存/解除链与前端组件层刷新"},
            {"id": "restart-check", "purpose": "新进程同隔离库复读限制/计划/确认状态"},
            {"id": "faults", "purpose": "过期/重复/版本冲突/错误恢复确认矩阵"},
        ],
        "source_snapshot": "manifest/source-before.json",
    }
    return manifest


def plan_mode() -> dict:
    MANIFEST.mkdir(parents=True, exist_ok=True)
    plan_path = MANIFEST / "plan.json"
    source_path = MANIFEST / "source-before.json"
    if plan_path.exists() or source_path.exists():
        raise RuntimeError("预登记清单已存在，禁止重复生成或改写")
    write_json(source_path, source_snapshot())
    manifest = build_manifest()
    write_json(plan_path, manifest)
    print(json.dumps({"samples": len(manifest["samples"]), "order": sample_order()},
                     ensure_ascii=False))
    return manifest


PREPARE_TOOLS = {
    "prepare_profile_update": "profile",
    "prepare_plan": "plan",
    "prepare_plan_adjustment": "plan",
    "prepare_plan_import": "plan",
}


def parse_sample_id(sample_id: str) -> tuple[str, int, str]:
    scenario, repeat, group = sample_id.rsplit("-", 2)
    if scenario not in SCENARIOS or group not in GROUPS or not repeat.startswith("r"):
        raise RuntimeError(f"非法样本标识: {sample_id}")
    return scenario, int(repeat[1:]), group


def prepare_environment(
    namespace: str,
    database_path: Path | None = None,
    group: str | None = None,
    counters: dict | None = None,
) -> dict:
    from test import regression_support

    landing = install_isolation(namespace, database_path)
    regression_support.install_test_model_config(root=BATCH / landing["model_config_root"])
    # 文件工具的会话工作目录默认值在导入时绑定，必须在服务启动前改指到隔离目录。
    import app.agent.tools.files as file_tools
    import app.interfaces.http as http

    original_file_tools = http.create_file_tools
    workspace_root = BATCH / landing["attachment_root"]
    http.create_file_tools = lambda session_id: original_file_tools(
        session_id, tmp_root=workspace_root
    )
    file_tools.TMP_ROOT = workspace_root
    if counters is not None:
        install_observation(counters)
    surface = install_group(group) if group is not None else None
    return {"landing": landing, "surface": surface,
            "file_tools_patched": http.create_file_tools is not original_file_tools}


def open_client(server) -> object:
    from test import regression_support

    return regression_support.client(server.base_url, timeout=240)


def read_state(client, session_id: str | None = None) -> dict:
    state = {
        "profile": client.get("/api/profile").json(),
        "plans": client.get("/api/plans").json(),
        "current_plan": client.get("/api/plans/current").json(),
    }
    state["plan_count"] = len(state["plans"])
    if session_id:
        history = client.get(f"/api/sessions/{session_id}/history").json()
        state["session"] = {
            "confirmations": len(history["confirmations"]),
            "pending_proposals": history["pending_proposals"],
            "runs": len(history["runs"]),
        }
    return state


def state_digest(state: dict) -> str:
    payload = {
        "profile": state["profile"],
        "plan_records": [
            {"is_current": record["is_current"], "content": record["content"]}
            for record in sorted(state["plans"], key=lambda item: item["id"])
        ],
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


def run_turn(client, session_id: str, prompt: str) -> dict:
    from test.check_http import events, final_text, validate_events, wait_idle

    operation_id = str(uuid4())
    started = time.time_ns() // 1_000_000
    with client.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session_id, "operation_id": operation_id, "request": prompt},
    ) as response:
        assert response.status_code == 200, response.text
        run_id = response.headers["X-Run-ID"]
        stream_events = list(events(response))
    terminal = stream_events[-1]["event"]
    turn = {
        "prompt": prompt,
        "operation_id": operation_id,
        "run_id": run_id,
        "started_at_ms": started,
        "elapsed_ms": time.time_ns() // 1_000_000 - started,
        "terminal_event": terminal,
        "event_count": len(stream_events),
        "tools": [],
    }
    if terminal == "error":
        turn["error"] = stream_events[-1]["data"]
        wait_idle()
        return turn
    starts, results = validate_events(stream_events)
    finals = {
        event["data"]["tool_call_id"]: event["data"]
        for event in stream_events
        if event["event"] == "tool_execution_end"
    }
    displayed = {
        event["data"]["tool_call_id"]: event["data"]
        for event in stream_events
        if event["event"] == "tool_result"
    }
    for call_id, start in starts.items():
        turn["tools"].append(
            {
                "tool_call_id": call_id,
                "name": start["name"],
                "arguments": start["arguments"],
                "result_text": results[call_id],
                "result_chars": len(results[call_id]),
                "is_error": displayed[call_id]["is_error"],
                "display_entry_id": displayed[call_id]["entry_id"],
                "execution_chars": len(finals[call_id]["content"]),
            }
        )
    turn["stop_reason"] = terminal_data_stop_reason(stream_events)
    turn["final_text"] = final_text(stream_events)
    turn["final_text_chars"] = len(turn["final_text"])
    wait_idle()
    return turn


def terminal_data_stop_reason(stream_events: list) -> str:
    last_message = next(
        event for event in reversed(stream_events) if event["event"] == "message_end"
    )
    return last_message["data"]["stop_reason"]


def find_proposal(turn: dict) -> dict | None:
    for tool in turn["tools"]:
        kind = PREPARE_TOOLS.get(tool["name"])
        if kind is None or tool["is_error"]:
            continue
        payload = json.loads(tool["result_text"])
        return {
            "tool": tool["name"],
            "business_kind": kind,
            "proposal": payload,
            "display_entry_id": tool["display_entry_id"],
        }
    return None


def post_confirmation(
    client, session_id: str, business_kind: str, proposal_id: str, display_entry_id: str,
    operation_id: str | None = None,
) -> dict:
    payload = {
        "operation_id": operation_id or str(uuid4()),
        "business_kind": business_kind,
        "proposal_id": proposal_id,
        "display_entry_id": display_entry_id,
    }
    response = client.post(f"/api/sessions/{session_id}/confirmations", json=payload)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    return {"request": payload, "status_code": response.status_code, "body": body}


def exercise_facts(catalog, exercise_id: str) -> dict | None:
    exercise = catalog.get(exercise_id)
    if exercise is None:
        return None
    return {
        "id": exercise.id, "name": exercise.name, "equipment": exercise.equipment,
        "body_part": exercise.body_part, "target": exercise.target,
        "muscle_group": exercise.muscle_group,
    }


def plan_exercise_ids(content: dict) -> list[tuple[int, int, str]]:
    return [
        (day_index, index, exercise["exercise_id"])
        for day_index, day in enumerate(content["days"])
        for index, exercise in enumerate(day["exercises"])
        if exercise.get("exercise_id")
    ]


def count_check(counters: dict, turn: dict) -> list[dict]:
    searches = [tool for tool in turn["tools"] if tool["name"] == "search_exercises"]
    details = [tool for tool in turn["tools"] if tool["name"] == "get_exercise_details"]
    return [
        {
            "name": "per_sample_model_requests",
            "passed": counters["chat"]["attempts"] <= PER_SAMPLE_CAPS["model_requests"],
            "detail": {"attempts": counters["chat"]["attempts"]},
        },
        {
            "name": "per_sample_search_calls",
            "passed": len(searches) <= PER_SAMPLE_CAPS["search_calls"],
            "detail": {"calls": len(searches)},
        },
        {
            "name": "per_sample_details_calls",
            "passed": len(details) <= PER_SAMPLE_CAPS["details_calls"],
            "detail": {"calls": len(details)},
        },
    ]


def volume_records(turn: dict) -> dict:
    by_tool: dict[str, dict] = {}
    for tool in turn["tools"]:
        entry = by_tool.setdefault(tool["name"], {"calls": 0, "result_chars": 0, "max_result_chars": 0})
        entry["calls"] += 1
        entry["result_chars"] += tool["result_chars"]
        entry["max_result_chars"] = max(entry["max_result_chars"], tool["result_chars"])
    return by_tool


def budget_check(turn: dict, group: str) -> list[dict]:
    searches = [tool for tool in turn["tools"] if tool["name"] == "search_exercises"]
    details = [tool for tool in turn["tools"] if tool["name"] == "get_exercise_details"]
    if group == "baseline":
        # 旧口径按九字段完整数组返回且不截断：登记原始体积，并以旧契约自身判据核验。
        payloads = [json.loads(tool["result_text"]) for tool in searches]
        return [
            {
                "name": "legacy_search_returns_nine_field_array",
                "passed": all(isinstance(payload, list)
                              and all(len(item) == 9 for item in payload) for payload in payloads),
                "detail": {"chars": [tool["result_chars"] for tool in searches],
                           "item_counts": [len(payload) for payload in payloads if isinstance(payload, list)],
                           "legacy_caliber": "旧名称子串口径无字符预算，返回体积单列登记"},
            },
            {
                "name": "legacy_details_tool_absent",
                "passed": not details,
                "detail": {"details_calls": len(details)},
            },
        ]
    return [
        {
            "name": "search_budget_within_4000",
            "passed": all(tool["result_chars"] <= 4000 for tool in searches),
            "detail": {"chars": [tool["result_chars"] for tool in searches]},
        },
        {
            "name": "details_budget_within_7000",
            "passed": all(tool["result_chars"] <= 7000 for tool in details),
            "detail": {"chars": [tool["result_chars"] for tool in details]},
        },
    ]


def scenario_assertions(
    scenario: str, group: str, turn: dict, before: dict, after: dict,
    confirmed: dict | None, proposal: dict | None, catalog, counters: dict,
) -> list[dict]:
    checks: list[dict] = [
        {"name": "run_completed", "passed": turn["terminal_event"] == "done",
         "detail": turn.get("error") or {"stop_reason": turn.get("stop_reason")}},
    ]
    tools = turn["tools"]
    searches = [tool for tool in tools if tool["name"] == "search_exercises"]
    details = [tool for tool in tools if tool["name"] == "get_exercise_details"]
    checks.append({
        "name": "retrieval_search_called", "passed": bool(searches),
        "detail": {"arguments": [tool["arguments"] for tool in searches]},
    })
    checks.extend(budget_check(turn, group))

    if scenario == "verify":
        surfaced = None
        for tool in details:
            payload = json.loads(tool["result_text"])
            item = next((entry for entry in payload["items"] if entry["id"] == "0095"), None)
            if item is not None:
                surfaced = item
        if surfaced is None:
            for tool in searches:
                payload = json.loads(tool["result_text"])
                items = payload if isinstance(payload, list) else payload.get("items", [])
                if any(entry["id"] == "0095" for entry in items):
                    surfaced = next(entry for entry in items if entry["id"] == "0095")
        checks.append({
            "name": "catalog_record_surfaced_to_model", "passed": surfaced is not None,
            "detail": {"surfaced_keys": sorted(surfaced) if surfaced else None},
        })
        checks.append({
            "name": "verify_equipment_fact",
            "passed": bool(surfaced) and surfaced.get("equipment") == "barbell",
            "detail": {"catalog_fact": exercise_facts(catalog, "0095"), "surfaced": surfaced},
        })
        steps = (surfaced or {}).get("steps")
        checks.append({
            "name": "verify_steps_from_catalog",
            "passed": bool(steps) and bool(steps.get("zh")),
            "detail": {"step_count": len((steps or {}).get("zh") or [])},
        })
        checks.append({
            "name": "verify_details_called",
            "passed": bool(details) if group == "optimized" else not details,
            "detail": {"group": group, "details_calls": len(details)},
        })
        if group == "optimized":
            checks.append({
                "name": "lookup_purpose_used",
                "passed": any(tool["arguments"].get("purpose") == "lookup" for tool in searches),
                "detail": {"purposes": [tool["arguments"].get("purpose") for tool in searches]},
            })
        checks.append({
            "name": "verify_zero_business_write",
            "passed": state_digest(before) == state_digest(after)
            and after["profile"]["version"] is None
            and after["plan_count"] == 0
            and after["session"]["confirmations"] == 0,
            "detail": {"before": state_digest(before), "after": state_digest(after),
                       "session": after.get("session")},
        })
    elif scenario == "generate":
        checks.append({
            "name": "prepare_plan_proposed", "passed": bool(proposal)
            and proposal["business_kind"] == "plan" and proposal["tool"] == "prepare_plan",
            "detail": {"proposal_id": (proposal or {}).get("proposal", {}).get("proposal_id")},
        })
        payload = ((proposal or {}).get("proposal") or {}).get("payload") or {}
        pairs = plan_exercise_ids(payload) if payload else []
        facts = {exercise_id: exercise_facts(catalog, exercise_id) for _, _, exercise_id in pairs}
        checks.append({
            "name": "plan_exercises_exist_in_catalog", "passed": bool(pairs) and all(facts.values()),
            "detail": {"ids": sorted(facts)},
        })
        equipments = sorted({fact["equipment"] for fact in facts.values() if fact})
        checks.append({
            "name": "plan_respects_saved_profile",
            "passed": all(fact is not None and fact["equipment"] not in (after["profile"]["content"]["unavailable_equipment"] or [])
                          and fact["id"] not in (after["profile"]["content"]["forbidden_exercise_ids"] or [])
                          for fact in facts.values()),
            "detail": {"equipments": equipments,
                       "saved_unavailable": after["profile"]["content"]["unavailable_equipment"]},
        })
        if group == "optimized":
            checks.append({
                "name": "plan_home_default_no_extra_equipment",
                "passed": set(equipments) <= {"body weight"},
                "detail": {"equipments": equipments,
                          "search_arguments": [tool["arguments"] for tool in searches]},
            })
        checks.append(confirmed_check(confirmed, after, before))
    else:
        before_ids = plan_exercise_ids(before["current_plan"]["content"])
        dumbbell_back = [
            (day, index, exercise_id)
            for day, index, exercise_id in before_ids
            if (exercise_facts(catalog, exercise_id) or {}).get("equipment") == "dumbbell"
            and (exercise_facts(catalog, exercise_id) or {}).get("body_part") == "back"
        ]
        checks.append({
            "name": "precondition_saved_plan_has_dumbbell_back",
            "passed": bool(dumbbell_back),
            "detail": {"positions": dumbbell_back},
        })
        checks.append({
            "name": "prepare_plan_adjustment_proposed",
            "passed": bool(proposal) and proposal["tool"] == "prepare_plan_adjustment",
            "detail": {"tool": (proposal or {}).get("tool")},
        })
        payload = ((proposal or {}).get("proposal") or {}).get("payload") or {}
        after_ids = plan_exercise_ids(payload)
        replaced = []
        for day, index, exercise_id in dumbbell_back:
            match = next(((d, i, e) for d, i, e in after_ids if (d, i) == (day, index)), None)
            facts = exercise_facts(catalog, match[2]) if match else None
            replaced.append({
                "position": [day, index], "original": exercise_id,
                "replacement": match[2] if match else None, "fact": facts,
            })
        checks.append({
            "name": "same_position_replaced_by_other_catalog_exercise",
            "passed": bool(replaced) and all(
                item["replacement"] and item["replacement"] != item["original"] and item["fact"]
                for item in replaced
            ),
            "detail": {"replacements": replaced},
        })
        checks.append({
            "name": "replacement_excludes_dumbbell",
            "passed": all(item["fact"] and item["fact"]["equipment"] != "dumbbell" for item in replaced),
            "detail": {"equipments": [item["fact"]["equipment"] if item["fact"] else None for item in replaced]},
        })
        if group == "optimized":
            checks.append({
                "name": "temporary_unavailable_equipment_declared",
                "passed": any(
                    tool["arguments"].get("purpose") == "replacement"
                    and "dumbbell" in (tool["arguments"].get("unavailable_equipment") or [])
                    for tool in searches
                ),
                "detail": {"arguments": [tool["arguments"] for tool in searches]},
            })
        else:
            checks.append({
                "name": "legacy_replacement_has_no_task_condition_channel",
                "passed": True,
                "detail": {"not_applicable": "旧口径工具面无 unavailable_equipment 参数"},
            })
        checks.append({
            "name": "long_term_profile_unchanged",
            "passed": before["profile"] == after["profile"],
            "detail": {"before": before["profile"], "after": after["profile"]},
        })
        checks.append(confirmed_check(confirmed, after, before))
        checks.append({
            "name": "previous_plan_history_preserved",
            "passed": all(record["id"] in {item["id"] for item in after["plans"]}
                          for record in before["plans"]),
            "detail": {"before_ids": len(before["plans"]), "after_ids": len(after["plans"])},
        })
    checks.extend(count_check(counters, turn))
    return checks


def confirmed_check(confirmed: dict | None, after: dict, before: dict) -> dict:
    accepted = bool(confirmed) and confirmed["post_status_code"] in {200, 202}
    saved = accepted and confirmed["terminal"]["final_status"] == "saved"
    plan_delta = after["plan_count"] - before["plan_count"]
    return {
        "name": "real_confirmation_saved_and_plan_appended",
        "passed": saved and plan_delta == 1,
        "detail": {
            "post_status_code": (confirmed or {}).get("post_status_code"),
            "terminal_status": (confirmed or {}).get("terminal", {}).get("final_status"),
            "polls": (confirmed or {}).get("terminal", {}).get("polls"),
            "body_status": ((confirmed or {}).get("body") or {}).get("status"),
            "plan_delta": plan_delta,
        },
    }


def backup_database(source: Path, target: Path) -> dict:
    # SQLite 标准备份：逐份独立文件，源与副本内容一致可由哈希与完整性检查证明。
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    destination = sqlite3.connect(target)
    try:
        origin.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        tables = destination.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        rows = {
            name: destination.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for (name,) in tables
        }
    finally:
        destination.close()
        origin.close()
    return {
        "source": source.relative_to(BATCH).as_posix(),
        "target": target.relative_to(BATCH).as_posix(),
        "source_sha256": sha256_file(source),
        "target_sha256": sha256_file(target),
        "integrity_check": integrity,
        "table_rows": rows,
    }


def snapshot_protected() -> dict:
    paths = [
        REPO / "tmp/action-retrieval-t8/frozen-parameters.json",
        REPO / "tmp/action-retrieval-t8/holdout-executions.json",
        REPO / "tmp/action-retrieval-t8-requalification/authorization.json",
        REPO / "tmp/action-retrieval-t8-requalification/executions.json",
        BACKEND / "dataset/action-retrieval-queries.json",
        BACKEND / "dataset/action-retrieval-purpose-adaptation.json",
        BACKEND / "dataset/actions.agent.json",
    ]
    return {path.relative_to(REPO).as_posix(): sha256_file(path) for path in paths}


PROTECTED_BASELINE = {
    "tmp/action-retrieval-t8/frozen-parameters.json": "8ce5b16b8ab4fae70bb1ee0c90f6910c73894ff1d8c152c4b5bf26fff8080285",
    "tmp/action-retrieval-t8/holdout-executions.json": "a7040d9749b60ee24664879a5baf8bb9f4197d9d1d6153880d0ed0359bed7ac9",
    "tmp/action-retrieval-t8-requalification/authorization.json": "5dc9a6a4d1d5e6b91d5ab1c23ad3b6ff511bd27cfb911e5fdd25719a98da578f",
}


def require_protected_originals() -> dict:
    current = snapshot_protected()
    drift = {
        name: {"expected": expected, "current": current[name]}
        for name, expected in PROTECTED_BASELINE.items()
        if current.get(name) != expected
    }
    if drift:
        raise RuntimeError(f"受保护原件漂移: {json.dumps(drift, ensure_ascii=False)}")
    return current


def session_setup(client) -> str:
    from test.check_http import create_session

    session_id = str(uuid4())
    create_session(client, session_id, "T8 真实业务验收")
    return session_id


def finalize(namespace: str, evidence: dict, directory: Path, failures: list[str]) -> None:
    evidence["finished_at"] = now_iso()
    write_json(directory / f"{namespace}.json", evidence)
    print(json.dumps({"namespace": namespace, "failed": failures}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


def counters_view(counters: dict) -> dict:
    return {kind: dict(entry) for kind, entry in counters.items()}


def cross_check(namespace: str, counters: dict, trace_database: str) -> dict:
    observations = read_cache_observations(namespace)
    trace = read_trace_models(BATCH / trace_database)
    chat_attempts = counters["chat"]["attempts"]
    summary_attempts = counters["summary"]["attempts"]
    categories = observations["summary"]["totals"]["categories"]
    observed_calls = sum(categories.get(key, {}).get("calls", 0) for key in ("first", "append", "summary", "reset"))
    # 隔离观测库无 Span 写入时第三路计数不可比：按未验证标注，不判为业务失败。
    spans_comparable = trace["trace_database_present"] and trace["model_spans"] > 0
    return {
        "wrap_counts": {"chat": chat_attempts, "summary": summary_attempts,
                        "embedding": counters["embedding"]["attempts"]},
        "cache_observation_records": observations["records"],
        "cache_observation_summary": observations["summary"],
        "trace_model_spans": trace,
        "agreement": {
            "chat_matches_observed_calls": chat_attempts == observed_calls,
            "observed_calls_total": observed_calls,
            "trace_spans_comparable": spans_comparable,
            "chat_and_summary_match_trace_spans": (
                chat_attempts + summary_attempts == trace["model_spans"] if spans_comparable else None
            ),
        },
    }


def run_mode(namespace: str, scenario: str, group: str, repeat: int) -> None:
    from app.application.business.catalog import Catalog
    from test import regression_support

    require_source_identity(namespace)
    protected = require_protected_originals()
    sample_id = f"{scenario}-r{repeat}-{group}"
    directory = SAMPLES
    database = DATABASES / f"{sample_id}.db"
    counters = new_counters()
    source = PRECONDITIONS / f"{scenario}.db"
    backups = None
    if scenario == "verify":
        database.unlink(missing_ok=True)
    else:
        if not source.is_file():
            raise RuntimeError(f"缺少前置源库: {source}")
        if database.exists():
            raise RuntimeError(f"样本库已存在，禁止复用: {database}")
        backups = backup_database(source, database)
    landing = prepare_environment(sample_id, database, group, counters)
    from app.interfaces.http import app  # 隔离补丁之后导入
    trace_database = landing["landing"]["trace_database"]
    catalog = Catalog.load()
    evidence: dict = {
        "sample_id": sample_id, "scenario": scenario, "group": group, "repeat": repeat,
        "user_input": USER_INPUTS[scenario], "started_at": now_iso(),
        "isolation": landing["landing"], "tool_surface": landing["surface"],
        "model_identity": model_identity(), "database_backup": backups,
        "protected_originals": protected, "frozen_reference_run": "20261015T081832Z-9a806e7e",
        "catalog_size": len(catalog),
    }
    failures: list[str] = []
    try:
        with regression_support.Server(app) as server:
            client = open_client(server)
            session_id = session_setup(client)
            before = read_state(client, session_id)
            turn = run_turn(client, session_id, USER_INPUTS[scenario])
            proposal = find_proposal(turn)
            mid = read_state(client, session_id)
            confirmed = None
            if proposal is not None:
                confirmed = confirm_proposal(client, session_id, proposal, counters)
            after = read_state(client, session_id)
            assertions = scenario_assertions(
                scenario, group, turn, before, after, confirmed, proposal, catalog, counters
            )
            if confirmed is not None:
                assertions.append({
                    "name": "confirmation_and_reads_call_no_model",
                    "passed": all(value == 0 for value in confirmed["model_request_delta"].values()),
                    "detail": confirmed["model_request_delta"],
                })
            replayed = None
            if scenario in ("generate", "replace") and confirmed \
                    and confirmed["terminal"]["final_status"] == "saved":
                replayed = post_confirmation(
                    client, session_id, proposal["business_kind"],
                    confirmed["request"]["proposal_id"], confirmed["request"]["display_entry_id"],
                    operation_id=confirmed["request"]["operation_id"],
                )
                replay_state = read_state(client, session_id)
                replay_terminal = wait_for_confirmation(
                    client, session_id, confirmed["request"]["operation_id"]
                )
                same_node = (
                    (replay_terminal["final"] or {}).get("confirmation_entry_id")
                    == (confirmed["terminal"]["final"] or {}).get("confirmation_entry_id")
                )
                assertions.append({
                    "name": "confirmation_idempotent_replay",
                    "passed": replayed["status_code"] in {200, 202}
                    and replay_terminal["final_status"] == "saved" and same_node,
                    "detail": {"post_status_code": replayed["status_code"],
                               "terminal": replay_terminal, "same_confirmation_node": same_node},
                })
                assertions.append({
                    "name": "replay_adds_no_plan_version",
                    "passed": replay_state["plan_count"] == after["plan_count"],
                    "detail": {"plan_count": replay_state["plan_count"]},
                })
            evidence.update({
                "session_id": session_id, "before_state": before, "mid_state": mid,
                "turn": turn, "proposal": proposal, "confirmation": confirmed,
                "replay": replayed, "after_state": after,
                "state_digest_before": state_digest(before),
                "state_digest_after": state_digest(after),
                "return_volumes": volume_records(turn),
                "assertions": assertions,
                "counters": counters_view(counters),
            })
            failures = [entry["name"] for entry in assertions if not entry["passed"]]
    except BaseException as error:
        evidence["fatal"] = {"type": type(error).__name__, "message": str(error),
                             "traceback": traceback.format_exc(limit=8)}
        failures.append(f"fatal:{type(error).__name__}")
    evidence["cross_check"] = cross_check(sample_id, counters, trace_database)
    evidence["failed"] = sorted(set(failures))
    evidence["first_run"] = True
    ledger_append(sample_id, counters, {"kind": "sample", "scenario": scenario,
                                       "group": group, "repeat": repeat,
                                       "failed": evidence["failed"]})
    finalize(sample_id, evidence, directory, evidence["failed"])


def storage_state(database_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return {
            name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for (name,) in tables
        }
    finally:
        connection.close()


def stage_pending_snapshot(fixture, session_id: str, kind: str, arguments: dict, tool_name: str) -> dict:
    # 故障矩阵需要在无模型的隔离副本上登记待确认快照：走真实准备与绑定方法，助手节点由测试写入并如实标注。
    identifiers = asyncio.run(fixture.stage(session_id, kind, arguments, tool_name))
    pending = [
        item for item in asyncio.run(fixture.business.list_pending_references(session_id))
        if item.proposal_id == identifiers["proposal"]
    ]
    return {
        "staged_by": "service-prepare-plus-test-authored-assistant-node",
        "identifiers": identifiers,
        "confirmation_ids": [
            {"business_kind": item.business_kind, "proposal_id": item.proposal_id,
             "display_entry_id": item.display_entry_id, "status": item.status}
            for item in pending
        ],
    }


def error_code_of(body: dict | None) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("detail", "error"):
        value = body.get(key)
        if isinstance(value, dict) and value.get("code"):
            return value["code"]
    return None


def fault_cases(fixture, client, session_id: str, other_session_id: str, counters: dict) -> list[dict]:
    results: list[dict] = []

    def record(name: str, observed: object, expected: object, extra: dict | None = None) -> None:
        results.append({
            "case": name, "expected": expected, "observed": observed,
            "passed": observed == expected, "detail": extra or {},
        })

    def confirm(name, business_kind, proposal_id, display_entry_id, operation_id=None, target=session_id):
        before = {kind: counters[kind]["attempts"] for kind in counters}
        response = post_confirmation(
            client, target, business_kind, proposal_id, display_entry_id, operation_id
        )
        record(f"{name}:confirmation_calls_no_model",
               {kind: counters[kind]["attempts"] - value for kind, value in before.items()},
               {"chat": 0, "summary": 0, "embedding": 0})
        return response

    identifiers = stage_pending_snapshot(
        fixture, session_id, "profile",
        {"profile_id": 1, "base_profile_version": None, "payload": PROFILE_FIXTURES["replace"]},
        "prepare_profile_update",
    )
    stage_pending_snapshot(
        fixture, other_session_id, "profile",
        {"profile_id": 1, "base_profile_version": None, "payload": PROFILE_FIXTURES["generate"]},
        "prepare_profile_update",
    )
    # 确认入口按待确认条目读取服务端绑定的提案与展示节点 id，与 18 样本同一口径。
    primary = read_pending(client, session_id, "profile")
    foreign = read_pending(client, other_session_id, "profile")
    proposal_id = primary["proposal_id"]
    display_entry_id = primary["display_entry_id"]
    details = {"staged": identifiers["staged_by"], "primary": primary, "foreign": foreign}

    response = confirm("display_binding_mismatch", "profile", proposal_id, str(uuid4()))
    record("display_binding_mismatch", (response["status_code"], error_code_of(response["body"])),
           (409, "profile_confirmation_invalid"), details)

    response = confirm("unknown_proposal_not_found", "profile", str(uuid4()), str(uuid4()))
    record("unknown_proposal_not_found", (response["status_code"], error_code_of(response["body"])),
           (404, "profile_proposal_not_found"))

    response = confirm("cross_session_access", "profile", foreign["proposal_id"],
                       foreign["display_entry_id"], target=session_id)
    record("cross_session_access", (response["status_code"], error_code_of(response["body"])),
           (403, "profile_access_denied"), {"foreign_session": other_session_id})

    shared_operation = str(uuid4())
    first_reject = confirm("duplicate_operation_first", "profile", proposal_id, str(uuid4()),
                           operation_id=shared_operation)
    second_reject = confirm("duplicate_operation_conflict", "profile", proposal_id, display_entry_id,
                            operation_id=shared_operation)
    record("duplicate_operation_same_key_conflict",
           (first_reject["status_code"], error_code_of(first_reject["body"]),
            second_reject["status_code"], error_code_of(second_reject["body"])),
           (409, "profile_confirmation_invalid", 409, "operation_conflict"),
           {"first": first_reject["body"], "second": second_reject["body"]})

    name = "http_invalid_wire_shape"
    response = client.post(
        f"/api/sessions/{session_id}/confirmations",
        json={"operation_id": str(uuid4()), "business_kind": "profile",
              "proposal_id": proposal_id, "display_entry_id": display_entry_id, "extra": 1},
    )
    record(name, response.status_code, 422)
    return results


async def expect_service_error(fixture, arguments: dict, tool_name: str) -> str | None:
    from app.domain.business.errors import BusinessError
    from app.domain.business.models import (
        PlanProposalArguments,
        ProfileProposalArguments,
    )

    if tool_name == "prepare_profile_update":
        model = ProfileProposalArguments
        method = fixture.business.prepare_profile_update
    else:
        model = PlanProposalArguments
        method = fixture.business.prepare_plan
    session_id = await fixture.session("故障矩阵会话")
    run = await fixture.turn(session_id, "请整理待确认内容")
    context = fixture.context(session_id, run.request_entry_id, run.request_entry_id)
    try:
        await method(context, model.model_validate(arguments))
    except BusinessError as error:
        return error.code
    return None


def faults_mode() -> None:
    from test import regression_support
    from test.check_structured_confirmation import Fixture

    require_source_identity("faults")
    require_protected_originals()
    counters = new_counters()
    database = DATABASES / "faults.db"
    if database.exists():
        raise RuntimeError(f"故障矩阵库已存在，禁止复用: {database}")
    landing = prepare_environment("faults", database, None, counters)
    from app.interfaces.http import app  # 隔离补丁之后导入
    trace_database = landing["landing"]["trace_database"]

    async def build_fixture() -> Fixture:
        fixture = Fixture(database)
        await fixture.open()
        async with fixture.repository.transaction():
            from app.application.business.catalog import Catalog

            await fixture.repository.replace_exercises(Catalog.load().all())
        return fixture

    fixture = asyncio.run(build_fixture())
    session_id = asyncio.run(fixture.session("故障矩阵主会话"))
    other_session_id = asyncio.run(fixture.session("故障矩阵越权会话"))
    results: list[dict] = []
    failures: list[str] = []
    state_before = None
    state_after = None
    try:
        asyncio.run(fixture.close())
        with regression_support.Server(app) as server:
            client = open_client(server)
            state_before = read_state(client, session_id)
            fixture = asyncio.run(build_fixture())
            try:
                results = fault_cases(fixture, client, session_id, other_session_id, counters)
                for name, arguments, tool in (
                    ("stale_base_profile_version", {
                        "profile_id": 1, "base_profile_version": 99,
                        "payload": PROFILE_FIXTURES["generate"]}, "prepare_profile_update"),
                    ("unknown_equipment_rejected", {
                        "profile_id": 1, "base_profile_version": None,
                        "payload": dict(PROFILE_FIXTURES["generate"],
                                        unavailable_equipment=["not-a-tool"])}, "prepare_profile_update"),
                    ("plan_stale_profile_version", {
                        "base_profile_version": 99, "base_plan_id": None,
                        "payload": {"repeat": False, "days": [], "notes": None,
                                    "suggested_fields": []}}, "prepare_plan"),
                ):
                    code = asyncio.run(expect_service_error(fixture, arguments, tool))
                    results.append({
                        "case": name, "observed": code,
                        "expected": SERVICE_EXPECTED[name],
                        "passed": code == SERVICE_EXPECTED[name],
                        "detail": {"channel": "真实业务服务授权校验，不经模型"},
                    })
            finally:
                asyncio.run(fixture.close())
            state_after = read_state(client, session_id)
    except BaseException as error:
        failures.append("fatal")
        results.append({"case": "fatal", "passed": False,
                        "detail": {"type": type(error).__name__, "traceback": traceback.format_exc(limit=8)}})
    no_error_commit = (
        state_before is not None and state_after is not None
        and state_digest(state_before) == state_digest(state_after)
        and state_after["profile"]["version"] is None
        and state_after["plan_count"] == 0
        and all(case.get("body_status") not in {"saved"} for case in results)
    )
    results.append({
        "case": "no_error_commit_on_any_failure", "passed": no_error_commit,
        "expected": "画像与计划保持空", "observed": {
            "profile": (state_after or {}).get("profile"),
            "plan_count": (state_after or {}).get("plan_count"),
            "digest_equal": state_before is not None and state_after is not None
            and state_digest(state_before) == state_digest(state_after),
        },
    })
    evidence = {
        "mode": "faults", "started_at": now_iso(), "cases": results,
        "state_before": state_before, "state_after": state_after,
        "counters": counters_view(counters),
        "storage_after": storage_state(database),
        "cross_check": cross_check("faults", counters, trace_database),
        "staging_note": "待确认快照由真实业务准备与绑定方法登记，助手节点由测试写入；模型请求计数必须保持 0。",
    }
    failures = [case["case"] for case in results if not case["passed"]]
    evidence["failed"] = sorted(set(failures))
    ledger_append("faults", counters, {"kind": "faults", "failed": evidence["failed"]})
    finalize("faults", evidence, FAULTS, evidence["failed"])


SERVICE_EXPECTED = {
    "stale_base_profile_version": "profile_version_conflict",
    "unknown_equipment_rejected": "invalid_business_payload",
    "plan_stale_profile_version": "profile_version_conflict",
}


def chain_reference_path() -> Path:
    return CHAIN / "chain-reference.json"


def restart_mode() -> None:
    # 第二次进程：全新 Database 与全新服务，指向同一隔离库，只做读取。
    from test import regression_support


    require_source_identity("restart-check")
    require_protected_originals()
    reference = read_json(chain_reference_path())
    counters = new_counters()
    database = DATABASES / "profile-chain.db"
    landing = prepare_environment("restart-check", database, None, counters)
    from app.interfaces.http import app  # 隔离补丁之后导入
    trace_database = landing["landing"]["trace_database"]
    failures: list[str] = []
    evidence = {
        "mode": "restart-check", "started_at": now_iso(), "pid": os.getpid(),
        "reference": reference, "isolation": landing["landing"],
        "storage_identical": None, "reads": None,
    }
    try:
        evidence["storage_identical"] = storage_state(database) == reference["storage_before_restart"]
        with regression_support.Server(app) as server:
            client = open_client(server)
            reads = {
                "profile": client.get("/api/profile").json(),
                "plans_count": len(client.get("/api/plans").json()),
                "history": {
                    key: len(client.get(f"/api/sessions/{reference['session_id']}/history").json()[key])
                    for key in ("entries", "runs", "confirmations", "pending_proposals")
                },
                "confirmation_replay_after_restart": post_confirmation(
                    client, reference["session_id"], "profile",
                    reference["final_proposal_id"], reference["final_display_entry_id"],
                    operation_id=reference["final_operation_id"],
                ),
            }
        evidence["reads"] = reads
        evidence["counters"] = counters_view(counters)
        body = reads["confirmation_replay_after_restart"]["body"] or {}
        failures = [
            name for name, passed in [
                ("storage_identical", evidence["storage_identical"] is True),
                ("profile_current_matches", reads["profile"]["version"] == reference["final_version"]
                 and reads["profile"]["content"] == reference["final_content"]),
                ("plan_count_preserved", reads["plans_count"] == reference["plan_count"]),
                ("history_preserved", reads["history"]["runs"] >= reference["history_runs"]),
                ("replay_after_restart_same_result",
                 reads["confirmation_replay_after_restart"]["status_code"] == 200
                 and body.get("status") == "saved"
                 and body.get("confirmation_entry_id") == reference["final_confirmation_entry_id"]),
                ("restart_reads_call_no_model",
                 all(entry["attempts"] == 0 for entry in counters.values())),
            ] if not passed
        ]
    except BaseException as error:
        evidence["fatal"] = {"type": type(error).__name__,
                             "traceback": traceback.format_exc(limit=8)}
        failures.append("fatal")
    evidence["cross_check"] = cross_check("restart-check", counters, trace_database)
    evidence["failed"] = sorted(set(failures))
    ledger_append("restart-check", counters, {"kind": "restart", "failed": evidence["failed"]})
    finalize("restart-check", evidence, RESTART, evidence["failed"])


def chain_mode() -> None:
    from test import regression_support

    require_source_identity("chain")
    require_protected_originals()
    database = DATABASES / "profile-chain.db"
    if database.exists():
        raise RuntimeError(f"限制链库已存在，禁止复用: {database}")
    counters = new_counters()
    landing = prepare_environment("chain", database, "optimized", counters)
    from app.interfaces.http import app  # 隔离补丁之后导入

    trace_database = landing["landing"]["trace_database"]
    turns: list[dict] = []
    checks: list[dict] = []
    evidence = {
        "mode": "chain", "started_at": now_iso(), "pid": os.getpid(),
        "isolation": landing["landing"], "model_identity": model_identity(),
        "tool_surface": landing["surface"], "frozen_reference_run": "20261015T081832Z-9a806e7e",
        "protected_originals": snapshot_protected(), "file_tools_patched": landing["file_tools_patched"],
    }
    failures: list[str] = []
    try:
        with regression_support.Server(app) as server:
            client = open_client(server)
            session_id = session_setup(client)
            before = read_state(client, session_id)
            evidence["session_id"] = session_id
            evidence["initial_profile"] = before["profile"]

            first_prompt = "以后都没有哑铃，请更新不可用器械。"
            turn = run_turn(client, session_id, first_prompt)
            proposal = find_proposal(turn)
            prepared_state = read_state(client, session_id)
            checks.append({
                "name": "model_prepared_profile_proposal",
                "passed": bool(proposal) and proposal["business_kind"] == "profile",
                "detail": {"tool": (proposal or {}).get("tool"),
                           "payload": (proposal or {}).get("proposal", {}).get("payload")},
            })
            checks.append({
                "name": "prepare_submits_no_business_write",
                "passed": state_digest(before) == state_digest(prepared_state),
                "detail": {"before": state_digest(before), "prepared": state_digest(prepared_state)},
            })
            turns.append({"prompt": first_prompt, "turn": turn, "proposal": proposal})

            text_confirm_prompt = "确认保存"
            text_turn = run_turn(client, session_id, text_confirm_prompt)
            text_state = read_state(client, session_id)
            checks.append({
                "name": "plain_text_confirmation_grants_no_save",
                "passed": state_digest(prepared_state) == state_digest(text_state)
                and text_state["session"]["confirmations"] == 0,
                "detail": {"state": text_state["session"], "turn_terminal": text_turn["terminal_event"]},
            })
            turns.append({"prompt": text_confirm_prompt, "turn": text_turn})

            save_chain_confirmation(client, session_id, proposal, prepared_state, checks, turns,
                                    "restrict", counters)
            restricted = read_state(client, session_id)
            checks.append({
                "name": "restriction_saved_current_value",
                "passed": "dumbbell" in (restricted["profile"]["content"]["unavailable_equipment"] or []),
                "detail": {"profile": restricted["profile"]},
            })
            history = client.get(f"/api/sessions/{session_id}/history").json()
            checks.append({
                "name": "history_snapshots_independent",
                "passed": len(history["confirmations"]) == 2
                and all(item["business_kind"] == "profile" for item in history["confirmations"]),
                "detail": {"confirmations": history["confirmations"]},
            })

            second_prompt = "哑铃已恢复，解除限制。"
            release_turn = run_turn(client, session_id, second_prompt)
            release_proposal = find_proposal(release_turn)
            release_state = read_state(client, session_id)
            checks.append({
                "name": "model_prepared_release_proposal",
                "passed": bool(release_proposal)
                and "dumbbell" not in (release_proposal["proposal"]["payload"]["unavailable_equipment"] or []),
                "detail": {"payload": (release_proposal or {}).get("proposal", {}).get("payload")},
            })
            save_chain_confirmation(client, session_id, release_proposal, release_state, checks,
                                    turns, "relieve", counters)
            relieved = read_state(client, session_id)
            checks.append({
                "name": "restriction_relieved_current_value",
                "passed": relieved["profile"]["content"]["unavailable_equipment"] == []
                and relieved["profile"]["version"] == restricted["profile"]["version"] + 1,
                "detail": {"profile": relieved["profile"]},
            })
            evidence["final_read"] = relieved
            evidence["storage_before_restart"] = storage_state(database)
            final = [entry for entry in turns if entry.get("stage") == "relieve"][0]
            evidence["restart_reference"] = {
                "session_id": session_id,
                "final_version": relieved["profile"]["version"],
                "final_content": relieved["profile"]["content"],
                "plan_count": relieved["plan_count"],
                "history_runs": relieved["session"]["runs"],
                "final_proposal_id": final["confirmation"]["request"]["proposal_id"],
                "final_display_entry_id": final["confirmation"]["request"]["display_entry_id"],
                "final_operation_id": final["confirmation"]["request"]["operation_id"],
                "final_confirmation_entry_id": final["confirmation"]["body"]["confirmation_entry_id"],
            }
            write_json(chain_reference_path(), {
                **evidence["restart_reference"],
                "storage_before_restart": evidence["storage_before_restart"],
                "written_at": now_iso(),
            })
    except BaseException as error:
        evidence["fatal"] = {"type": type(error).__name__,
                             "traceback": traceback.format_exc(limit=8)}
        failures.append("fatal")
    evidence["turns"] = turns
    evidence["checks"] = checks
    evidence["counters"] = counters_view(counters)
    evidence["cross_check"] = cross_check("chain", counters, trace_database)
    evidence["failed"] = sorted(set(failures + [entry["name"] for entry in checks if not entry["passed"]]))
    ledger_append("chain", counters, {"kind": "chain", "failed": evidence["failed"]})
    finalize("chain", evidence, CHAIN, evidence["failed"])


def save_chain_confirmation(client, session_id: str, proposal: dict | None, state_before: dict,
                            checks: list[dict], turns: list[dict], stage: str,
                            counters: dict) -> None:
    if proposal is None:
        checks.append({"name": f"{stage}:proposal_present", "passed": False, "detail": {}})
        return
    snapshot = {kind: counters[kind]["attempts"] for kind in counters}
    confirmed = confirm_proposal(client, session_id, proposal, counters)
    after = read_state(client, session_id)
    replay = post_confirmation(
        client, session_id, "profile", confirmed["request"]["proposal_id"],
        confirmed["request"]["display_entry_id"],
        operation_id=confirmed["request"]["operation_id"],
    )
    replay_terminal = wait_for_confirmation(
        client, session_id, confirmed["request"]["operation_id"]
    )
    checks.append({
        "name": f"{stage}:confirmation_saved",
        "passed": confirmed["post_status_code"] in {200, 202}
        and confirmed["terminal"]["final_status"] == "saved",
        "detail": {"post_status_code": confirmed["post_status_code"],
                   "terminal": confirmed["terminal"]},
    })
    checks.append({
        "name": f"{stage}:version_incremented_once",
        "passed": after["profile"]["version"] == state_before["profile"]["version"] + 1,
        "detail": {"before": state_before["profile"]["version"], "after": after["profile"]["version"]},
    })
    checks.append({
        "name": f"{stage}:replay_returns_same_result",
        "passed": replay["status_code"] in {200, 202}
        and replay_terminal["final"] == confirmed["terminal"]["final"],
        "detail": {"post_status_code": replay["status_code"],
                   "replay_terminal": replay_terminal,
                   "same_object": replay_terminal["final"] == confirmed["terminal"]["final"]},
    })
    checks.append({
        "name": f"{stage}:confirmation_calls_no_model",
        "passed": all(counters[kind]["attempts"] == value for kind, value in snapshot.items()),
        "detail": {kind: counters[kind]["attempts"] - value for kind, value in snapshot.items()},
    })
    turns.append({"stage": stage, "proposal": proposal, "confirmation": confirmed,
                  "replay": replay, "state_after": after})


def report_mode() -> None:
    samples = [read_json(path) for path in sorted(SAMPLES.glob("*.json"))]
    first_runs = [item for item in samples if item.get("first_run")]
    def group_results(group: str) -> dict:
        entries = [item for item in first_runs if item["group"] == group]
        return {
            "runs": len(entries),
            "succeeded": sum(1 for item in entries if not item["failed"]),
            "by_scenario": {
                scenario: {
                    "runs": len([item for item in entries if item["scenario"] == scenario]),
                    "succeeded": len([item for item in entries
                                      if item["scenario"] == scenario and not item["failed"]]),
                    "failed_names": sorted({name for item in entries
                                            if item["scenario"] == scenario for name in item["failed"]}),
                }
                for scenario in SCENARIOS
            },
        }

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    latencies = [item["turn"]["elapsed_ms"] for item in first_runs if "turn" in item]
    search_chars = [
        tool["result_chars"] for item in first_runs for tool in item["turn"]["tools"]
        if tool["name"] == "search_exercises"
    ]
    details_chars = [
        tool["result_chars"] for item in first_runs for tool in item["turn"]["tools"]
        if tool["name"] == "get_exercise_details"
    ]
    ledger_lines = (USAGE / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if (USAGE / "ledger.jsonl").is_file() else []
    ledger = [json.loads(line) for line in ledger_lines]

    def totals() -> dict:
        return {
            kind: {
                key: sum(entry[kind].get(key, 0) for entry in ledger if kind in entry)
                for key in ("attempts", "success", "failure")
            }
            for kind in ("chat", "summary", "embedding")
        }

    usage_sums = {
        kind: {
            key: sum(entry[kind].get(key, 0) for entry in ledger if kind in entry)
            for key in ("prompt_tokens", "completion_tokens", "cache_read_tokens",
                        "cache_write_tokens", "total_tokens")
            if any(key in (entry.get(kind) or {}) for entry in ledger)
        }
        for kind in ("chat", "summary", "embedding")
    }
    unknowns = {
        kind: {
            f"{key}_unknown": sum(entry[kind].get(f"{key}_unknown", 0) for entry in ledger if kind in entry)
            for key in ("prompt_tokens", "completion_tokens")
        }
        for kind in ("chat", "summary", "embedding")
    }
    evidence = {
        "mode": "report",
        "generated_at": now_iso(),
        "first_run_success": {"baseline": group_results("baseline"), "optimized": group_results("optimized")},
        "latency_ms": {
            "sample_size": len(latencies),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "tool_result_chars": {
            "search": {"sample_size": len(search_chars), "p50": percentile(search_chars, 0.5),
                       "p95": percentile(search_chars, 0.95), "max": max(search_chars) if search_chars else None},
            "details": {"sample_size": len(details_chars), "p50": percentile(details_chars, 0.5),
                        "p95": percentile(details_chars, 0.95), "max": max(details_chars) if details_chars else None},
        },
        "requests": totals(),
        "provider_usage_tokens": usage_sums,
        "usage_unknown_counts": unknowns,
        "caps": CAPS,
        "token_cap_policy": "聊天输入上限只统计非缓存输入 tokens；cache 读写单独记录且不计入上限（用户裁决）。",
        "cache_usage_tokens": {
            kind: {
                key: sum(entry[kind].get(key, 0) for entry in ledger if kind in entry)
                for key in ("cache_read_tokens", "cache_write_tokens")
            }
            for kind in ("chat",)
        },
        "cap_consumption": {
            "chat_attempts": totals()["chat"]["attempts"],
            "embedding_attempts": totals()["embedding"]["attempts"],
            "summary_attempts": totals()["summary"]["attempts"],
            "chat_prompt_tokens": sum(
                entry["chat"].get("prompt_tokens", 0) for entry in ledger if "chat" in entry
            ),
            "chat_completion_tokens": sum(
                entry["chat"].get("completion_tokens", 0) for entry in ledger if "chat" in entry
            ),
            "embedding_tokens": sum(
                entry["embedding"].get("total_tokens", 0) for entry in ledger if "embedding" in entry
            ),
        },
        "cost": "unknown",
        "ledger_entries": len(ledger),
        "unaccounted_attempts_note": (
            "一次链路执行在确认步骤失败后被中止，其尝试未写入 ledger；"
            "实际尝试数以 isolated/chain/cache-observation 的逐调用记录为准。"
        ),
        "samples_present": [item["sample_id"] for item in samples],
        "auxiliary_present": sorted(path.stem for path in (CHAIN / "chain.json", RESTART / "restart-check.json",
                                                           FAULTS / "faults.json") if path.is_file()),
        "not_measured": [
            "429 限流与 UNAVAILABLE 恢复重连：本轮未触发，保持未实测边界。",
            "共享 Milvus 停服窗口：本轮未执行。",
            "真实浏览器视觉与鼠标自动化：本轮只做组件/动作层验证。",
            "q-cond-002/006/015 业务语义：保持暂缓，未纳入样本。",
        ],
    }
    write_json(BATCH / "summary.json", evidence)
    print(json.dumps({"samples": len(samples), "cap_consumption": evidence["cap_consumption"]},
                     ensure_ascii=False))


def observe_milvus_rpc(stats: dict):
    import app.application.business.retrieval_index as index_module

    original = index_module.MilvusConnection.call

    async def counted(self, method, *args, **kwargs):
        stats["total"] += 1
        stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
        started = time.perf_counter()
        try:
            return await original(self, method, *args, **kwargs)
        finally:
            stats["elapsed_ms"] += (time.perf_counter() - started) * 1000

    index_module.MilvusConnection.call = counted
    return original


def preflight_mode() -> None:
    import test.retrieval_evaluation_support as support
    from app.application.business.retrieval_index import MilvusConnection

    require_source_identity("preflight")
    protected = require_protected_originals()
    counters = new_counters()
    landing = prepare_environment("preflight", counters=counters)
    rpc = {"total": 0, "by_method": {}, "elapsed_ms": 0.0}
    restore = observe_milvus_rpc(rpc)

    async def verify() -> dict:
        corpus = support.RetrievalCorpus.load()
        async with MilvusConnection.from_env() as db:
            collection, identity = await support.open_published(db, corpus)
        frozen = support.load_frozen()
        return {
            "collection": collection,
            "index_identity": identity,
            "matches_frozen": identity == frozen["index_identity"],
            "catalog_size": len(corpus.catalog),
        }

    result = asyncio.run(verify())
    import app.application.business.retrieval_index as index_module

    index_module.MilvusConnection.call = restore
    checks = {
        "index_identity_matches_frozen": result["matches_frozen"],
        "catalog_size_1324": result["catalog_size"] == 1324,
        "no_model_requests": all(entry["attempts"] == 0 for entry in counters.values()),
        "protected_originals_intact": all(
            current == PROTECTED_BASELINE.get(name) or name not in PROTECTED_BASELINE
            for name, current in protected.items()
        ),
    }
    evidence = {
        "mode": "preflight", "started_at": now_iso(), "isolation": landing["landing"],
        "milvus_rpc": rpc, "published": result, "checks": checks,
        "failed": sorted(name for name, passed in checks.items() if not passed),
        "embedding_cache_reference": "tmp/action-retrieval-t8-requalification/formal/summary.json",
        "protected_originals": protected,
    }
    ledger_append("preflight", counters, {"kind": "preflight", "failed": evidence["failed"]})
    finalize("preflight", evidence, BATCH / "preflight", evidence["failed"])


def prebuild_mode(scenario: str) -> None:
    from test import regression_support

    require_source_identity(f"prebuild-{scenario}")
    require_protected_originals()
    if scenario == "verify":
        raise RuntimeError("verify 场景无前置状态，不需要 prebuild")
    prompts = precondition_prompts(scenario)
    database = PRECONDITIONS / f"{scenario}.db"
    if database.exists():
        raise RuntimeError(f"前置源库已存在，禁止覆盖: {database}")
    counters = new_counters()
    landing = prepare_environment(f"prebuild-{scenario}", database, "optimized", counters)
    trace_database = landing["landing"]["trace_database"]
    evidence = {
        "mode": "prebuild", "scenario": scenario, "started_at": now_iso(),
        "prompts": prompts, "isolation": landing["landing"], "model_identity": model_identity(),
        "turns": [],
    }
    checks: list[dict] = []
    try:
        from app.interfaces.http import app

        with regression_support.Server(app) as server:
            client = open_client(server)
            session_id = session_setup(client)
            evidence["session_id"] = session_id
            for prompt in prompts:
                before = read_state(client, session_id)
                turn = run_turn(client, session_id, prompt)
                proposal = find_proposal(turn)
                prepared = read_state(client, session_id)
                checks.append({
                    "name": "prepare_submits_no_business_write",
                    "passed": state_digest(before) == state_digest(prepared),
                    "detail": {"prompt": prompt},
                })
                confirmed = None
                if proposal is not None:
                    confirmed = confirm_proposal(client, session_id, proposal, counters)
                evidence["turns"].append({
                    "prompt": prompt, "turn": turn, "proposal": proposal, "confirmation": confirmed,
                })
            final = read_state(client, session_id)
            evidence["final_state"] = final
            evidence["storage"] = storage_state(database)
            content = final["profile"]["content"] or {}
            checks.append({
                "name": "profile_saved",
                "passed": final["profile"]["version"] == 1
                and content.get("unavailable_equipment") == PROFILE_FIXTURES[scenario]["unavailable_equipment"]
                and not content.get("forbidden_exercise_ids"),
                "detail": {"profile": final["profile"]},
            })
            checks.append({
                "name": "environment_values_captured",
                "passed": content.get("environment") is not None and content.get("goal") is not None,
                "detail": {"environment": content.get("environment"), "goal": content.get("goal"),
                           "experience": content.get("experience"), "availability": content.get("availability")},
            })
            if scenario == "generate":
                checks.append({
                    "name": "generate_home_environment_resolves_body_weight",
                    "passed": home_environment(str(content.get("environment"))),
                    "detail": {"environment": content.get("environment")},
                })
            else:
                pairs = plan_exercise_ids(final["current_plan"]["content"]) if final["current_plan"]["id"] else []
                facts = {exercise_id: exercise_facts(catalog_snapshot(), exercise_id) for _, _, exercise_id in pairs}
                checks.append({
                    "name": "replace_plan_has_dumbbell_back_exercise",
                    "passed": bool(final["current_plan"]["id"]) and any(
                        fact and fact["equipment"] == "dumbbell" and fact["body_part"] == "back"
                        for fact in facts.values()
                    ),
                    "detail": {"plan_ids": sorted(facts),
                               "facts": {key: value for key, value in facts.items() if value}},
                })
    except BaseException as error:
        evidence["fatal"] = {"type": type(error).__name__,
                             "traceback": traceback.format_exc(limit=8)}
        checks.append({"name": "fatal", "passed": False, "detail": evidence["fatal"]})
    evidence["checks"] = checks
    evidence["counters"] = counters_view(counters)
    evidence["cross_check"] = cross_check(f"prebuild-{scenario}", counters, trace_database)
    evidence["failed"] = sorted({entry["name"] for entry in checks if not entry["passed"]})
    if evidence["failed"]:
        database.unlink(missing_ok=True)
    ledger_append(f"prebuild-{scenario}", counters, {"kind": "prebuild", "scenario": scenario,
                                                    "failed": evidence["failed"]})
    finalize(f"prebuild-{scenario}", evidence, PRECONDITIONS, evidence["failed"])


def catalog_snapshot():
    from app.application.business.catalog import Catalog

    return Catalog.load()


def home_environment(value: str) -> bool:
    from app.application.business import retrieval_text as rt
    from app.application.business.retrieval_conditions import (
        HOME_ENVIRONMENT_TOKENS,
        HOME_ENVIRONMENT_VALUES,
    )

    normalized = rt.normalize(value)
    return normalized in HOME_ENVIRONMENT_VALUES or any(
        token in normalized for token in (*HOME_ENVIRONMENT_TOKENS, "户外", "室外")
    )


def wait_for_confirmation(client, session_id: str, operation_id: str, timeout: float = 90.0) -> dict:
    # 按钮受理即返回；保存可能在后台完成任务，按同一 operation 轮询真实终态。
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        response = client.get(f"/api/sessions/{session_id}/confirmations/{operation_id}")
        polls += 1
        body = response.json() if response.status_code == 200 else None
        status = (body or {}).get("status")
        if status in {"saved", "failed"}:
            return {"final": body, "polls": polls, "final_status": status}
        if time.monotonic() >= deadline:
            return {"final": body, "polls": polls, "final_status": status, "timed_out": True}
        time.sleep(0.2)


def confirm_proposal(
    client, session_id: str, proposal: dict, counters: dict | None = None
) -> dict:
    # 传入真实计数器时给出前后差值；未接入观测时 model_request_delta 记为 null 并注明未观测。
    identifiers = read_pending(client, session_id, proposal["business_kind"])
    before = {kind: counters[kind]["attempts"] for kind in counters} if counters is not None else None
    confirmed = post_confirmation(
        client, session_id, proposal["business_kind"], identifiers["proposal_id"],
        identifiers["display_entry_id"],
    )
    terminal = wait_for_confirmation(
        client, session_id, confirmed["request"]["operation_id"]
    )
    result = {
        **confirmed,
        "post_status_code": confirmed["status_code"],
        "terminal": terminal,
        "model_request_delta": (
            None if counters is None
            else {kind: counters[kind]["attempts"] - value for kind, value in before.items()}
        ),
        "pending": identifiers,
    }
    if counters is None:
        result["model_request_delta_note"] = "未观测"
    return result


def read_pending(client, session_id: str, business_kind: str) -> dict:
    # 展示绑定与待确认条目 id 一律从服务端读取，禁止由测试自造；此读取不调用模型。
    # 字段严格照 history.pending_proposals 白名单投影，不读取不存在的字段，也不给生产投影加字段。
    history = client.get(f"/api/sessions/{session_id}/history").json()
    reference = next(
        (item for item in history["pending_proposals"] if item["business_kind"] == business_kind), None
    )
    if reference is None:
        raise RuntimeError(f"服务端无待确认{business_kind}提案: {json.dumps(history['pending_proposals'])}")
    return {
        "proposal_id": reference["proposal_id"],
        "status": reference["status"],
        "request_entry_id": reference["request_entry_id"],
        "source_entry_id": reference["source_entry_id"],
        "display_entry_id": reference["display_entry_id"],
        "confirmation_entry_id": reference["confirmation_entry_id"],
        "pending_from_history": True,
    }


def probe_mode() -> None:
    from test import regression_support

    require_source_identity("probe")
    require_protected_originals()
    counters = new_counters()
    landing = prepare_environment("probe", None, "optimized", counters)
    trace_database = landing["landing"]["trace_database"]
    evidence = {"mode": "probe", "started_at": now_iso(), "isolation": landing["landing"],
                "turns": []}
    checks: list[dict] = []
    try:
        from app.interfaces.http import app

        with regression_support.Server(app) as server:
            client = open_client(server)
            session_id = session_setup(client)
            short = run_turn(client, session_id, "只回答：完成")
            evidence["turns"].append({"prompt": "只回答：完成", "turn": short})
            checks.append({"name": "short_turn_completed",
                           "passed": short["terminal_event"] == "done", "detail": {}})
            checks.append({"name": "short_turn_without_retrieval_tools",
                           "passed": not [tool for tool in short["tools"]
                                         if tool["name"] in ("search_exercises", "get_exercise_details")],
                           "detail": {"tools": [tool["name"] for tool in short["tools"]]}})
            search = run_turn(client, session_id, "杠铃耸肩需要用到什么器械？")
            evidence["turns"].append({"prompt": "杠铃耸肩需要用到什么器械？", "turn": search})
            searches = [tool for tool in search["tools"] if tool["name"] == "search_exercises"]
            checks.append({"name": "online_embedding_exercised",
                           "passed": counters["embedding"]["attempts"] >= 1 and bool(searches),
                           "detail": {"embedding_attempts": counters["embedding"]["attempts"],
                                      "search_arguments": [tool["arguments"] for tool in searches]}})
        cross = cross_check("probe", counters, trace_database)
        checks.append({"name": "chat_counter_matches_observed_calls",
                       "passed": cross["agreement"]["chat_matches_observed_calls"],
                       "detail": cross["agreement"]})
        checks.append({"name": "chat_plus_summary_matches_trace_model_spans",
                       "passed": cross["agreement"]["chat_and_summary_match_trace_spans"] is not False,
                       "status": "verified" if cross["agreement"]["trace_spans_comparable"] else "unverified",
                       "detail": {"trace": cross["trace_model_spans"],
                                  "chat": counters["chat"]["attempts"],
                                  "summary": counters["summary"]["attempts"]}})
        checks.append({"name": "cache_observation_records_present",
                       "passed": cross["cache_observation_records"] >= counters["chat"]["attempts"],
                       "detail": {"records": cross["cache_observation_records"]}})
        evidence["cross_check"] = cross
    except BaseException as error:
        evidence["fatal"] = {"type": type(error).__name__,
                             "traceback": traceback.format_exc(limit=8)}
        checks.append({"name": "fatal", "passed": False, "detail": evidence["fatal"]})
    evidence["checks"] = checks
    evidence["counters"] = counters_view(counters)
    evidence["failed"] = sorted({entry["name"] for entry in checks if not entry["passed"]})
    ledger_append("probe", counters, {"kind": "probe", "failed": evidence["failed"]})
    finalize("probe", evidence, BATCH / "probe", evidence["failed"])


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="T8 真实业务验收夹具")
    parser.add_argument(
        "mode",
        choices=["selfcheck", "plan", "preflight", "probe", "prebuild", "sample",
                 "chain", "restart-check", "faults", "report"],
    )
    parser.add_argument("--id", help="sample 模式的样本标识，取自 manifest/plan.json")
    parser.add_argument("--scenario", choices=list(SCENARIOS), help="prebuild 模式的前置场景")
    args = parser.parse_args(argv)
    if args.mode == "selfcheck":
        selfcheck()
    elif args.mode == "plan":
        plan_mode()
    elif args.mode == "preflight":
        preflight_mode()
    elif args.mode == "probe":
        probe_mode()
    elif args.mode == "prebuild":
        if args.scenario is None:
            raise RuntimeError("prebuild 需要 --scenario")
        prebuild_mode(args.scenario)
    elif args.mode == "sample":
        if args.id is None:
            raise RuntimeError("sample 需要 --id")
        scenario, repeat, group = parse_sample_id(args.id)
        run_mode(args.id, scenario, group, repeat)
    elif args.mode == "chain":
        chain_mode()
    elif args.mode == "restart-check":
        restart_mode()
    elif args.mode == "faults":
        faults_mode()
    else:
        report_mode()


if __name__ == "__main__":
    main(sys.argv[1:])
