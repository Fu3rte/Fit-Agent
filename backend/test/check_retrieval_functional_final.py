import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from test import check_retrieval_business_e2e as e2e
from test import regression_support

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
ROOT = REPO / "tmp" / "action-retrieval-functional-final"
EXECUTOR = os.environ.get("FUNCTIONAL_EXECUTOR_ID", "independent-executor")
# 真实配置仅在内存注入，进程结束时按现有生命周期恢复。
_RESTORERS: list = []
SECRET_ENV = ("FIT_AGENT_TEST_MODEL_API_KEY", "DASHSCOPE_API_KEY", "FIT_AGENT_MILVUS_TOKEN")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_directory() -> Path:
    value = os.environ.get("FUNCTIONAL_RUN_DIR")
    if not value:
        raise RuntimeError("缺少 FUNCTIONAL_RUN_DIR：功能入口必须在专用运行目录内执行")
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_identity() -> dict:
    snapshot = e2e.source_snapshot()
    return {"head": snapshot["head"], "file_sha256": snapshot["file_sha256"]}


def check(name: str, passed: bool, detail: object = None) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail if detail is not None else {}}


def install_isolation(namespace: str, database_path: Path | None = None) -> dict:
    # 逐项把业务库、观测库、模型配置目录与缓存观测落点指向本轮运行根，并回报实际落点。
    from app import model_config
    from app.agent import cache_observation
    from app.domain.session import attachments
    from app.infrastructure.persistence.sqlite import database as database_module

    root = run_directory() / "isolated" / namespace
    root.mkdir(parents=True, exist_ok=True)
    regression_support.TEMP_ROOT = root
    if database_path is None:
        path = regression_support.patch_default_database(f"db-{namespace}")
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        path = database_path
        database_module.default_database_path = lambda: path
    trace_path = regression_support.patch_trace_database(f"trace-{namespace}")
    observation_root = root / "cache-observation"
    observation_root.mkdir(parents=True, exist_ok=True)
    cache_observation.observation_directory = lambda: observation_root
    model_root = root / "model-config"
    model_config.DATA_ROOT = model_root
    attachments.TMP_ROOT = root / "attachments"
    (root / "attachments").mkdir(parents=True, exist_ok=True)
    base = run_directory()
    return {
        "namespace": namespace,
        "root": root.relative_to(base).as_posix(),
        "database": path.relative_to(base).as_posix(),
        "trace_database": trace_path.relative_to(base).as_posix(),
        "model_config_root": model_root.relative_to(base).as_posix(),
        "cache_observation_root": observation_root.relative_to(base).as_posix(),
        "attachment_root": (root / "attachments").relative_to(base).as_posix(),
    }


def install_in_memory_model_config() -> dict:
    # 真实模型配置仅在内存中使用：从测试环境变量构造 ModelConfig，接入 http 实际查找点，不落盘。
    import app.interfaces.http as http
    from app import model_config

    api = os.environ.get("FIT_AGENT_TEST_MODEL_API", "openai-completions")
    base_url = os.environ.get("FIT_AGENT_TEST_MODEL_BASE_URL")
    identifier = os.environ.get("FIT_AGENT_TEST_MODEL_ID")
    api_key = os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY")
    provider = os.environ.get("FIT_AGENT_TEST_MODEL_PROVIDER")
    missing = [
        name
        for name, value in (
            ("FIT_AGENT_TEST_MODEL_BASE_URL", base_url),
            ("FIT_AGENT_TEST_MODEL_ID", identifier),
            ("FIT_AGENT_TEST_MODEL_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("缺少测试模型环境变量: " + ", ".join(missing))
    snapshot = model_config.ModelConfig(
        api=api,
        base_url=model_config.validate_http_url(base_url),
        model=identifier,
        provider=model_config.effective_provider(api, provider),
        api_key=api_key,
    )
    status = model_config.ProviderStatus(
        api=snapshot.api,
        base_url=snapshot.base_url,
        model=snapshot.model,
        provider=snapshot.provider,
        api_key=snapshot.api_key,
    )
    from app.interfaces.http_api.routes import provider as provider_routes

    from app.interfaces.http_api.routes import agent as agent_routes

    original_load = agent_routes.load_model_config
    original_status = provider_routes.load_provider_status
    original_key = http.saved_api_key
    # from-import 绑定在实际使用模块，替换各自的查找位置。
    agent_routes.load_model_config = lambda: snapshot
    provider_routes.load_provider_status = lambda: status
    http.saved_api_key = lambda: snapshot.api_key

    def restore() -> None:
        agent_routes.load_model_config = original_load
        provider_routes.load_provider_status = original_status
        http.saved_api_key = original_key

    _RESTORERS.append(restore)
    return {
        "api": snapshot.api,
        "model": snapshot.model,
        "provider": snapshot.provider,
        "api_key_present": bool(snapshot.api_key),
        "persisted_to_disk": False,
    }


def prepare_environment(namespace: str, database_path: Path | None = None) -> dict:
    # 隔离补丁之后再导入真实应用：模块级连接与默认路径必须指向本轮运行根。
    landing = install_isolation(namespace, database_path)
    model = install_in_memory_model_config()
    import app.agent.tools.files as file_tools
    from app.interfaces.http_api.routes import agent as agent_routes

    original_file_tools = agent_routes.create_file_tools
    workspace_root = run_directory() / landing["attachment_root"]
    agent_routes.create_file_tools = lambda session_id: original_file_tools(
        session_id, tmp_root=workspace_root
    )
    file_tools.TMP_ROOT = workspace_root
    return {
        "landing": landing,
        "model_config": model,
        "file_tools_patched": agent_routes.create_file_tools is not original_file_tools,
    }


def tool_payload(message) -> dict:
    return json.loads("".join(block.text for block in message.content))


def tool_text(message) -> str:
    return "".join(block.text for block in message.content)


def search_matches(tool) -> list[str]:
    payload = json.loads(tool["result_text"])
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return [note for item in items for note in item.get("matches", [])]


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


class _FakeConfirmResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload


class _FakeConfirmClient:
    # 离线驱动共享确认函数：history 给出白名单画像，确认 POST 与终态轮询返回固定形状。
    def __init__(self, reference: dict, terminal: dict, on_post=None) -> None:
        self._reference = reference
        self._terminal = terminal
        self._on_post = on_post

    def get(self, url: str) -> _FakeConfirmResponse:
        if url.endswith("/history"):
            return _FakeConfirmResponse({"pending_proposals": [self._reference]})
        return _FakeConfirmResponse(self._terminal)

    def post(self, _url: str, json: dict) -> _FakeConfirmResponse:
        if self._on_post is not None:
            self._on_post()
        return _FakeConfirmResponse(json)


def selfcheck_mode(run: Path) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    landing = install_isolation("selfcheck")
    checks.append(check(
        "isolation_landed_under_run",
        all(not str(value).startswith(str(BACKEND / "data"))
            for value in landing.values() if isinstance(value, str)),
        landing,
    ))

    generate_prompts = e2e.precondition_prompts("generate")
    replace_prompts = e2e.precondition_prompts("replace")
    checks.append(check(
        "precondition_generate_single_full_prompt",
        generate_prompts == [e2e.GENERATE_PRECONDITION_PROMPT]
        and all(isinstance(item, str) and len(item) > 8 for item in generate_prompts),
        {"rounds": len(generate_prompts)},
    ))
    checks.append(check(
        "precondition_replace_two_full_prompts",
        replace_prompts == list(e2e.REPLACE_PRECONDITION_PROMPTS)
        and len(replace_prompts) == e2e.PRECONDITION_ROUNDS["replace"] == 2
        and all(isinstance(item, str) and len(item) > 8 for item in replace_prompts),
        {"rounds": len(replace_prompts)},
    ))
    checks.append(check(
        "precondition_rounds_are_literal",
        e2e.PRECONDITION_ROUNDS == {"generate": 1, "replace": 2}
        and all(isinstance(e2e.PRECONDITION_PROMPTS[s], tuple) for s in e2e.PRECONDITION_ROUNDS),
        {},
    ))
    # 裸 str 退化形态必须被拒绝：否则会按字符展开成 66 个单字回合。
    original = e2e.PRECONDITION_PROMPTS["generate"]
    e2e.PRECONDITION_PROMPTS["generate"] = e2e.GENERATE_PRECONDITION_PROMPT
    try:
        try:
            e2e.precondition_prompts("generate")
            rejected = False
        except RuntimeError:
            rejected = True
    finally:
        e2e.PRECONDITION_PROMPTS["generate"] = original
    checks.append(check("precondition_rejects_bare_str", rejected, {}))

    # read_pending 只读 history.pending_proposals 白名单字段：history 缺少 run_id 时不得失败。
    reference = {
        "business_kind": "plan",
        "proposal_id": "11111111-1111-4111-8111-111111111111",
        "status": "pending",
        "request_entry_id": "22222222-2222-4222-8222-222222222222",
        "source_entry_id": "33333333-3333-4333-8333-333333333333",
        "display_entry_id": "44444444-4444-4444-8444-444444444444",
        "confirmation_entry_id": None,
    }
    try:
        pending = e2e.read_pending(
            _FakeClient({"pending_proposals": [reference]}), "session", "plan"
        )
        keys_ok = set(pending) == {
            "proposal_id", "status", "request_entry_id", "source_entry_id",
            "display_entry_id", "confirmation_entry_id", "pending_from_history",
        }
        values_ok = pending["display_entry_id"] == reference["display_entry_id"] and \
            pending["request_entry_id"] == reference["request_entry_id"] and \
            "run_id" not in pending
    except BaseException as error:
        keys_ok = False
        values_ok = False
        pending = {"error": f"{type(error).__name__}: {error}"}
    checks.append(check("read_pending_whitelist_only", keys_ok and values_ok, pending))

    from app.application.business.retrieval_frozen import frozen_values
    values = frozen_values()
    checks.append(check(
        "runtime_frozen_parameters_wired",
        values == {"fusion": "rrf", "rrf_k": 60, "bm25_depth": 100, "dense_depth": 100,
                   "threshold": 0.0, "recall_limit": 11},
        values,
    ))

    import app.interfaces.http as http
    from app import model_config
    from app.application.session.credentials import CredentialFilter
    injection = install_in_memory_model_config()
    from app.interfaces.http_api.routes import agent as agent_routes

    snapshot = agent_routes.load_model_config()
    live_key = os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY")
    detected = CredentialFilter(http.active_secrets()).contains("pre" + live_key + "post") \
        if live_key else False
    checks.append(check(
        "memory_injection_uses_real_config_type_and_detection",
        isinstance(snapshot, model_config.ModelConfig)
        and injection["api_key_present"]
        and injection["persisted_to_disk"] is False
        and bool(live_key) and http.saved_api_key() == live_key and detected,
        {"snapshot_type": type(snapshot).__name__,
         "api_key_present": injection["api_key_present"],
         "persisted_to_disk": injection["persisted_to_disk"],
         "credential_detected": detected},
    ))
    checks.append(check(
        "memory_injection_writes_no_credential_file",
        not list((run / "isolated" / "selfcheck" / "model-config").rglob("auth.json")),
        {"model_config_root": landing["model_config_root"]},
    ))

    confirm_reference = {
        "business_kind": "plan",
        "proposal_id": "11111111-1111-4111-8111-111111111111",
        "status": "pending",
        "request_entry_id": "22222222-2222-4222-8222-222222222222",
        "source_entry_id": "33333333-3333-4333-8333-333333333333",
        "display_entry_id": "44444444-4444-4444-8444-444444444444",
        "confirmation_entry_id": None,
    }
    confirm_terminal = {"status": "saved",
                        "confirmation_entry_id": "55555555-5555-4555-8555-555555555555"}
    unobserved = e2e.confirm_proposal(
        _FakeConfirmClient(confirm_reference, confirm_terminal), "session", {"business_kind": "plan"}
    )
    checks.append(check(
        "confirm_without_observations_reports_null",
        unobserved["model_request_delta"] is None
        and unobserved["model_request_delta_note"] == "未观测"
        and unobserved["terminal"]["final_status"] == "saved",
        {"model_request_delta": unobserved["model_request_delta"],
         "model_request_delta_note": unobserved["model_request_delta_note"]},
    ))
    counters = {"chat": {"attempts": 2}, "summary": {"attempts": 0}, "embedding": {"attempts": 0}}

    def bump() -> None:
        counters["chat"]["attempts"] = 5

    observed = e2e.confirm_proposal(
        _FakeConfirmClient(confirm_reference, confirm_terminal, bump),
        "session", {"business_kind": "plan"}, counters,
    )
    checks.append(check(
        "confirm_with_real_counters_keeps_delta",
        observed["model_request_delta"] == {"chat": 3, "summary": 0, "embedding": 0}
        and "model_request_delta_note" not in observed,
        {"model_request_delta": observed["model_request_delta"]},
    ))
    return checks, {"isolation": landing}


def eval_prep_mode(run: Path) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    install_isolation("eval-prep")
    import importlib

    from evaluation import retrieval_support as support

    module = importlib.import_module("evaluation.retrieval")
    checks.append(check("evaluation_entry_importable",
                        module.__name__ == "evaluation.retrieval",
                        {"module": module.__name__}))

    environment = dict(os.environ)
    environment.setdefault("PYTHONPATH", str(BACKEND))
    environment["PYTHONIOENCODING"] = "utf-8"
    help_run = subprocess.run(
        [sys.executable, "-m", "evaluation.retrieval", "--help"],
        cwd=BACKEND, env=environment, capture_output=True, text=True, encoding="utf-8",
    )
    bad_run = subprocess.run(
        [sys.executable, "-m", "evaluation.retrieval", "not-a-mode"],
        cwd=BACKEND, env=environment, capture_output=True, text=True, encoding="utf-8",
    )
    checks.append(check("evaluation_cli_parses",
                        help_run.returncode == 0 and bad_run.returncode == 2,
                        {"help_exit": help_run.returncode, "bad_mode_exit": bad_run.returncode}))

    corpus = support.RetrievalCorpus.load()
    verification = support.verify_inputs(corpus.catalog)
    checks.append(check("approved_inputs_verified", verification["checks"]["passed"],
                        {"counts": verification["counts"]}))

    from evaluation.retrieval_support import load_queries
    dev = load_queries("dev")
    holdout = load_queries("holdout")
    checks.append(check("dev_holdout_split_intact",
                        len(dev) == 61 and len(holdout) == 67,
                        {"dev": len(dev), "holdout": len(holdout)}))

    frozen = support.load_frozen()
    wiring = support.frozen_config_identity()
    checks.append(check("frozen_config_hash_matches_ledger",
                        wiring["sha256"] == frozen["frozen_config_identity"]["sha256"],
                        {"runtime": wiring["sha256"], "ledger": frozen["frozen_config_identity"]["sha256"]}))
    checks.append(check("runtime_wiring_matches_frozen",
                        all(wiring["values"][key] == frozen[key] for key in
                            ("alpha", "bm25_depth", "dense_depth", "threshold"))
                        and wiring["values"]["recall_limit"] == 11,
                        wiring["values"]))

    from app.application.business.retrieval_embedding import EmbeddingClient
    identity = support.query_vector_identity(EmbeddingClient.from_env().config, corpus)
    try:
        vectors, entries = support.read_query_cache(identity, support.query_documents())
        cache_ok = len(vectors) == 128 and all(
            len(vector) == identity["dimensions"] for vector in vectors.values()
        )
        cache_detail = {"entries": len(entries), "dimensions": identity["dimensions"],
                        "cache_path": str(support.cache_path(identity))}
    except support.EvaluationBlocked as error:
        cache_ok = False
        cache_detail = {"blocked": error.reason, "missing": list(error.missing)[:5]}
    checks.append(check("real_vector_cache_readable", cache_ok, cache_detail))

    ledgers = {
        "frozen": support.FROZEN_PATH.is_file(),
        "holdout_ledger": support.HOLDOUT_LEDGER_PATH.is_file(),
        "requal_authorization": support.REQUAL_AUTH_PATH.is_file(),
        "requal_ledger": support.REQUAL_LEDGER_PATH.is_file(),
    }
    checks.append(check("historical_ledgers_preserved", all(ledgers.values()), ledgers))
    return checks, {"identity": identity, "cache": cache_detail, "ledgers": ledgers}


def search_mode(run: Path) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    landing = install_isolation("search")
    from evaluation import retrieval_support as support
    from app.agent.tool import run_tool_call
    from app.agent.tools.business import bind_business_tools, business_tool_declarations
    from app.ai.messages import ToolCall
    from test.check_profile_confirmation import Fixture

    declared = {item.name: item for item in business_tool_declarations()}
    database = run / landing["database"]
    queries = support.query_documents()

    def first_of(intent: str) -> dict | None:
        return next((item for item in queries
                     if item["split"] == "dev" and item["intent"] == intent
                     and not item["expect_empty"] and item["labels"]), None)

    selected = [item for item in (first_of("alias"), first_of("english"), first_of("description")) if item]
    checks.append(check("predetermined_dev_queries_selected", len(selected) == 3,
                        {"ids": [item["id"] for item in selected],
                         "intents": [item["intent"] for item in selected]}))

    evidence: dict = {"queries": [], "errors": {}}

    async def call(tool, arguments: dict):
        return await run_tool_call(
            ToolCall(type="toolCall", id="call-" + uuid4().hex, name=tool.name, arguments=arguments),
            tools={tool.name: tool}, declared={tool.name: declared[tool.name]},
        )

    async def execute() -> None:
        fixture = await Fixture(database).seeded()
        try:
            session_id = await fixture.session("功能检索会话")
            context = fixture.context(session_id, session_id, session_id)
            tools = bind_business_tools(fixture.business, context, fixture.call, {}, {})
            search_tool, details_tool = tools["search_exercises"], tools["get_exercise_details"]
            catalog = fixture.catalog
            nine_fields = {"id", "name", "body_part", "equipment", "target", "muscle_group",
                           "secondary_muscles", "load_convention", "steps"}
            for query in selected:
                filters = {key: value for key, value in query["filters"].items() if value is not None}
                message = await call(search_tool, {"purpose": "lookup", "name": query["query"], **filters})
                payload = tool_payload(message)
                text = tool_text(message)
                expected = [label["id"] for label in query["labels"] if label["grade"] > 0]
                items = payload.get("items", []) if isinstance(payload, dict) else []
                returned = [item["id"] for item in items]
                entry = {"query_id": query["id"], "intent": query["intent"], "query": query["query"],
                         "filters": filters, "expected_labels": expected, "returned": returned,
                         "search_chars": len(text), "is_error": message.is_error}
                checks.append(check(f"search_ok_{query['id']}", not message.is_error, entry))
                checks.append(check(f"search_shape_{query['id']}",
                                    isinstance(payload, dict)
                                    and set(payload) == {"items", "has_more"},
                                    {"keys": sorted(payload) if isinstance(payload, dict) else None}))
                checks.append(check(f"search_budget_{query['id']}",
                                    len(returned) <= 5 and len(text) <= 4000,
                                    {"items": len(returned), "chars": len(text)}))
                checks.append(check(f"search_expected_hit_{query['id']}",
                                    bool(set(expected) & set(returned)),
                                    {"expected": expected, "returned": returned}))
                checks.append(check(f"search_item_fields_{query['id']}",
                                    all(set(item) == {"id", "name", "equipment", "target", "matches"}
                                        and item["id"] in catalog.exercise_ids
                                        and item["equipment"] == catalog.get(item["id"]).equipment
                                        and item["target"] == catalog.get(item["id"]).target
                                        for item in items),
                                    {}))

                detail_ids = expected[:2]
                details = await call(details_tool, {"ids": detail_ids})
                detail_payload = tool_payload(details)
                detail_text = tool_text(details)
                detail_items = detail_payload.get("items", []) if isinstance(detail_payload, dict) else []
                checks.append(check(f"details_ok_{query['id']}", not details.is_error, {}))
                checks.append(check(f"details_shape_{query['id']}",
                                    isinstance(detail_payload, dict)
                                    and set(detail_payload) == {"items", "has_more", "remaining_ids"}, {}))
                checks.append(check(f"details_order_{query['id']}",
                                    [item["id"] for item in detail_items] == detail_ids,
                                    {"requested": detail_ids,
                                     "returned": [item["id"] for item in detail_items]}))
                checks.append(check(f"details_nine_fields_{query['id']}",
                                    bool(detail_items)
                                    and all(set(item) == nine_fields for item in detail_items), {}))
                checks.append(check(f"details_steps_real_{query['id']}",
                                    bool(detail_items)
                                    and all(item["steps"].get("zh") for item in detail_items), {}))
                checks.append(check(f"details_budget_{query['id']}", len(detail_text) <= 7000,
                                    {"chars": len(detail_text)}))
                entry["details_ids"] = detail_ids
                entry["details_chars"] = len(detail_text)
                evidence["queries"].append(entry)

            unknown = await call(details_tool, {"ids": ["9999"]})
            unknown_payload = tool_payload(unknown)
            evidence["errors"]["unknown_id"] = unknown_payload
            checks.append(check("details_unknown_id_errors",
                                unknown.is_error and unknown_payload.get("code") == "exercise_not_found",
                                unknown_payload))

            bad_condition = await call(
                search_tool, {"purpose": "lookup", "name": "卧推", "equipment": "不存在的器械"}
            )
            bad_condition_payload = tool_payload(bad_condition)
            evidence["errors"]["illegal_condition"] = bad_condition_payload
            checks.append(check("search_illegal_condition_errors",
                                bad_condition.is_error
                                and bad_condition_payload.get("code") == "invalid_search_condition",
                                bad_condition_payload))

            bad_params = await call(search_tool, {"purpose": "search", "name": "卧推"})
            bad_params_payload = tool_payload(bad_params)
            evidence["errors"]["bad_parameters"] = bad_params_payload
            checks.append(check("search_bad_parameters_errors",
                                bad_params.is_error
                                and bad_params_payload.get("code") == "invalid_tool_arguments",
                                bad_params_payload))

            duplicate = await call(details_tool, {"ids": ["0001", "0001"]})
            duplicate_payload = tool_payload(duplicate)
            evidence["errors"]["duplicate_ids"] = duplicate_payload
            checks.append(check("details_duplicate_ids_errors",
                                duplicate.is_error
                                and duplicate_payload.get("code") == "invalid_tool_arguments",
                                duplicate_payload))
        finally:
            await fixture.close()

    asyncio.run(execute())
    return checks, {"isolation": landing, **evidence}


def generation_mode(run: Path) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    landing = prepare_environment("generate", None)
    from app.interfaces.http import app

    evidence: dict = {"isolation": landing["landing"]}
    with regression_support.Server(app) as server:
        client = e2e.open_client(server)
        session_id = e2e.session_setup(client)
        evidence["session_id"] = session_id
        before = e2e.read_state(client, session_id)

        prompts = e2e.precondition_prompts("generate")
        checks.append(check("generate_precondition_single_round", len(prompts) == 1,
                            {"rounds": len(prompts)}))
        profile_turn = e2e.run_turn(client, session_id, prompts[0])
        profile_proposal = e2e.find_proposal(profile_turn)
        prepared = e2e.read_state(client, session_id)
        evidence["profile_turn"] = profile_turn
        checks.append(check("profile_prepare_no_commit",
                            e2e.state_digest(before) == e2e.state_digest(prepared)
                            and prepared["profile"]["version"] is None,
                            {"profile": prepared["profile"]}))
        checks.append(check("profile_proposal_prepared",
                            bool(profile_proposal)
                            and profile_proposal["tool"] == "prepare_profile_update",
                            {"tool": (profile_proposal or {}).get("tool"),
                             "turn_tools": [tool["name"] for tool in profile_turn["tools"]],
                             "final_text": profile_turn.get("final_text")}))
        if profile_proposal is None:
            return checks, evidence
        confirmed_profile = e2e.confirm_proposal(client, session_id, profile_proposal)
        saved = e2e.read_state(client, session_id)
        content = saved["profile"]["content"] or {}
        checks.append(check("profile_confirmed_saved",
                            confirmed_profile["terminal"]["final_status"] == "saved"
                            and saved["profile"]["version"] == 1,
                            {"terminal": confirmed_profile["terminal"],
                             "version": saved["profile"]["version"]}))
        checks.append(check("profile_home_barbell_restriction",
                            (content.get("unavailable_equipment") or []) == ["barbell"]
                            and (content.get("forbidden_exercise_ids") or []) == []
                            and e2e.home_environment(str(content.get("environment"))),
                            {"content": content}))
        evidence["profile_confirmation"] = confirmed_profile

        generate_turn = e2e.run_turn(client, session_id, e2e.USER_INPUTS["generate"])
        proposal = e2e.find_proposal(generate_turn)
        searches = [tool for tool in generate_turn["tools"] if tool["name"] == "search_exercises"]
        notes = [note for tool in searches for note in search_matches(tool)]
        evidence["turn"] = generate_turn
        # 检索层判据独立于是否产出计划快照：用途、环境默认与已保存限制在真实结果中直接可见。
        checks.append(check("generate_search_purpose_generation",
                            any(tool["arguments"].get("purpose") == "generation" for tool in searches),
                            {"arguments": [tool["arguments"] for tool in searches]}))
        checks.append(check("generate_search_applies_home_default_and_restriction",
                            "环境=居家" in notes and "不可用器械含barbell" in notes,
                            {"notes": sorted(set(notes))}))
        if proposal is None:
            checks.append(check("generate_plan_proposed", False,
                                {"turn_tools": [tool["name"] for tool in generate_turn["tools"]],
                                 "final_text": generate_turn.get("final_text")}))
            return checks, evidence
        after_prepare = e2e.read_state(client, session_id)
        checks.append(check("generate_plan_proposed",
                            proposal["tool"] == "prepare_plan" and proposal["business_kind"] == "plan",
                            {"tool": proposal["tool"]}))
        checks.append(check("generate_prepare_no_commit",
                            e2e.state_digest(saved) == e2e.state_digest(after_prepare),
                            {"before": e2e.state_digest(saved),
                             "prepared": e2e.state_digest(after_prepare)}))
        payload = (proposal.get("proposal") or {}).get("payload") or {}
        pairs = e2e.plan_exercise_ids(payload) if payload else []
        catalog = e2e.catalog_snapshot()
        facts = {exercise_id: e2e.exercise_facts(catalog, exercise_id) for _, _, exercise_id in pairs}
        equipments = sorted({fact["equipment"] for fact in facts.values() if fact})
        checks.append(check("generate_plan_exercises_in_catalog",
                            bool(pairs) and all(facts.values()), {"ids": sorted(facts)}))
        checks.append(check("generate_home_default_body_weight_only",
                            bool(equipments) and set(equipments) <= {"body weight"},
                            {"equipments": equipments,
                             "arguments": [tool["arguments"] for tool in searches]}))
        confirmed = e2e.confirm_proposal(client, session_id, proposal)
        after = e2e.read_state(client, session_id)
        checks.append(check("generate_confirmed_saved_plan",
                            confirmed["terminal"]["final_status"] == "saved"
                            and after["plan_count"] == saved["plan_count"] + 1
                            and (after["current_plan"] or {}).get("id") is not None,
                            {"terminal": confirmed["terminal"]["final_status"],
                             "plan_count": after["plan_count"]}))
        replay = e2e.post_confirmation(
            client, session_id, "plan", confirmed["request"]["proposal_id"],
            confirmed["request"]["display_entry_id"],
            operation_id=confirmed["request"]["operation_id"],
        )
        replay_terminal = e2e.wait_for_confirmation(
            client, session_id, confirmed["request"]["operation_id"]
        )
        replay_state = e2e.read_state(client, session_id)
        same_node = (replay_terminal["final"] or {}).get("confirmation_entry_id") == \
            (confirmed["terminal"]["final"] or {}).get("confirmation_entry_id")
        checks.append(check("generate_confirmation_idempotent",
                            replay["status_code"] in {200, 202}
                            and replay_terminal["final_status"] == "saved" and same_node
                            and replay_state["plan_count"] == after["plan_count"],
                            {"post_status": replay["status_code"], "same_node": same_node,
                             "plan_count": replay_state["plan_count"]}))
        evidence["proposal"] = proposal
        evidence["confirmation"] = confirmed
    return checks, evidence


def replacement_mode(run: Path) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    landing = prepare_environment("replace", None)
    from app.interfaces.http import app

    evidence: dict = {"isolation": landing["landing"]}
    with regression_support.Server(app) as server:
        client = e2e.open_client(server)
        session_id = e2e.session_setup(client)
        evidence["session_id"] = session_id
        e2e.read_state(client, session_id)
        prompts = e2e.precondition_prompts("replace")
        checks.append(check("replace_precondition_two_rounds", len(prompts) == 2,
                            {"rounds": len(prompts)}))

        profile_turn = e2e.run_turn(client, session_id, prompts[0])
        profile_proposal = e2e.find_proposal(profile_turn)
        evidence["profile_turn"] = profile_turn
        checks.append(check("replace_precondition_profile_proposed",
                            bool(profile_proposal)
                            and profile_proposal["tool"] == "prepare_profile_update",
                            {"tool": (profile_proposal or {}).get("tool"),
                             "final_text": profile_turn.get("final_text")}))
        if profile_proposal is None:
            return checks, evidence
        confirmed_profile = e2e.confirm_proposal(client, session_id, profile_proposal)
        state1 = e2e.read_state(client, session_id)
        content = state1["profile"]["content"] or {}
        checks.append(check("replace_precondition_profile_saved",
                            confirmed_profile["terminal"]["final_status"] == "saved"
                            and (content.get("unavailable_equipment") or []) == []
                            and (content.get("forbidden_exercise_ids") or []) == [],
                            {"content": content}))

        plan_turn = e2e.run_turn(client, session_id, prompts[1])
        plan_proposal = e2e.find_proposal(plan_turn)
        evidence["plan_turn"] = plan_turn
        checks.append(check("replace_precondition_plan_proposed",
                            bool(plan_proposal) and plan_proposal["tool"] == "prepare_plan",
                            {"tool": (plan_proposal or {}).get("tool"),
                             "final_text": plan_turn.get("final_text")}))
        if plan_proposal is None:
            return checks, evidence
        confirmed_plan = e2e.confirm_proposal(client, session_id, plan_proposal)
        state2 = e2e.read_state(client, session_id)
        catalog = e2e.catalog_snapshot()
        saved_pairs = e2e.plan_exercise_ids((state2["current_plan"] or {}).get("content") or {"days": []})
        dumbbell_back = [
            (day, index, exercise_id) for day, index, exercise_id in saved_pairs
            if (e2e.exercise_facts(catalog, exercise_id) or {}).get("equipment") == "dumbbell"
            and (e2e.exercise_facts(catalog, exercise_id) or {}).get("body_part") == "back"
        ]
        checks.append(check("replace_precondition_saved_dumbbell_back_plan",
                            confirmed_plan["terminal"]["final_status"] == "saved"
                            and bool(dumbbell_back),
                            {"positions": dumbbell_back,
                             "saved_ids": [exercise_id for _, _, exercise_id in saved_pairs]}))
        evidence["precondition_plan_ids"] = [exercise_id for _, _, exercise_id in saved_pairs]

        replace_turn = e2e.run_turn(client, session_id, e2e.USER_INPUTS["replace"])
        proposal = e2e.find_proposal(replace_turn)
        evidence["turn"] = replace_turn
        checks.append(check("replace_adjustment_proposed",
                            bool(proposal) and proposal["tool"] == "prepare_plan_adjustment",
                            {"tool": (proposal or {}).get("tool"),
                             "turn_tools": [tool["name"] for tool in replace_turn["tools"]],
                             "final_text": replace_turn.get("final_text")}))
        if proposal is None:
            return checks, evidence
        after_prepare = e2e.read_state(client, session_id)
        checks.append(check("replace_prepare_no_commit",
                            e2e.state_digest(state2) == e2e.state_digest(after_prepare),
                            {"before": e2e.state_digest(state2),
                             "prepared": e2e.state_digest(after_prepare)}))
        searches = [tool for tool in replace_turn["tools"] if tool["name"] == "search_exercises"]
        replacement_notes = [note for tool in searches
                             if tool["arguments"].get("purpose") == "replacement"
                             for note in search_matches(tool)]
        # 本次不可用哑铃是否生效以服务端结果中的规范化条件说明为准：模型可传中文别名（哑铃→dumbbell）。
        checks.append(check("replace_temporary_dumbbell_effective",
                            "不可用器械含dumbbell" in replacement_notes,
                            {"notes": sorted(set(replacement_notes)),
                             "arguments": [tool["arguments"] for tool in searches]}))
        payload = ((proposal or {}).get("proposal") or {}).get("payload") or {}
        after_ids = e2e.plan_exercise_ids(payload) if payload else []
        replaced = []
        for day, index, exercise_id in dumbbell_back:
            match = next(((d, i, e) for d, i, e in after_ids if (d, i) == (day, index)), None)
            fact = e2e.exercise_facts(catalog, match[2]) if match else None
            replaced.append({"position": [day, index], "original": exercise_id,
                             "replacement": match[2] if match else None, "fact": fact})
        checks.append(check("replace_same_position_other_exercise",
                            bool(replaced) and all(
                                item["replacement"] and item["replacement"] != item["original"]
                                and item["fact"] for item in replaced),
                            {"replacements": replaced}))
        checks.append(check("replace_excludes_dumbbell",
                            all(item["fact"] and item["fact"]["equipment"] != "dumbbell"
                                for item in replaced),
                            {"equipments": [item["fact"]["equipment"] if item["fact"] else None
                                            for item in replaced]}))
        confirmed = e2e.confirm_proposal(client, session_id, proposal)
        after = e2e.read_state(client, session_id)
        checks.append(check("replace_confirmed_saved_new_version",
                            confirmed["terminal"]["final_status"] == "saved"
                            and after["plan_count"] == state2["plan_count"] + 1,
                            {"terminal": confirmed["terminal"]["final_status"],
                             "plan_count": after["plan_count"]}))
        before_ids = {record["id"] for record in state2["plans"]}
        after_ids_set = {record["id"] for record in after["plans"]}
        checks.append(check("replace_previous_plan_version_preserved",
                            before_ids <= after_ids_set,
                            {"before": sorted(before_ids), "after": sorted(after_ids_set)}))
        checks.append(check("replace_long_term_profile_unchanged",
                            after["profile"] == state2["profile"],
                            {"before": state2["profile"], "after": after["profile"]}))
        evidence["proposal"] = proposal
        evidence["confirmation"] = confirmed
    return checks, evidence


def confirm_path_mode(run: Path) -> tuple[list[dict], dict]:
    # 最小真实链路：内存配置驱动一次模型准备画像 -> 读取 pending -> 确认保存 -> 读回。
    checks: list[dict] = []
    landing = prepare_environment("confirm-path", None)
    from app.interfaces.http import active_secrets, app

    evidence: dict = {"isolation": landing["landing"], "model_config": landing["model_config"]}
    api_key = os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY")
    leaked = bool(api_key) and api_key in active_secrets()
    checks.append(check("live_credential_visible_to_active_secrets", leaked,
                        {"api_key_present": bool(api_key), "in_active_secrets": leaked}))
    with regression_support.Server(app) as server:
        client = e2e.open_client(server)
        session_id = e2e.session_setup(client)
        evidence["session_id"] = session_id
        before = e2e.read_state(client, session_id)

        turn = e2e.run_turn(client, session_id, e2e.precondition_prompts("generate")[0])
        proposal = e2e.find_proposal(turn)
        evidence["turn_tools"] = [tool["name"] for tool in turn["tools"]]
        checks.append(check("memory_config_drives_real_turn",
                            bool(proposal) and proposal["tool"] == "prepare_profile_update",
                            {"tool": (proposal or {}).get("tool"),
                             "final_text": turn.get("final_text")}))
        if proposal is None:
            return checks, evidence
        prepared = e2e.read_state(client, session_id)
        checks.append(check("prepare_stage_no_commit",
                            e2e.state_digest(before) == e2e.state_digest(prepared)
                            and prepared["profile"]["version"] is None,
                            {"version": prepared["profile"]["version"]}))
        confirmed = e2e.confirm_proposal(client, session_id, proposal)
        saved = e2e.read_state(client, session_id)
        checks.append(check("confirmation_saved_and_read_back",
                            confirmed["terminal"]["final_status"] == "saved"
                            and saved["profile"]["version"] == 1,
                            {"terminal": confirmed["terminal"]["final_status"],
                             "version": saved["profile"]["version"]}))
        checks.append(check("unobserved_delta_is_null",
                            confirmed.get("model_request_delta") is None
                            and confirmed.get("model_request_delta_note") == "未观测",
                            {"model_request_delta": confirmed.get("model_request_delta"),
                             "model_request_delta_note": confirmed.get("model_request_delta_note")}))
        evidence["confirmation"] = {
            "post_status_code": confirmed["post_status_code"],
            "model_request_delta": confirmed.get("model_request_delta"),
            "model_request_delta_note": confirmed.get("model_request_delta_note"),
            "pending": confirmed["pending"],
        }
    return checks, evidence


HANDLERS = {
    "selfcheck": selfcheck_mode,
    "eval-prep": eval_prep_mode,
    "search": search_mode,
    "generate": generation_mode,
    "replace": replacement_mode,
    "confirm-path": confirm_path_mode,
}


def evidence_scan(run: Path) -> dict:
    # 只输出命中路径与布尔结果，不输出任何密钥或端点取值。
    needles: dict[str, str] = {}
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            needles[name] = value
    base_url = os.environ.get("FIT_AGENT_TEST_MODEL_BASE_URL")
    if base_url:
        host = urlsplit(base_url).netloc
        if host:
            needles["model_endpoint_host"] = host
    files = [path for path in run.rglob("*") if path.is_file()]
    scan: dict = {}
    for label, value in needles.items():
        hits = [path.relative_to(run).as_posix() for path in files
                if value in path.read_text(encoding="utf-8", errors="ignore")]
        scan[label] = {"hit": bool(hits), "paths": hits}
    credential_files = [path.relative_to(run).as_posix() for path in files
                        if path.name in {"auth.json", "provider.journal"}]
    scan["credential_file_names"] = {"hit": bool(credential_files), "paths": credential_files}
    return scan


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="运动检索模块最小功能执行入口")
    parser.add_argument("mode", choices=sorted(HANDLERS))
    args = parser.parse_args(argv)
    started = now_iso()
    run = run_directory()
    checks: list[dict] = []
    extra: dict = {}
    fatal = None
    try:
        checks, extra = HANDLERS[args.mode](run)
    except BaseException as error:
        fatal = {"type": type(error).__name__, "message": str(error),
                 "traceback": traceback.format_exc(limit=12)}
        checks.append(check("fatal", False, fatal))
    finally:
        for restore in _RESTORERS:
            restore()
    credential_files = [path.relative_to(run).as_posix() for path in run.rglob("*")
                        if path.is_file() and path.name in {"auth.json", "provider.journal"}]
    checks.append(check("no_credential_files_in_run", not credential_files,
                        {"paths": credential_files}))
    failed = sorted({entry["name"] for entry in checks if not entry["passed"]})
    evidence = {
        "mode": args.mode, "run_id": run.name, "executor": EXECUTOR,
        "started_at": started, "finished_at": now_iso(),
        "identity": source_identity(), "checks": checks, "failed": failed, **extra,
    }
    if fatal is not None:
        evidence["fatal"] = fatal
    path = run / f"{args.mode}.json"
    e2e.write_json(path, evidence)
    evidence["credential_scan"] = evidence_scan(run)
    e2e.write_json(path, evidence)
    print(json.dumps({"mode": args.mode, "failed": failed, "run_dir": str(run)},
                     ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
