"""v2 全量量化评测单一入口。

从 backend 执行：
    .venv/Scripts/python -m test.run_v2_eval --experiment-id <id> --processes 2

编排：用例校验 → 冻结模型配置 → 写入 manifest → 派发 2 个隔离执行进程（各领独立槽位）
→ 汇总 → 运行工具调度与压缩专项 → 写出 summary/execution-ledger/comparison/budget-ledger。
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from test.v2_attempts import (
    attempt_summary,
    budget_path,
    create_attempt,
    execution_identity,
    read_record,
    reserve_experiment,
    timestamp,
    validate_unique_rounds,
    write_record,
)
from test.v2_budget import (
    DEFAULT_LIMITS,
    BudgetExhausted,
    BudgetLedger,
    LedgerAttributionUnresolved,
    StopTriggered,
)
from test.v2_contract import ROUNDS, ExperimentEnv, RoundObservation
from test.v2_eval_support import (
    BACKEND,
    DEFAULT_ACTIONS_PATH,
    DEFAULT_DATA_ROOT,
    _milvus_identity,
    aggregate,
    apply_v2_review,
    baseline_artifacts,
    baseline_identity,
    build_manifest,
    build_review_packet,
    compare_baseline,
    exception_type,
    execution_guards,
    experiment_env,
    freeze_model_config,
    install_embedding_recorder,
    load_cases,
    load_schedule,
    protected_baseline,
    run_case_round,
    sha256_file,
    slot_plan,
    trusted_business_date,
    use_frozen_model_config,
    validate_cases,
    write_stop_report,
)

REPO_ROOT = BACKEND.parent

# 普通运行错误缺少可靠现场类型时的固定类别：与已分类同权参与连续计数。
UNCLASSIFIED_RUN_ERROR = "unclassified_run_error"

# 异常退出子进程的安全收尾观察窗口：只等待在途 usage 结算与证据落盘，不新增强杀规则。
SHUTDOWN_OBSERVE_SECONDS = 30.0

# v1 原始值（quantitive-index/v1.md 原文），用于对照表；不重算历史证据。
V1_BASELINE = [
    ("每轮通过率-轮1", "66.0%", "33/50", "n=50"),
    ("每轮通过率-轮2", "66.0%", "33/50", "n=50"),
    ("每轮通过率-轮3", "74.0%", "37/50", "n=50"),
    ("场景执行通过率", "68.7%", "103/150", "n=150"),
    ("稳定通过率", "50.0%", "25/50", "n=50"),
    ("完整场景耗时 P50", "37.2s", "", "n=150（原计时边界含多回合）"),
    ("首文本事件耗时 P50", "3.12s", "", "n=282（原采集未校验非空正文）"),
    ("input tokens", "22,381,853", "", "282 runs"),
    ("output tokens", "491,007", "", "282 runs"),
    ("cache_read tokens", "5,667,264", "", "282 runs"),
    ("total tokens", "28,540,124", "", "282 runs（含一次摘要）"),
    ("缓存命中率（原公式）", "20.2%", "5,667,264 / 28,049,117", "原公式 cache_read/(input+cache_read)"),
    ("串行批次 P50", "182.77ms", "", "30 组配对"),
    ("并行批次 P50", "139.01ms", "", "30 组配对"),
    ("工具并行收益", "1.315x", "", "串行 P50 / 并行 P50"),
    ("摘要耗时 P50", "22.503s", "", "3 个压缩样本"),
    ("估算消息上下文缩减率", "70.59%", "", "3 个压缩样本"),
    ("关键事实保留", "摘要 15/15、回答 15/15", "", "分别统计"),
]

V1_RAW = {name: raw for name, raw, _sample, _note in V1_BASELINE}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Fit-Agent v2 业务量化评测单一入口")
    parser.add_argument("--cases", default=str(BACKEND / "test" / "v2_cases.json"))
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--experiment-root", default=str(REPO_ROOT / "tmp" / "v2-evaluation"))
    parser.add_argument("--model-source", default=None)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--process-index", type=int, default=None)
    parser.add_argument("--smoke-cases", default=None)
    parser.add_argument("--review", default=None)
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--benchmark-topic", default=None,
                        choices=("tool_parallel", "compaction", "save_confirmation"))
    parser.add_argument("--benchmarks-only", action="store_true")
    parser.add_argument("--check-release", action="store_true")
    parser.add_argument("--purpose", choices=("reproduction", "extra"))
    parser.add_argument("--reason")
    parser.add_argument("--case-id")
    parser.add_argument("--round-index", type=int, choices=range(ROUNDS))
    parser.add_argument("--parent-experiment-id")
    return parser.parse_args(argv)


def resolve_model_source(args) -> str:
    if args.model_source:
        return str(Path(args.model_source))
    override = os.environ.get("FIT_AGENT_V2_MODEL_SOURCE")
    if override:
        return str(Path(override))
    return str(DEFAULT_DATA_ROOT)


def new_experiment_id() -> str:
    return "v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def precheck(cases_path: Path, experiment_dir: Path, smoke_cases: str | None = None):
    cases = load_cases(cases_path)
    errors = validate_cases(cases)
    if errors:
        raise SystemExit("用例校验失败:\n" + "\n".join(errors))
    if smoke_cases:
        wanted = {item for item in smoke_cases.split(",") if item}
        missing = sorted(wanted - {case.case_id for case in cases})
        if missing:
            raise SystemExit(f"冒烟用例不存在：{missing}")
        cases = [case for case in cases if case.case_id in wanted]
    experiment_dir.mkdir(parents=True, exist_ok=True)
    probe = experiment_dir / ".write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return cases


def slot_pairs(cases, smoke: bool = False):
    return [(item["slot"], item["case_id"], item["round_index"]) for item in slot_plan(cases, smoke)]


def pairs_from_plan(plan, cases_by_id, processes: int = 1, process_index: int = 0):
    pairs = []
    for record in plan:
        slot = int(record["slot"])
        if slot % processes != process_index:
            continue
        case = cases_by_id.get(record["case_id"])
        if case is None:
            raise ValueError(f"槽位计划指向未知用例：{record['case_id']}")
        pairs.append((slot, case, int(record["round_index"])))
    return pairs


def manifest_slot_plan(experiment_dir: Path) -> list[dict]:
    plan = read_record(Path(experiment_dir) / "manifest.json").get("slot_plan")
    if not plan:
        raise ValueError("manifest 缺少冻结的 slot_plan")
    return plan


def claimed_pair(experiment_dir: Path, case_id: str, round_index: int) -> bool:
    # 已领取或已有结果的槽位保持原证据，重复领取一律跳过。
    claim = experiment_dir / "claims" / f"{case_id}-r{round_index}.json"
    return claim.exists()


def budget_pause_guard(ledger: BudgetLedger, schedule, slot, case, round_index, process_index):
    # 领取新槽位前读取共享账本：任一模型已结算 actual 达到上限即停止新增派发。
    paused = ledger.paused_models()
    if not paused:
        return None
    started_at = timestamp()
    detail = "; ".join(f"{item['model']} actual={item['actual']} limit={item['limit']}" for item in paused)
    _append_schedule(schedule, slot, case, round_index, process_index, started_at,
                     "blocked_by_embedding_budget", detail)
    print(f"[slot {slot} {case.case_id} r{round_index} p{process_index}] "
          f"Embedding 额度触达阈值，停止新增派发并保留证据：{detail}", flush=True)
    return paused


def run_worker(args, cases) -> int:
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    identity = use_frozen_model_config(experiment_dir)
    manifest = read_record(experiment_dir / "manifest.json")
    # 业务日期与时区由 manifest 单一取值下发，避免跨午夜偏移。
    business_date_value = manifest["business_date"]
    ledger = BudgetLedger(Path(manifest["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=manifest["experiment_id"])
    try:
        verify_frozen_identity(experiment_dir, args.cases, identity)
    except ValueError as error:
        # 冻结身份漂移：置位共享停止后向上传播，主控据异常退出与停止报告暴露原因。
        _trip_stop(ledger, experiment_dir, "frozen_identity_drift", experiment_id=args.experiment_id,
                   trigger=f"worker:p{args.process_index}", detail=str(error))
        raise
    install_embedding_recorder(
        ledger, experiment_id=args.experiment_id, process_index=args.process_index
    )
    logs = experiment_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    schedule = logs / f"schedule-p{args.process_index}.jsonl"
    cases_by_id = {case.case_id: case for case in cases}
    interrupted = 0
    for slot, case, round_index in pairs_from_plan(
        manifest_slot_plan(experiment_dir), cases_by_id, args.processes, args.process_index
    ):
        if _attribution_guard(ledger, experiment_dir, schedule, slot, case, round_index, args.process_index):
            return 3
        if budget_pause_guard(ledger, schedule, slot, case, round_index, args.process_index):
            _write_budget_pause(experiment_dir, ledger, args.process_index)
            return 3
        if _stop_guard(ledger, experiment_dir, schedule, slot, case, round_index, args.process_index):
            return 3
        if claimed_pair(experiment_dir, case.case_id, round_index):
            interrupted += 1
            with schedule.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "slot": slot, "case_id": case.case_id, "round_index": round_index,
                    "process_index": args.process_index, "status": "skipped_claimed",
                    "at": timestamp(),
                }, ensure_ascii=False) + "\n")
            continue
        env = experiment_env(
            args.experiment_id, case, round_index, slot, args.process_index,
            experiment_dir, identity, business_date_value,
        )
        started_at = timestamp()
        started = time.perf_counter()
        try:
            observation = run_case_round(case, round_index, slot, env, identity)
            # observation 返回路径沿用捕获现场保存的结构化异常类型；缺失时才回落到未分类值。
            status, error = observation.status, None
            error_type = observation.error_type
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # 取消、中断与进程退出保留原控制流语义：不记 run_error、不继续派发新槽位。
            raise
        except LedgerAttributionUnresolved as unresolved:
            _append_schedule(schedule, slot, case, round_index, args.process_index, started_at,
                             "blocked_by_ledger_attribution",
                             "; ".join(unresolved.attribution.get("problems") or []))
            print(f"[slot {slot} {case.case_id} r{round_index}] 账本归属未确认，停止派发", flush=True)
            return 3
        except StopTriggered as stopped:
            _append_schedule(schedule, slot, case, round_index, args.process_index,
                             started_at, "blocked_by_shared_stop", stopped.stop.get("reason_category"))
            write_stop_report(experiment_dir, stopped.stop)
            print(f"[slot {slot} {case.case_id} r{round_index}] 共享停止已置位，停止派发", flush=True)
            return 3
        except BudgetExhausted as exhausted:
            _append_schedule(schedule, slot, case, round_index, args.process_index,
                             started_at, "blocked_by_embedding_budget", str(exhausted))
            _write_budget_pause(experiment_dir, ledger, args.process_index)
            print(f"[slot {slot} {case.case_id} r{round_index}] Embedding 额度已用尽，停止派发", flush=True)
            return 3
        except BaseException as failure:
            status = "run_error"
            failure_type = exception_type(failure)
            error = f"{failure_type}: {failure}"
            _append_schedule(schedule, slot, case, round_index, args.process_index,
                             started_at, status, error, error_type=failure_type)
            print(f"[slot {slot} {case.case_id} r{round_index} p{args.process_index}] {status}: {error}", flush=True)
            if _record_run_outcome(ledger, experiment_dir, args.experiment_id, status, error,
                                   execution_unit=_unit_identity("slot", slot=slot),
                                   error_type=failure_type, slot=slot,
                                   process_index=args.process_index):
                return 3
            continue
        _append_schedule(
            schedule, slot, case, round_index, args.process_index, started_at, status, None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        print(f"[slot {slot} {case.case_id} r{round_index} p{args.process_index}] {status}", flush=True)
        _record_run_outcome(ledger, experiment_dir, args.experiment_id, status, error,
                            execution_unit=_unit_identity("slot", slot=slot),
                            error_type=error_type, slot=slot,
                            process_index=args.process_index)
    snapshot = ledger.snapshot()
    with (logs / f"budget-p{args.process_index}.json").open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    if interrupted:
        print(f"跳过已领取槽位 {interrupted} 个", flush=True)
    return 0


def _write_budget_pause(experiment_dir: Path, ledger: BudgetLedger, process_index: int) -> None:
    # 暂停事件保留触发身份与累计量，供主控通知与放行判定读取。
    paused = ledger.paused_models()
    if not paused:
        return
    target = experiment_dir / "logs" / f"budget-pause-p{process_index}.json"
    target.write_text(
        json.dumps({"paused": paused, "at": timestamp(), "process_index": process_index},
                   ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8",
    )


def _append_schedule(path, slot, case, round_index, process_index, started_at, status, error, **extra):
    record = {
        "slot": slot,
        "case_id": case.case_id,
        "category": case.category,
        "round_index": round_index,
        "process_index": process_index,
        "pid": os.getpid(),
        "started_at": started_at,
        "ended_at": timestamp(),
        "status": status,
    }
    if error:
        record["error"] = error
    record.update(extra)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def spawn_processes(args, cases_path: Path, model_source: str) -> None:
    directory = Path(args.experiment_root) / args.experiment_id
    ledger = BudgetLedger(Path(read_record(directory / "manifest.json")["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=args.experiment_id)
    environment = dict(os.environ)
    environment["FIT_AGENT_V2_MODEL_SOURCE"] = model_source
    processes = []
    for index in range(args.processes):
        command = [
            sys.executable, "-m", "test.run_v2_eval",
            "--cases", str(cases_path),
            "--experiment-root", args.experiment_root,
            "--experiment-id", args.experiment_id,
            "--model-source", model_source,
            "--processes", str(args.processes),
            "--process-index", str(index),
        ]
        if args.smoke_cases:
            command.extend(["--smoke-cases", args.smoke_cases])
        processes.append(subprocess.Popen(command, cwd=str(BACKEND), env=environment))
    # 并发观察两个子进程：任一停止或异常退出即置位共享停止，其余进程按既有门禁自行拒绝新领取并收尾。
    failures: list[str] = []
    pending = set(range(len(processes)))
    while pending:
        progressed = False
        for index in sorted(pending):
            code = processes[index].poll()
            if code is None:
                continue
            pending.discard(index)
            progressed = True
            if code in (0, 3):
                stop = ledger.stop_info()
                if stop is not None:
                    write_stop_report(directory, stop)
                    print(f"[主控] 子进程 {index} 停止：{stop.get('reason_category')} "
                          f"trigger={stop.get('trigger')} detail={stop.get('detail')}", flush=True)
                continue
            # 异常退出：置位共享停止并通过既有取消机制让其余进程安全收尾；结果未知一律保留。
            print(f"[主控] 子进程 {index} 异常退出码 {code}，置位共享停止", flush=True)
            failures.append(f"进程 {index} 退出码 {code}")
            # 异常子进程退出：置位实验级 dispatch_halt，与八类事故统计和连续错误计数相互独立。
            halt = ledger.halt_dispatch(detail={"child_index": index, "exit_code": code,
                                                "reason": "child_process_abnormal_exit",
                                                "at": timestamp()})
            write_stop_report(directory, halt)
            print(f"[主控] 已置位实验级 dispatch_halt：{halt.get('detail')}", flush=True)
            break
        if pending and not progressed:
            time.sleep(0.05)
    for index in sorted(pending):
        processes[index].wait(timeout=None)
    for process in processes:
        if process.poll() is None:
            process.wait()
    if failures:
        raise RuntimeError("执行进程失败：" + "; ".join(failures))


def load_round_evidence(experiment_dir: Path) -> list[RoundObservation]:
    rounds = []
    paths = sorted((experiment_dir / "cases").glob("*/round-*/round.json"))
    for path in paths:
        rounds.append(RoundObservation.model_validate(read_record(path)))
    validate_unique_rounds(rounds)
    for path, record in zip(paths, rounds):
        if path.parent.name != f"round-{record.round_index}" or path.parent.parent.name != record.case_id:
            raise ValueError(f"正式槽位路径与结果不匹配：{path}")
    return rounds


def benchmark_topics() -> tuple[tuple[str, str], ...]:
    # (topic, 目录名)：正式入口每项只执行一次，子进程内各自初始化隔离资源。
    return (
        ("tool_parallel", "tool-parallel"),
        ("compaction", "compaction"),
        ("save_confirmation", "save-confirmation"),
    )


def run_benchmark_worker(args, topic: str) -> int:
    # 专项在独立子进程初始化：先显式注入冻结模型配置、隔离业务库/Trace/附件/临时根，再运行真实测量逻辑。
    from app import model_config
    from app.agent import telemetry
    from app.application.session import service as session_service
    from app.infrastructure.persistence.sqlite import database as database_module
    from test import (
        check_compaction_performance,
        check_save_confirmation_benchmark,
        check_tool_parallel_performance,
    )

    names = dict(benchmark_topics())
    if topic not in names:
        raise ValueError(f"未知专项主题：{topic}")
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    identity = use_frozen_model_config(experiment_dir)
    manifest = read_record(experiment_dir / "manifest.json")
    ledger = BudgetLedger(Path(manifest["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=manifest["experiment_id"])
    try:
        verify_frozen_identity(experiment_dir, args.cases, identity)
    except ValueError as error:
        _trip_stop(ledger, experiment_dir, "frozen_identity_drift", experiment_id=args.experiment_id,
                   trigger=f"benchmark:{topic}", detail=str(error))
        raise
    install_embedding_recorder(ledger, experiment_id=args.experiment_id, process_index=-1)
    paused = ledger.paused_models()
    if paused:
        print(f"Embedding 额度触达阈值，专项 {topic} 停止运行：{paused}", flush=True)
        return 3
    stop = ledger.stop_info()
    if stop is not None:
        write_stop_report(experiment_dir, stop)
        print(f"共享停止已置位（{stop.get('reason_category')}），专项 {topic} 停止运行", flush=True)
        return 3
    directory = experiment_dir / "benchmarks" / names[topic]
    directory.mkdir(parents=True, exist_ok=True)
    temp_root = directory / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    database_dir = directory / "dbs"
    database_dir.mkdir(parents=True, exist_ok=True)
    attachments = directory / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    # 隔离根显式注入：不触碰生产 backend/data 与系统临时目录。
    database_module.default_database_path = lambda: database_dir / "benchmark.db"
    telemetry.trace_database_path = lambda: directory / "trace.db"
    session_service.PROJECT_ROOT = attachments
    (directory / "init.json").write_text(
        json.dumps(
            {
                "topic": topic,
                "experiment_id": args.experiment_id,
                "model_identity": identity,
                "model_config_root": str(model_config.DATA_ROOT),
                "zero_provider_requests": topic == "tool_parallel",
                "roots": {
                    "benchmark_dir": str(directory),
                    "business_database": str(database_module.default_database_path()),
                    "trace_database": str(telemetry.trace_database_path()),
                    "attachment_root": str(session_service.PROJECT_ROOT),
                    "temp_root": str(temp_root),
                },
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # 专项执行入口持有完整包装生命周期：初始化、真实调用、异常/取消/停止后的在途收尾
    # 全部落在 execution_guards 有效范围内；本进程是该 topic 的唯一终态结算所有者。
    execution_unit = _unit_identity("topic", topic=topic)
    outcome = {"status": "pass", "error": None, "failure_type": None}
    with execution_guards(ledger, directory / "evidence", experiment_id=args.experiment_id,
                          execution_unit=execution_unit):
        try:
            if topic == "tool_parallel":
                result = check_tool_parallel_performance.run_benchmark(str(directory), tmp_root=temp_root)
            elif topic == "compaction":
                result = check_compaction_performance.run_benchmark(
                    str(directory),
                    model_root=str(experiment_dir / "model-config"),
                    database_dir=str(database_dir),
                )
            else:
                result = check_save_confirmation_benchmark.run_benchmark(str(directory))
            if result.topic != topic:
                raise ValueError(f"专项 {names[topic]} 返回主题 {result.topic} != {topic}")
            (directory / "benchmark.json").write_text(
                json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False, indent=2),
                encoding="utf-8",
            )
            # 专项业务不通过：完整跑完但结果非 complete，按业务不通过结算（重置连续计数）。
            if result.status != "complete":
                outcome["status"] = "business_failure"
                outcome["error"] = f"专项 {topic} 结果状态 {result.status}"
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            # 取消与中断保留原控制流语义，同时按取消结算：不计入连续错误、不置位停止。
            outcome["status"] = "cancelled"
            _settle_benchmark(ledger, experiment_dir, args.experiment_id, topic, execution_unit, outcome)
            raise
        except (StopTriggered, BudgetExhausted) as blocked:
            outcome["status"] = ("budget_paused" if isinstance(blocked, BudgetExhausted)
                                 else "expected_failure")
            outcome["error"] = f"{exception_type(blocked)}: {blocked}"
            if isinstance(blocked, BudgetExhausted):
                _write_budget_pause(experiment_dir, ledger, -1)
            else:
                write_stop_report(experiment_dir, blocked.stop)
            return 3
        except BaseException as failure:
            outcome["status"] = "run_error"
            outcome["failure_type"] = exception_type(failure)
            outcome["error"] = f"{outcome['failure_type']}: {failure}"
        _settle_benchmark(ledger, experiment_dir, args.experiment_id, topic, execution_unit, outcome)
    if outcome["status"] == "run_error":
        raise RuntimeError(outcome["error"])
    return 0


def _settle_benchmark(ledger, experiment_dir, experiment_id, topic, execution_unit, outcome) -> dict | None:
    # 专项 topic 的唯一终态结算点：成功/业务不通过重置计数，运行错误按结构化类型累积，
    # 取消与预算暂停排除且不重置。同一 execution_unit 重复上报在账本内保持幂等。
    status = outcome["status"]
    if status in ("pass", "business_failure"):
        record_outcome = "success" if status == "pass" else "business_failure"
        ledger.record_outcome(record_outcome, execution_unit=execution_unit, owner=experiment_id,
                              trigger=f"benchmark:{topic}",
                              evidence_refs=[f"benchmarks/{topic}"])
        return None
    if status == "run_error":
        result = ledger.record_outcome("run_error", execution_unit=execution_unit,
                                       error_type=outcome["failure_type"], owner=experiment_id,
                                       trigger=f"benchmark:{topic}",
                                       evidence_refs=[f"benchmarks/{topic}"],
                                       detail={"error": outcome["error"]})
        if result["triggered"]:
            write_stop_report(experiment_dir, result["stop"])
            print(f"[stop] 专项 {topic} 同类别连续 {result['consecutive']['count']} 次运行错误，停止新增派发", flush=True)
            return result["stop"]
        return None
    ledger.record_outcome(status, execution_unit=execution_unit, owner=experiment_id,
                          trigger=f"benchmark:{topic}", evidence_refs=[f"benchmarks/{topic}"],
                          detail={"error": outcome["error"]})
    return None


def _milvus_guard(ledger, experiment_dir, experiment_id) -> bool:
    # Milvus 实际错误：真实只读探针返回错误即置位共享停止；不触碰生产集合/别名/manifest。
    try:
        identity = _milvus_identity()
    except BaseException as error:
        identity = {"error": f"{type(error).__name__}: {error}"}
    if identity.get("error"):
        _trip_stop(ledger, experiment_dir, "milvus_unavailable", experiment_id=experiment_id,
                   trigger="run_benchmarks:milvus_probe", detail=identity["error"])
        return True
    return False


def run_benchmarks(experiment_dir: Path) -> dict:
    # 每个专项在独立子进程初始化：正式入口保持单次编排，每项只执行一次。
    # 唯一终态结算所有者为持有 execution_guards 生命周期的专项子进程：主控只负责
    # 派发门禁、退出码归档与已跳过 topic 的登记，不对已执行 topic 重复结算。
    experiment_dir = Path(experiment_dir)
    manifest = read_record(experiment_dir / "manifest.json")
    ledger = BudgetLedger(Path(manifest["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=manifest["experiment_id"])
    results: dict = {}
    settled: list[str] = []
    for topic, name in benchmark_topics():
        paused = ledger.paused_models()
        if paused:
            # 专项同样受额度暂停门禁约束：停止新增运行并保留已产出证据。
            print(f"Embedding 额度触达阈值，专项 {name} 及其后专项停止运行：{paused}", flush=True)
            results["budget_paused"] = {"before_topic": topic, "paused": paused}
            break
        stop = ledger.stop_info()
        if stop is not None:
            write_stop_report(experiment_dir, stop)
            print(f"共享停止已置位（{stop.get('reason_category')}），专项 {name} 及其后专项停止运行", flush=True)
            results["shared_stop"] = {"before_topic": topic, "stop": stop}
            break
        if _milvus_guard(ledger, experiment_dir, experiment_dir.name):
            results["shared_stop"] = {"before_topic": topic, "stop": ledger.stop_info()}
            break
        directory = experiment_dir / "benchmarks" / name
        directory.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["FIT_AGENT_V2_MODEL_SOURCE"] = str(experiment_dir / "model-config")
        completed = subprocess.run(
            [
                sys.executable, "-m", "test.run_v2_eval",
                "--cases", str(manifest["cases_path"]),
                "--experiment-root", str(experiment_dir.parent),
                "--experiment-id", experiment_dir.name,
                "--benchmark-topic", topic,
            ],
            cwd=str(BACKEND),
            env=environment,
        )
        if completed.returncode == 3:
            # 子进程已按预算暂停或共享停止自行结算终态；主控只归档退出原因。
            settled.append(topic)
            results["budget_paused"] = {"before_topic": topic, "paused": ledger.paused_models()}
            break
        if completed.returncode != 0:
            # 子进程退出前已按 run_error 结算；主控不重复结算，只传播失败。
            settled.append(topic)
            results["failed_topic"] = {"topic": topic, "exit_code": completed.returncode}
            raise RuntimeError(f"专项 {name} 子进程退出码 {completed.returncode}")
        settled.append(topic)
        results[topic] = read_record(directory / "benchmark.json")
    results["settled_topics"] = settled
    return results


LEDGER_STATUSES = (
    "pass", "fail", "pending_review", "run_error", "precondition_failed", "blocked",
    "cancelled", "insufficient_evidence", "blocked_by_embedding_budget",
    "blocked_by_shared_stop", "blocked_by_ledger_attribution", "claimed_interrupted",
    "not_executed",
)


def _trip_stop(ledger, experiment_dir, category, *, experiment_id, trigger, slot=None,
               operation_id=None, evidence_refs=None, detail=None) -> dict:
    # 真实触发源就地置位共享停止，并写持久化停止报告与主控输出；停止保持置位。
    stop = ledger.request_stop(
        category, owner=experiment_id, trigger=trigger, slot=slot,
        operation_id=operation_id, evidence_refs=evidence_refs, detail=detail,
    )
    write_stop_report(experiment_dir, stop)
    print(f"[stop] {category} trigger={trigger} owner={experiment_id} at={stop.get('at')} "
          f"detail={stop.get('detail')}", flush=True)
    return stop


def _attribution_guard(ledger, experiment_dir, schedule, slot, case, round_index, process_index) -> dict | None:
    # 旧账本归属未确认、或已确认但同锁迁移尚未落实：一律拒绝新增领取与执行。
    # 两种情况都保留原始状态与归属证据供监督裁定，禁止在迁移前放行。
    attribution = ledger.attribution()
    status = attribution["status"]
    if status not in ("unconfirmed", "confirmed"):
        return None
    detail = "; ".join(attribution.get("problems") or []) or (
        "归属已确认但迁移未落实，执行保持阻塞" if status == "confirmed" else ""
    )
    _append_schedule(schedule, slot, case, round_index, process_index, timestamp(),
                     "blocked_by_ledger_attribution",
                     f"attribution_status={status}; {detail}")
    target = (experiment_dir / "ledger-attribution-migration-pending.json" if status == "confirmed"
              else experiment_dir / "ledger-attribution-unresolved.json")
    target.write_text(
        json.dumps({"attribution": attribution, "at": timestamp(),
                    "experiment_id": ledger.experiment_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[slot {slot} {case.case_id} r{round_index} p{process_index}] "
          f"账本归属状态 {status}，拒绝新增领取：{detail}", flush=True)
    return attribution


def _stop_guard(ledger, experiment_dir, schedule, slot, case, round_index, process_index) -> dict | None:
    # 领取新槽前读取共享停止：任一进程置位即停止新增派发并保留证据。
    stop = ledger.stop_info()
    if not stop:
        return None
    write_stop_report(experiment_dir, stop)
    _append_schedule(schedule, slot, case, round_index, process_index, timestamp(),
                     "blocked_by_shared_stop", stop.get("reason_category"))
    print(f"[slot {slot} {case.case_id} r{round_index} p{process_index}] "
          f"共享停止已置位（{stop.get('reason_category')}），停止新增派发：{stop.get('trigger')}", flush=True)
    return stop


def _unit_identity(kind: str, *, slot=None, topic=None, attempt_id=None) -> str:
    # 稳定执行单元身份：实验作用域 + 入口种类 + slot/topic/attempt 身份。
    if kind == "slot":
        return f"slot:{slot}"
    if kind == "topic":
        return f"topic:{topic}"
    if kind == "attempt":
        return f"attempt:{attempt_id}"
    raise ValueError(f"未登记的执行单元种类：{kind}")


def _record_run_outcome(ledger, experiment_dir, experiment_id, status, error, *, execution_unit,
                        error_type=None, slot=None, process_index=None, trigger=None,
                        evidence_refs=None) -> dict | None:
    # 完成时刻在锁内全局串行结算：成功与业务不通过重置计数；运行错误按结构化异常类型累积。
    # 异常类型取自捕获现场；缺少可靠类型时固定为 unclassified_run_error，原文单独留证。
    trigger = trigger or f"worker:p{process_index}"
    if status in {"pass", "fail", "pending_review"}:
        ledger.record_outcome("success" if status == "pass" else "business_failure",
                              execution_unit=execution_unit, owner=experiment_id, trigger=trigger,
                              slot=slot, evidence_refs=evidence_refs)
        return None
    category = error_type or UNCLASSIFIED_RUN_ERROR
    result = ledger.record_outcome("run_error", execution_unit=execution_unit, error_type=category,
                                   owner=experiment_id, trigger=trigger, slot=slot,
                                   evidence_refs=evidence_refs,
                                   detail={"error": error} if error else None)
    if result["triggered"]:
        write_stop_report(experiment_dir, result["stop"])
        print(f"[stop] 同类别连续 {result['consecutive']['count']} 槽运行错误（{category}），停止新增派发", flush=True)
        return result["stop"]
    return None


def _slot_reason(experiment_dir: Path, case_id: str, round_index: int, status: str) -> str | None:
    directory = experiment_dir / "cases" / case_id / f"round-{round_index}"
    error_path = directory / "error.json"
    if status == "precondition_failed" and error_path.exists():
        return read_record(error_path).get("error")
    if status == "run_error" and error_path.exists():
        return read_record(error_path).get("error")
    if status == "claimed_interrupted":
        claim = read_record(experiment_dir / "claims" / f"{case_id}-r{round_index}.json")
        return f"已领取未完成：claim={claim.get('status')}"
    if status in {"pass", "fail", "pending_review"}:
        return None
    return None


def _slot_status(experiment_dir: Path, case_id: str, round_index: int, record, schedule_entry) -> tuple[str, str | None]:
    if record is not None:
        if record.status == "error":
            # 前置构造失败与运行期失败分别登记，便于按原因统计。
            directory = experiment_dir / "cases" / case_id / f"round-{round_index}"
            kind = "precondition_failed" if (directory / "precondition.json").exists() is False else "run_error"
            return kind, _slot_reason(experiment_dir, case_id, round_index, kind)
        return record.status, None
    if schedule_entry is not None and schedule_entry.get("status"):
        status = schedule_entry["status"]
        if status == "skipped_claimed":
            status = "claimed_interrupted"
        return status, schedule_entry.get("error") or _slot_reason(
            experiment_dir, case_id, round_index, status
        )
    claim = experiment_dir / "claims" / f"{case_id}-r{round_index}.json"
    if claim.exists():
        return "claimed_interrupted", _slot_reason(experiment_dir, case_id, round_index, "claimed_interrupted")
    return "not_executed", None


def build_execution_ledger(plan, rounds: list[RoundObservation], experiment_dir=None) -> dict:
    validate_unique_rounds(rounds)
    observed = {(r.case_id, r.round_index): r for r in rounds}
    schedule = load_schedule(experiment_dir) if experiment_dir else {}
    slots = []
    statuses: dict[str, int] = {}
    for record in plan:
        case_id, round_index = record["case_id"], int(record["round_index"])
        slot = int(record["slot"])
        entry = schedule.get((case_id, round_index))
        status, reason = _slot_status(experiment_dir, case_id, round_index, observed.get((case_id, round_index)), entry)
        statuses[status] = statuses.get(status, 0) + 1
        slots.append(
            {
                "slot": slot,
                "case_id": case_id,
                "category": record["category"],
                "round_index": round_index,
                "status": status,
                "reason": reason,
                "process_index": (
                    observed[(case_id, round_index)].process_index
                    if (case_id, round_index) in observed else (entry or {}).get("process_index")
                ),
                "evidence_dir": (
                    str(Path(experiment_dir) / "cases" / case_id / f"round-{round_index}")
                    if experiment_dir else None
                ),
            }
        )
    for status in statuses:
        if status not in LEDGER_STATUSES:
            raise ValueError(f"未登记的执行账本状态：{status}")
    return {
        "planned_slots": len(plan),
        "executed_slots": len(rounds),
        "status_counts": statuses,
        "slots": slots,
    }


V1_RECAL_CANDIDATES = (
    REPO_ROOT / "tmp" / "v2-evaluation" / "v1-recalc" / "v1-recalc.json",
    BACKEND / "tmp" / "v2-evaluation" / "v1-recalc" / "v1-recalc.json",
)


def load_v1_recalc() -> dict | None:
    for path in V1_RECAL_CANDIDATES:
        if path.exists():
            return read_record(path)
    return None


def _change(v1, v2, *, percent=False, lower_is_better=None):
    """返回 (变化量, 相对变化, 判定说明)。"""
    if v1 is None or v2 is None:
        return "不适用", "不适用", "任一侧缺值"
    scale = 100.0 if percent else 1.0
    unit = "个百分点" if percent else ""
    delta = (v2 - v1) * scale
    if v1 == v2:
        return f"{delta:+.4g}{unit}", "持平", "相同值"
    if v1 == 0:
        return f"{delta:+.4g}{unit}", "不适用", "基线为零"
    relative = (v2 - v1) / v1 * 100.0
    direction = ""
    if lower_is_better is not None:
        direction = "（改善）" if (delta < 0) == lower_is_better else "（退化）"
    return f"{delta:+.4g}{unit}", f"{relative:+.2f}%{direction}", "可比"


def _comparability_level(value) -> tuple[str, str | None]:
    # 可比性来源为 {level, reason} 结构；按实际 schema 读取，禁止把对象与字符串直接比较。
    if isinstance(value, dict):
        return value.get("level", "comparable"), value.get("reason")
    if isinstance(value, str):
        return value, None
    return "comparable", None


def _row(name, v1_raw, v1_value, v2_value, *, samples, comparability, evidence, percent=False, lower=None):
    level, reason = _comparability_level(comparability)
    change, relative, note = _change(v1_value, v2_value, percent=percent, lower_is_better=lower)
    if level != "comparable":
        change = relative = "不适用"
        note = f"{level}：{reason}" if reason else level
    fmt = (lambda v: "null" if v is None else f"{v * 100:.1f}%") if percent else (
        lambda v: "null" if v is None else f"{v:.4g}"
    )
    return [
        name,
        v1_raw,
        fmt(v1_value) if v1_value is not None else "不可重算",
        fmt(v2_value),
        change,
        relative,
        f"{samples}；可比性={note}",
        evidence,
    ]


def build_comparison(experiment: dict, summary: dict, recalc: dict | None = None) -> list[list[str]]:
    agg = summary["aggregate"]
    durations = agg["durations"]
    usage = agg["usage"]
    per_round = agg["per_round"]
    tool = experiment.get("tool_parallel", {}).get("stats", {}) or {}
    comp = experiment.get("compaction", {}).get("stats", {}) or {}
    retention = experiment.get("compaction", {}).get("correctness", {}) or {}

    rates = recalc["pass_rates"] if recalc else None
    tokens = recalc["token_totals"]["usage_sums"] if recalc else {}
    cache = recalc["cache_hit_rate"] if recalc else {}
    span = recalc["duration_stats"] if recalc else {}
    bench = recalc["benchmarks"] if recalc else {}
    comparable = (recalc or {}).get("comparability", {}).get("per_metric", {})

    def kind(key, fallback="comparable"):
        return comparable.get(key, fallback) if comparable else fallback

    def value(entry, key="value"):
        if not isinstance(entry, dict):
            return None
        return entry.get(key)

    rows = [
        ["指标", "v1 原始值", "v1 同口径重算值", "v2 实测值", "变化量", "相对变化", "两侧样本数与可比性", "证据位置"]
    ]
    for index, label in enumerate(("每轮通过率-轮1", "每轮通过率-轮2", "每轮通过率-轮3")):
        v1 = value((rates or {}).get("per_round_pass_rate", [])[index] if rates else None, "rate")
        v2 = per_round[index]["rate"] if len(per_round) > index else None
        rows.append(_row(
            label, dict(V1_RAW).get(label, ""), v1, v2,
            samples="v1 n=50、v2 n=%d" % (per_round[index]["observed"] if len(per_round) > index else 0),
            comparability=kind("per_round_pass_rate"),
            evidence="v1.md / v1-recalc.json pass_rates / summary.json aggregate.per_round",
            percent=True, lower=False,
        ))
    rows.append(_row(
        "场景执行通过率", "68.7%", value((rates or {}).get("execution_pass_rate"), "rate"),
        agg["execution_pass_rate"], samples="v1 103/150、v2 %d/%d" % (agg["passed_executions"], agg["executions"]),
        comparability=kind("execution_pass_rate"),
        evidence="v1.md / v1-recalc.json / summary.json aggregate", percent=True, lower=False,
    ))
    rows.append(_row(
        "稳定通过率", "50.0%", value((rates or {}).get("stable_pass_rate"), "rate"),
        agg["stable_pass_rate"], samples="v1 25/50、v2 %d/%d" % (agg["stable_cases"], agg["independent_cases"]),
        comparability=kind("stable_pass_rate"),
        evidence="v1.md / v1-recalc.json / summary.json aggregate", percent=True, lower=False,
    ))
    rows.append(_row(
        "完整场景耗时 P50", "37.2s", value((span.get("round_ms") or {}), "p50"),
        durations["round_ms"]["p50"],
        samples="v1 n=%s、v2 n=%s" % ((span.get("round_ms") or {}).get("samples"), durations["round_ms"]["samples"]),
        comparability=kind("round_ms", "protocol_changed"),
        evidence="v1-recalc.json duration_stats（v1 含启动与前置构造）/ summary.json aggregate.durations",
        lower=True,
    ))
    rows.append(_row(
        "首个非空正文延迟 P50", "3.12s（首个文本事件，正文为空）",
        None, durations["client_first_body_ms"]["p50"],
        samples="v1 缺测、v2 n=%s" % durations["client_first_body_ms"]["samples"],
        comparability="not_recalculable",
        evidence="v1-recalc.json missing_collection.first_non_empty_body_reconstruction / summary.json",
        lower=True,
    ))
    for key, label, raw in (
        ("input", "input tokens", "22,381,853"),
        ("output", "output tokens", "491,007"),
        ("cache_read", "cache_read tokens", "5,667,264"),
        ("cache_write", "cache_write tokens", "未上报"),
        ("total_tokens", "total tokens", "28,540,124"),
    ):
        rows.append(_row(
            label, raw, tokens.get(key), usage[key],
            samples="v1 282 runs、v2 %d runs" % usage["runs"],
            comparability=kind("token_totals"),
            evidence="v1-recalc.json token_totals / summary.json aggregate.usage",
            lower=True,
        ))
    rows.append(_row(
        "缓存命中率（新公式）", "20.2%（原公式 cache_read/(input+cache_read)）",
        value(cache.get("cache_rate_new")), usage["cache_hit_rate"],
        samples="v1 有效样本=%s、v2 共同样本=%d" % (
            (cache.get("cache_rate_new") or {}).get("valid_sample_count"), usage["cache_samples"]),
        comparability=kind("cache_rate_new", "not_recalculable"),
        evidence="v1-recalc.json cache_hit_rate / summary.json aggregate.usage",
        percent=True, lower=False,
    ))
    tp_v1 = (bench.get("tool_parallel") or {})
    rows.append(_row(
        "串行批次 P50", "182.77ms", value((tp_v1.get("serial_batch_ms") or {}), "p50"),
        value((tool.get("serial", {}).get("batch") or {}), "p50"),
        samples="v1 30 组、v2 %s" % (tool.get("serial", {}).get("batch") or {}).get("samples"),
        comparability=kind("tool_parallel_benchmark"),
        evidence="v1-recalc.json benchmarks.tool_parallel / benchmarks/tool-parallel/benchmark.json",
        lower=True,
    ))
    rows.append(_row(
        "并行批次 P50", "139.01ms", value((tp_v1.get("parallel_batch_ms") or {}), "p50"),
        value((tool.get("parallel", {}).get("batch") or {}), "p50"),
        samples="v1 30 组、v2 %s" % (tool.get("parallel", {}).get("batch") or {}).get("samples"),
        comparability=kind("tool_parallel_benchmark"),
        evidence="v1-recalc.json benchmarks.tool_parallel / benchmarks/tool-parallel/benchmark.json",
        lower=True,
    ))
    rows.append(_row(
        "工具并行收益", "1.315x", tp_v1.get("serial_over_parallel_batch_p50"),
        (tool.get("serial_over_parallel") or {}).get("batch_p50"),
        samples="v1 30 组配对、v2 %s" % (tool.get("pairs") if tool else "n/a"),
        comparability=kind("tool_parallel_benchmark"),
        evidence="v1-recalc.json / benchmarks/tool-parallel/benchmark.json", lower=False,
    ))
    cmp_v1 = bench.get("compaction") or {}
    rows.append(_row(
        "摘要耗时 P50", "22.503s", value((cmp_v1.get("summary_duration_ms") or {}), "p50"),
        comp.get("summary_ms", {}).get("median"),
        samples="v1 3 样本、v2 %s" % comp.get("summary_ms", {}).get("samples"),
        comparability=kind("compaction_benchmark"),
        evidence="v1-recalc.json benchmarks.compaction / benchmarks/compaction/benchmark.json",
        lower=True,
    ))
    rows.append(_row(
        "估算消息上下文缩减率", "70.59%", value((cmp_v1.get("context_reduction_ratio") or {}), "p50"),
        comp.get("context_reduction_ratio", {}).get("median"),
        samples="v1 3 样本、v2 %s" % comp.get("context_reduction_ratio", {}).get("samples"),
        comparability=kind("compaction_benchmark"),
        evidence="v1-recalc.json / benchmarks/compaction/benchmark.json", percent=True, lower=False,
    ))
    v2_retention = "摘要 %s/%s、回答 %s/%s" % (
        retention.get("summary_facts_passed"), retention.get("denominator"),
        retention.get("answer_facts_passed"), retention.get("denominator"),
    )
    rows.append([
        "关键事实保留", "摘要 15/15、回答 15/15",
        json.dumps((cmp_v1.get("fact_retention") or {}), ensure_ascii=False)[:80] if cmp_v1 else "不可重算",
        v2_retention, "不适用", "不适用",
        "v1 3 样本 15/15、v2 %s 项；可比性=%s" % (retention.get("denominator"), kind("compaction_benchmark")),
        "v1-recalc.json / benchmarks/compaction/benchmark.json",
    ])
    return rows


REVIEW_STATUSES = frozenset({"pass", "fail"})


def _evidence_ref_invalid(experiment_dir: Path, ref) -> bool:
    # 只有可解析为实验内文件的引用参与存在性校验；步骤标记与数据库标识等非路径引用保留。
    if not isinstance(ref, str) or not ref.strip():
        return True
    if ref.startswith("cases/") or ref.startswith("reviews/") or ref.endswith(".json"):
        return not (experiment_dir / ref).exists()
    return False


def _validate_review_payload(experiment_dir: Path, manifest, rounds, payload) -> tuple[list[str], list[str]]:
    # 审阅输入身份校验（放行与审阅应用共用同一套规则，避免两处分叉）：
    # 实验身份、包哈希、检查集合、评审者与证据引用逐项核对，重复检查与无效引用一并拦下。
    blockers: list[str] = []
    notes: list[str] = []
    if not isinstance(payload, dict):
        return ["审阅结论结构非法：缺少实验身份与包哈希"], notes
    if payload.get("experiment_id") != manifest["experiment_id"]:
        return [
            f"审阅结论指向其他实验：{payload.get('experiment_id')} != {manifest['experiment_id']}"
        ], notes
    cases_path = manifest.get("cases_path")
    if not cases_path or not Path(cases_path).exists():
        return ["审阅包无法重建：manifest 缺少可用的 cases_path"], notes
    packet = build_review_packet(rounds, load_cases(cases_path))
    digest = hashlib.sha256(
        json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if payload.get("packet_sha256") != digest:
        blockers.append(
            f"审阅包哈希不符（过期包）：{payload.get('packet_sha256')} != {digest}"
        )
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        return blockers + ["审阅结论 reviews 结构非法"], notes
    pending = {
        (item.case_id, item.round_index, check.check_id)
        for item in rounds
        for check in item.checks
        if check.status == "pending_review"
    }
    seen: set = set()
    for item in reviews:
        key = (item.get("case_id"), item.get("round_index"), item.get("check_id"))
        if key in seen:
            blockers.append(f"审阅结论身份重复：{key}")
            continue
        seen.add(key)
        if key not in pending:
            blockers.append(f"审阅结论指向未知或已判定检查项：{key}")
        if item.get("status") not in REVIEW_STATUSES:
            blockers.append(f"审阅结论状态非法：{key} -> {item.get('status')}")
        if not str(item.get("reviewer") or "").strip():
            blockers.append(f"审阅结论缺少评审者：{key}")
        refs = item.get("evidence_refs") or []
        if not refs:
            blockers.append(f"审阅结论缺少证据引用：{key}")
        for ref in refs:
            if _evidence_ref_invalid(experiment_dir, ref):
                blockers.append(f"审阅证据引用失效：{key} -> {ref}")
    uncommitted = pending - seen
    if uncommitted:
        notes.append(f"仍有 {len(uncommitted)} 项必需语义检查未给出审阅结论")
    return blockers, notes


def _review_blockers(experiment_dir: Path, manifest, rounds) -> tuple[list[str], list[str], list[dict] | None]:
    # 审阅输入与输出均须绑定实验身份、包哈希、检查身份、评审者与证据引用。
    review_path = experiment_dir / "review.json"
    if not review_path.exists():
        return ["缺少 review.json 审阅结论，必需语义检查保持待审阅"], [], None
    payload = read_record(review_path)
    blockers, notes = _validate_review_payload(experiment_dir, manifest, rounds, payload)
    if blockers:
        return blockers, notes, None
    return blockers, notes, payload.get("reviews")


def release_check(experiment_dir: Path) -> dict:
    """冒烟放行判定：期望槽位取 manifest 冻结计划，必需项任一未满足即保持未放行。"""
    blockers: list[str] = []
    notes: list[str] = []
    manifest = read_record(experiment_dir / "manifest.json")
    plan = manifest_slot_plan(experiment_dir)
    expected_pairs = {(record["case_id"], int(record["round_index"])) for record in plan}
    rounds = load_round_evidence(experiment_dir)
    observed_pairs = {(item.case_id, item.round_index) for item in rounds}
    if observed_pairs != expected_pairs:
        blockers.append(
            f"槽位集合与冻结计划不符：缺 {sorted(expected_pairs - observed_pairs)}，"
            f"多 {sorted(observed_pairs - expected_pairs)}"
        )
    review_blockers, review_notes, reviews = _review_blockers(experiment_dir, manifest, rounds)
    blockers += review_blockers
    notes += review_notes
    if reviews is not None and not review_blockers:
        rounds = apply_v2_review(rounds, reviews)
        notes.append(f"已应用 {len(reviews)} 条审阅结论")
    for round_item in rounds:
        tag = f"{round_item.case_id}/r{round_item.round_index}"
        if round_item.status != "pass":
            blockers.append(f"{tag} 状态={round_item.status}")
        for check in round_item.checks:
            if check.status == "fail":
                blockers.append(f"{tag} 检查 {check.check_id} 失败：{check.reason}")
            if check.status == "pending_review":
                blockers.append(f"{tag} 检查 {check.check_id} 仍待审阅")
        for item in round_item.confirmations:
            if item.kind != "confirm_negative":
                continue
            missing = [
                key for key in ("endpoint_hit", "condition_broken", "controlled_error", "zero_write")
                if not item.assertions.get(key)
            ]
            if missing:
                blockers.append(f"{tag} 步骤{item.step_index} 负向断言缺失 {missing}")
        if round_item.startup_ms is None or round_item.precondition_ms is None:
            blockers.append(f"{tag} 缺少 startup_ms/precondition_ms 单列计时")
    protection_path = experiment_dir / "protection.json"
    if not protection_path.exists():
        blockers.append("缺少 protection.json 受保护数据基线")
    else:
        drift = read_record(protection_path).get("drift") or []
        if drift:
            blockers.append(f"受保护数据漂移：{drift}")
    budget_file = experiment_dir / "budget-summary.json"
    if not budget_file.exists():
        blockers.append("缺少 budget-summary.json 记账快照")
    else:
        snapshot = read_record(budget_file)
        for model, bucket in snapshot.get("models", {}).items():
            if bucket.get("paused_at"):
                blockers.append(f"{model} 额度触达阈值已暂停（actual={bucket.get('actual')}）")
            if bucket.get("unknown_attempts"):
                notes.append(f"{model} 有 {len(bucket['unknown_attempts'])} 次用量未知")
    blockers += _trace_blockers(experiment_dir, manifest, expected_pairs)
    markers = ('"provider": "example"', '"model": "model-1"')
    for path in experiment_dir.glob("cases/*/round-*/step-*.json"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in markers):
            blockers.append(f"业务证据含构造标记：{path}")
    return {"released": not blockers, "planned_slots": len(plan), "blockers": blockers, "notes": notes}


def _trace_blockers(experiment_dir: Path, manifest, expected_pairs) -> list[str]:
    # 放行读取的 Trace 报告需与对账模块的实际结构一致，并逐项核对实验身份与槽位集合。
    trace_file = experiment_dir / "trace-reconciliation.json"
    if not trace_file.exists():
        return ["缺少 trace-reconciliation.json"]
    trace = read_record(trace_file)
    blockers: list[str] = []
    if trace.get("schema") != "v2-trace-reconciliation/1":
        blockers.append(f"Trace 报告结构版本未知：{trace.get('schema')}")
    if trace.get("experiment_id") != manifest["experiment_id"]:
        blockers.append(f"Trace 报告指向其他实验：{trace.get('experiment_id')}")
    slots = trace.get("slots")
    if not isinstance(slots, list):
        return blockers + ["Trace 报告缺少 slots 列表"]
    if not slots:
        blockers.append("Trace 报告 slots 为空")
    trace_pairs = {(item.get("case_id"), item.get("round_index")) for item in slots}
    if trace_pairs != expected_pairs:
        blockers.append(
            f"Trace 槽位集合与冻结计划不符：缺 {sorted(expected_pairs - trace_pairs)}，"
            f"多 {sorted(trace_pairs - expected_pairs)}"
        )
    for entry in slots:
        tag = f"{entry.get('case_id')}/r{entry.get('round_index')}"
        if entry.get("evidence_invalid") or entry.get("interrupted"):
            blockers.append(f"{tag} Trace 证据不可用：{entry.get('reason') or '中断槽位'}")
            continue
        if not entry.get("complete"):
            blockers.append(f"{tag} 链路不完整：{entry.get('incomplete_reasons')}")
        for field in ("missing",):
            if entry.get(field):
                blockers.append(f"{tag} 缺证据 {entry[field]}")
        agent = entry.get("agent_run") or {}
        for name, report in (agent.get("groups") or {}).items():
            for field in ("missing", "association_errors"):
                if report.get(field):
                    blockers.append(f"{tag} agent_run.{name}.{field}={report[field]}")
            if report.get("extra"):
                blockers.append(f"{tag} agent_run.{name}.extra={report['extra']}")
        if agent.get("unattributed"):
            blockers.append(f"{tag} agent_run.unattributed={agent['unattributed']}")
        if agent.get("duplicate_run_ids"):
            blockers.append(f"{tag} agent_run.duplicate_run_ids={agent['duplicate_run_ids']}")
        confirmation = entry.get("confirmation") or {}
        for field in ("missing", "extra", "business_kind_mismatches", "session_mismatches"):
            if confirmation.get(field):
                blockers.append(f"{tag} confirmation.{field}={confirmation[field]}")
        if not (confirmation.get("idempotency_check") or {}).get("passed", False):
            blockers.append(f"{tag} confirmation 幂等核验未通过：{confirmation.get('idempotency_check')}")
        if (entry.get("trace") or {}).get("structural_violations"):
            blockers.append(f"{tag} 结构违规={entry['trace']['structural_violations']}")
    aggregates = trace.get("aggregates") or {}
    if aggregates.get("structural_violations"):
        blockers.append(f"汇总结构违规={aggregates['structural_violations']}")
    return blockers


def run_release(args) -> int:
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    result = release_check(experiment_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["released"] else 1


def write_reports(experiment_dir: Path, experiment_id: str, plan, rounds, benchmarks, identity) -> dict:
    manifest = read_record(Path(experiment_dir) / "manifest.json")
    case_ids = list(dict.fromkeys(record["case_id"] for record in plan))
    summary = {
        "experiment_id": experiment_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases_sha256": sha256_file(manifest["cases_path"]),
        "planned_slots": len(plan),
        "smoke": bool(manifest.get("smoke")),
        "model_identity": identity,
        "status": "incomplete" if len(rounds) != len(plan) or any(r.status == "error" for r in rounds) else "complete",
        "aggregate": aggregate(rounds, case_ids, int(manifest["rounds"])),
        "additional_runs": attempt_summary(experiment_dir),
        "benchmarks": benchmarks,
        "rounds": [r.model_dump() for r in rounds],
    }
    (experiment_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    (experiment_dir / "execution-ledger.json").write_text(
        json.dumps(build_execution_ledger(plan, rounds, experiment_dir), ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    rows = build_comparison(benchmarks, summary, load_v1_recalc())
    with (experiment_dir / "comparison.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    ledger = BudgetLedger(budget_path(experiment_dir), DEFAULT_LIMITS, experiment_id=experiment_id)
    (experiment_dir / "budget-summary.json").write_text(
        json.dumps(ledger.snapshot(), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    return summary


def current_identity(cases_path, model_identity):
    return execution_identity(BACKEND, cases_path, model_identity, DEFAULT_ACTIONS_PATH)


def verify_frozen_identity(experiment_dir, cases_path, model_identity):
    manifest = read_record(Path(experiment_dir) / "manifest.json")
    identity = current_identity(cases_path, model_identity)
    if identity != manifest.get("execution_identity"):
        raise ValueError("冻结代码/配置身份不匹配；需要关联原槽位的新实验身份")
    return identity


def run_additional(args):
    if not args.experiment_id or not args.case_id or args.round_index is None or not args.reason:
        raise ValueError("追加运行需要 --experiment-id --case-id --round-index --reason")
    if args.finalize_only or args.benchmarks_only or args.process_index is not None or args.parent_experiment_id:
        raise ValueError("追加运行参数冲突")
    directory = Path(args.experiment_root) / args.experiment_id
    cases = load_cases(args.cases)
    case = next((item for item in cases if item.case_id == args.case_id), None)
    if case is None:
        raise ValueError("未知原用例")
    model_identity = use_frozen_model_config(directory)
    identity = verify_frozen_identity(directory, args.cases, model_identity)
    ledger = BudgetLedger(Path(read_record(directory / "manifest.json")["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=args.experiment_id)
    if ledger.paused_models():
        # 追加运行同样受共享额度暂停门禁约束，触达阈值后不再新增请求。
        print("Embedding 额度触达阈值，追加运行停止派发", flush=True)
        return 3
    stop = ledger.stop_info()
    if stop is not None:
        write_stop_report(directory, stop)
        print(f"共享停止已置位（{stop.get('reason_category')}），追加运行停止派发", flush=True)
        return 3
    attempt_root, record = create_attempt(directory, args.case_id, args.round_index, args.purpose, args.reason, identity)
    manifest = read_record(directory / "manifest.json")
    # 追加沿用父实验作用域，证据与结算另带独立 attempt 身份。
    install_embedding_recorder(ledger, experiment_id=args.experiment_id, process_index=-1)
    env = ExperimentEnv(
        experiment_id=args.experiment_id,
        experiment_scope=args.experiment_id,
        attempt_id=record["attempt_id"],
        case_id=case.case_id,
        round_index=args.round_index,
        slot=-1,
        process_index=-1,
        database_path=str(attempt_root / "dbs" / f"{case.case_id}-r{args.round_index}.db"),
        trace_path=str(attempt_root / "traces" / f"{case.case_id}-r{args.round_index}.db"),
        attachment_root=str(attempt_root / "attachments"),
        evidence_dir=str(attempt_root / "cases" / case.case_id / f"round-{args.round_index}"),
        execution_root=str(attempt_root),
        budget_ledger_path=record["budget_ledger_path"],
        business_date=manifest["business_date"],
        model_identity=model_identity,
    )
    parent_path = directory / "cases" / args.case_id / f"round-{args.round_index}" / "round.json"
    parent_status = read_record(parent_path)["status"] if parent_path.exists() else None
    try:
        observation = run_case_round(case, args.round_index, -1, env, model_identity)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        # 取消、中断与进程退出保留原控制流语义，不落 run_error 记录。
        raise
    except LedgerAttributionUnresolved as unresolved:
        record.update(
            status="blocked_by_ledger_attribution", ended_at=timestamp(), result=None,
            error="; ".join(unresolved.attribution.get("problems") or []), failure_reproduced=None,
        )
        write_record(attempt_root / "attempt.json", record)
        print(f"追加运行遇到账本归属未确认：{record['error']}", flush=True)
        return 3
    except StopTriggered as stopped:
        record.update(
            status="blocked_by_shared_stop", ended_at=timestamp(), result=None,
            error=f"StopTriggered: {stopped.stop.get('reason_category')}", failure_reproduced=None,
        )
        write_record(attempt_root / "attempt.json", record)
        write_stop_report(directory, stopped.stop)
        print(f"追加运行遇到共享停止：{stopped.stop.get('reason_category')}", flush=True)
        return 3
    except BaseException as failure:
        record.update(
            status="run_error", ended_at=timestamp(), result=None,
            error=f"{type(failure).__name__}: {failure}", failure_reproduced=None,
        )
        write_record(attempt_root / "attempt.json", record)
        print(f"追加运行失败：{record['error']}", flush=True)
        _record_run_outcome(ledger, directory, args.experiment_id, "run_error", record["error"],
                            execution_unit=_unit_identity("attempt", attempt_id=record["attempt_id"]),
                            error_type=exception_type(failure), slot=-1, process_index=-1,
                            trigger="additional:attempt")
        return 2
    record.update(
        status=observation.status,
        ended_at=timestamp(),
        result=observation.model_dump(),
        failure_reproduced=None if args.purpose != "reproduction" else observation.status == parent_status,
        excluded_from_denominator=True,
    )
    write_record(attempt_root / "attempt.json", record)
    _record_run_outcome(ledger, directory, args.experiment_id, observation.status, None,
                        execution_unit=_unit_identity("attempt", attempt_id=record["attempt_id"]),
                        error_type=observation.error_type, slot=-1, process_index=-1,
                        trigger="additional:attempt")
    print(f"追加运行 {record['attempt_id']} 结果 {observation.status}，不计入正式 150 分母", flush=True)
    return 0


def _review_stage(experiment_dir: Path, cases, rounds, review_path):
    # 审阅包与判定回写：语义结论只作用于 pending_review 检查项。
    packet = build_review_packet(rounds, cases)
    (experiment_dir / "review-packet.json").write_text(
        json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    if not review_path:
        return rounds, None
    manifest = read_record(experiment_dir / "manifest.json")
    payload = read_record(review_path)
    if isinstance(payload, list):
        payload = {"reviews": payload}
    # 应用与派生输出写入之前校验原输入身份：外实验、过期包、缺元数据、重复检查或无效引用一律拒绝。
    blockers, notes = _validate_review_payload(experiment_dir, manifest, rounds, payload)
    if blockers:
        (experiment_dir / "review-rejected.json").write_text(
            json.dumps({"input": str(review_path), "blockers": blockers, "notes": notes},
                       ensure_ascii=False, allow_nan=False, indent=2),
            encoding="utf-8",
        )
        raise ValueError("审阅输入未通过身份校验，拒绝应用且不重签原输入身份：" + "; ".join(blockers))
    reviews = payload["reviews"]
    applied = apply_v2_review(rounds, reviews)
    (experiment_dir / "review.json").write_text(
        json.dumps({
            "experiment_id": manifest["experiment_id"],
            "reviewed_by": "agent",
            "reviews": reviews,
            "packet_size": len(packet),
            "packet_sha256": hashlib.sha256(
                json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    return applied, reviews


def run_orchestrator(args, cases_path: Path) -> int:
    if args.experiment_id is None:
        args.experiment_id = new_experiment_id()
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    reserve_experiment(experiment_dir)
    cases = precheck(cases_path, experiment_dir, args.smoke_cases)
    model_source = resolve_model_source(args)
    identity = freeze_model_config(model_source, experiment_dir)
    business_date_value = trusted_business_date()
    manifest = build_manifest(
        REPO_ROOT, args.experiment_id, cases_path, identity, business_date_value,
        args.processes, bool(args.smoke_cases),
    )
    manifest["slot_plan"] = slot_plan(cases, bool(args.smoke_cases))
    manifest["planned_slots"] = len(manifest["slot_plan"])
    manifest["execution_identity"] = current_identity(cases_path, identity)
    if args.parent_experiment_id:
        if not args.case_id or args.round_index is None or not args.reason:
            raise ValueError("关联新实验需要 --case-id --round-index --reason")
        parent_dir = Path(args.experiment_root) / args.parent_experiment_id
        parent_manifest = read_record(parent_dir / "manifest.json")
        manifest["parent_slot"] = {"experiment_id": parent_manifest["experiment_id"], "case_id": args.case_id, "round_index": args.round_index}
        manifest["reason"] = args.reason
        if not (parent_dir / "cases" / args.case_id / f"round-{args.round_index}").exists() and not (parent_dir / "claims" / f"{args.case_id}-r{args.round_index}.json").exists():
            raise ValueError("关联原槽位不存在")
        manifest["budget_ledger_path"] = str(budget_path(parent_dir))
    else:
        manifest["budget_ledger_path"] = str(budget_path(experiment_dir))
    manifest["protected_baseline_before"] = protected_baseline(REPO_ROOT)
    (experiment_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    plan = manifest_slot_plan(experiment_dir)
    ledger = BudgetLedger(Path(manifest["budget_ledger_path"]), DEFAULT_LIMITS, experiment_id=manifest["experiment_id"])
    # 记账包装器幂等安装：主控进程内的专项与单进程派发都走同一真实 usage 口径。
    install_embedding_recorder(ledger, experiment_id=args.experiment_id, process_index=0)
    if args.benchmarks_only:
        rounds = load_round_evidence(experiment_dir)
    elif args.processes > 1:
        spawn_processes(args, cases_path, model_source)
        rounds = load_round_evidence(experiment_dir)
    else:
        identity = use_frozen_model_config(experiment_dir)
        try:
            verify_frozen_identity(experiment_dir, cases_path, identity)
        except ValueError as error:
            _trip_stop(ledger, experiment_dir, "frozen_identity_drift", experiment_id=args.experiment_id,
                       trigger="orchestrator:single", detail=str(error))
            raise
        cases_by_id = {case.case_id: case for case in cases}
        logs = experiment_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        schedule = logs / "schedule-p0.jsonl"
        for slot, case, round_index in pairs_from_plan(plan, cases_by_id):
            if _attribution_guard(ledger, experiment_dir, schedule, slot, case, round_index, 0):
                break
            if budget_pause_guard(ledger, schedule, slot, case, round_index, 0):
                _write_budget_pause(experiment_dir, ledger, 0)
                break
            if _stop_guard(ledger, experiment_dir, schedule, slot, case, round_index, 0):
                break
            if claimed_pair(experiment_dir, case.case_id, round_index):
                continue
            env = experiment_env(args.experiment_id, case, round_index, slot, 0, experiment_dir, identity, business_date_value)
            try:
                observation = run_case_round(case, round_index, slot, env, identity)
                # 与 worker 同源：observation 返回路径传播捕获现场的结构化异常类型。
                status, error = observation.status, None
                error_type = observation.error_type
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except LedgerAttributionUnresolved as unresolved:
                _append_schedule(schedule, slot, case, round_index, 0, timestamp(),
                                 "blocked_by_ledger_attribution",
                                 "; ".join(unresolved.attribution.get("problems") or []))
                print(f"[slot {slot} {case.case_id} r{round_index}] 账本归属未确认，停止派发", flush=True)
                break
            except StopTriggered as stopped:
                _append_schedule(schedule, slot, case, round_index, 0, timestamp(),
                                 "blocked_by_shared_stop", stopped.stop.get("reason_category"))
                write_stop_report(experiment_dir, stopped.stop)
                print(f"[slot {slot} {case.case_id} r{round_index}] 共享停止已置位，停止派发", flush=True)
                break
            except BudgetExhausted as exhausted:
                _append_schedule(schedule, slot, case, round_index, 0, timestamp(),
                                 "blocked_by_embedding_budget", str(exhausted))
                _write_budget_pause(experiment_dir, ledger, 0)
                print(f"[slot {slot} {case.case_id} r{round_index}] Embedding 额度已用尽，停止派发", flush=True)
                break
            except BaseException as failure:
                status = "run_error"
                failure_type = exception_type(failure)
                error = f"{failure_type}: {failure}"
                _append_schedule(schedule, slot, case, round_index, 0, timestamp(), status, error,
                                 error_type=failure_type)
                print(f"[slot {slot} {case.case_id} r{round_index}] {status}: {error}", flush=True)
                if _record_run_outcome(ledger, experiment_dir, args.experiment_id, status, error,
                                       execution_unit=_unit_identity("slot", slot=slot),
                                       error_type=failure_type, slot=slot, process_index=0):
                    break
                continue
            _record_run_outcome(ledger, experiment_dir, args.experiment_id, status, error,
                                execution_unit=_unit_identity("slot", slot=slot),
                                error_type=error_type, slot=slot, process_index=0)
        rounds = load_round_evidence(experiment_dir)
    if manifest.get("smoke"):
        # 冒烟只验证业务链路与放行门槛；专项样本在正式实验结算阶段独立运行。
        benchmarks = {}
        print("冒烟实验跳过专项基准，专项样本在正式实验内独立产出", flush=True)
    else:
        benchmarks = run_benchmarks(experiment_dir)
    rounds, _reviews = _review_stage(experiment_dir, cases, rounds, args.review)
    summary = write_reports(experiment_dir, args.experiment_id, plan, rounds, benchmarks, identity)
    paused = ledger.paused_models()
    if paused:
        summary["embedding_budget_paused"] = paused
        print(f"Embedding 额度触达阈值，已停止新增派发：{paused}", flush=True)
    after = protected_baseline(REPO_ROOT)
    drift = compare_baseline(manifest["protected_baseline_before"], after)
    if drift:
        # 生产保护差异：受保护范围出现差异即置位共享停止；发现时点为保护比较阶段。
        _trip_stop(ledger, experiment_dir, "production_drift", experiment_id=args.experiment_id,
                   trigger="orchestrator:protection", evidence_refs=["protection.json"], detail=drift)
    (experiment_dir / "protection.json").write_text(
        json.dumps(
            {
                "before": manifest["protected_baseline_before"],
                "after": after,
                # 稳定受保护身份与采集工件元数据分列：比较只使用前者，后者留证。
                "before_identity": baseline_identity(manifest["protected_baseline_before"]),
                "after_identity": baseline_identity(after),
                "before_artifacts": baseline_artifacts(manifest["protected_baseline_before"]),
                "after_artifacts": baseline_artifacts(after),
                "drift": drift,
                "conclusion": "受保护数据零变化" if not drift else "检出漂移，实验数据有效性逐项待判定",
            },
            ensure_ascii=False, allow_nan=False, indent=2,
        ),
        encoding="utf-8",
    )
    summary["protection_drift"] = drift
    (experiment_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2), flush=True)
    print(f"实验目录: {experiment_dir}", flush=True)
    if drift:
        print(f"受保护数据漂移: {drift}", flush=True)
        return 4
    if paused:
        return 3
    return 0


def run_finalize(args) -> int:
    if args.experiment_id is None:
        raise SystemExit("--finalize-only 需要 --experiment-id")
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    manifest = read_record(experiment_dir / "manifest.json")
    cases = load_cases(manifest["cases_path"])
    summary_path = experiment_dir / "summary.json"
    existing = json.loads(summary_path.read_text(encoding="utf-8"))
    rounds = load_round_evidence(experiment_dir)
    rounds, _reviews = _review_stage(experiment_dir, cases, rounds, args.review)
    benchmarks = existing.get("benchmarks", {})
    write_reports(experiment_dir, args.experiment_id, manifest_slot_plan(experiment_dir),
                  rounds, benchmarks, existing["model_identity"])
    return 0


def load_backend_env() -> None:
    # 检索与 Milvus 配置来自 backend/.env；子进程继承父进程环境，故只需在入口加载一次。
    from dotenv import load_dotenv

    path = BACKEND / ".env"
    if path.exists():
        load_dotenv(path, override=False)


def main(argv=None) -> int:
    load_backend_env()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cases_path = Path(args.cases)
    if args.benchmark_topic is not None:
        return run_benchmark_worker(args, args.benchmark_topic)
    if args.purpose:
        return run_additional(args)
    if args.check_release:
        return run_release(args)
    if args.finalize_only:
        return run_finalize(args)
    if args.process_index is not None:
        cases = precheck(cases_path, Path(args.experiment_root) / args.experiment_id, args.smoke_cases)
        return run_worker(args, cases)
    return run_orchestrator(args, cases_path)


if __name__ == "__main__":
    raise SystemExit(main())
