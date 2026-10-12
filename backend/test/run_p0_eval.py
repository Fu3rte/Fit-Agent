import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from test.p0_contract import ROUNDS, CaseRoundObservation
from test.p0_eval_support import (
    BACKEND,
    DEFAULT_DATA_ROOT,
    aggregate,
    apply_review,
    build_manifest,
    experiment_env,
    freeze_model_config,
    load_cases,
    load_reviews,
    run_case_round,
    sha256_file,
    trusted_business_date,
    validate_cases,
)

REPO_ROOT = BACKEND.parent


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fit-Agent P0 业务评测单一入口")
    parser.add_argument(
        "--cases", default=str(BACKEND / "test" / "p0_cases.json")
    )
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument(
        "--experiment-root", default=str(BACKEND / "temp" / "p0-evaluation")
    )
    parser.add_argument("--model-source", default=None)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=None)
    parser.add_argument("--review", default=None)
    parser.add_argument("--finalize-only", action="store_true")
    return parser.parse_args(argv)


def resolve_model_source(args: argparse.Namespace) -> str:
    if args.model_source:
        return str(Path(args.model_source))
    override = os.environ.get("FIT_AGENT_P0_MODEL_SOURCE")
    if override:
        return str(Path(override))
    return str(DEFAULT_DATA_ROOT)


def new_experiment_id() -> str:
    return "p0-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def precheck(cases_path: Path, experiment_dir: Path) -> list:
    cases = load_cases(cases_path)
    errors = validate_cases(cases)
    if errors:
        raise SystemExit("用例校验失败:\n" + "\n".join(errors))
    experiment_dir.mkdir(parents=True, exist_ok=True)
    probe = experiment_dir / ".write-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return cases


def run_rounds(cases: list, indices, experiment_id: str, experiment_dir: Path, identity: dict) -> list:
    rounds = []
    for index in indices:
        case = cases[index]
        for round_index in range(ROUNDS):
            env = experiment_env(
                experiment_id,
                case,
                round_index,
                experiment_dir,
                identity,
                trusted_business_date(),
            )
            round_observation = run_case_round(case, round_index, env, identity)
            rounds.append(round_observation)
            print(
                f"[{case.case_id} round {round_index}] {round_observation.status}",
                flush=True,
            )
    return rounds


def load_round_evidence(experiment_dir: Path) -> list:
    rounds = []
    for path in sorted((experiment_dir / "cases").glob("*/round-*/round.json")):
        rounds.append(
            CaseRoundObservation.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        )
    return rounds


def spawn_shards(args: argparse.Namespace, cases_path: Path, model_source: str) -> None:
    environment = dict(os.environ)
    environment["FIT_AGENT_P0_MODEL_SOURCE"] = model_source
    processes = []
    for shard_index in range(args.shards):
        command = [
            sys.executable,
            "-m",
            "test.run_p0_eval",
            "--cases",
            str(cases_path),
            "--experiment-root",
            args.experiment_root,
            "--experiment-id",
            args.experiment_id,
            "--model-source",
            model_source,
            "--shards",
            str(args.shards),
            "--shard-index",
            str(shard_index),
        ]
        processes.append(subprocess.Popen(command, cwd=str(BACKEND), env=environment))
    failures = []
    for index, process in enumerate(processes):
        code = process.wait()
        if code != 0:
            failures.append(f"分片 {index} 退出码 {code}")
    if failures:
        raise RuntimeError("分片执行失败：" + "; ".join(failures))


def run_benchmarks(experiment_dir: Path) -> dict:
    from test.check_compaction_performance import run_benchmark as compaction_benchmark
    from test.check_tool_parallel_performance import run_benchmark as tool_benchmark

    topics = (
        ("tool_parallel", "tool-parallel", tool_benchmark),
        ("compaction", "compaction", compaction_benchmark),
    )
    results = {}
    for topic, name, runner in topics:
        directory = experiment_dir / "benchmarks" / name
        directory.mkdir(parents=True, exist_ok=True)
        result = runner(str(directory))
        if result.topic != topic:
            raise ValueError(f"专项 {name} 返回主题 {result.topic} 与预期 {topic} 不符")
        (directory / "benchmark.json").write_text(
            json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False, indent=2),
            encoding="utf-8",
        )
        results[topic] = result.model_dump()
    return results


def summary_status(rounds: list, benchmarks: dict) -> str:
    if any(item.status == "error" for item in rounds):
        return "incomplete"
    if any(item["status"] != "complete" for item in benchmarks.values()):
        return "incomplete"
    return "complete"


def run_worker(args: argparse.Namespace, cases: list, cases_path: Path) -> int:
    from test.p0_eval_support import use_frozen_model_config

    experiment_dir = Path(args.experiment_root) / args.experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True)
    identity = use_frozen_model_config(experiment_dir)
    indices = [
        index for index in range(len(cases)) if index % args.shards == args.shard_index
    ]
    run_rounds(cases, indices, args.experiment_id, experiment_dir, identity)
    return 0


def run_orchestrator(args: argparse.Namespace, cases_path: Path) -> int:
    if args.experiment_id is None:
        args.experiment_id = new_experiment_id()
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    cases = precheck(cases_path, experiment_dir)
    model_source = resolve_model_source(args)
    identity = freeze_model_config(model_source, experiment_dir)
    manifest = build_manifest(REPO_ROOT, args.experiment_id, cases_path, identity)
    (experiment_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    if args.shards > 1:
        spawn_shards(args, cases_path, model_source)
        rounds = load_round_evidence(experiment_dir)
    else:
        rounds = run_rounds(
            cases, range(len(cases)), args.experiment_id, experiment_dir, identity
        )
    benchmarks = run_benchmarks(experiment_dir)
    summary = {
        "experiment_id": args.experiment_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases_sha256": sha256_file(cases_path),
        "model_identity": identity,
        "status": summary_status(rounds, benchmarks),
        "aggregate": aggregate(rounds),
        "benchmarks": benchmarks,
        "rounds": [item.model_dump() for item in rounds],
    }
    (experiment_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2), flush=True)
    print(f"实验目录: {experiment_dir}", flush=True)
    return 0


def run_finalize(args: argparse.Namespace) -> int:
    if args.experiment_id is None:
        raise SystemExit("--finalize-only 需要 --experiment-id")
    experiment_dir = Path(args.experiment_root) / args.experiment_id
    summary_path = experiment_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rounds = load_round_evidence(experiment_dir)
    if args.review:
        rounds = apply_review(rounds, load_reviews(args.review))
        (experiment_dir / "review.json").write_text(
            json.dumps({"source": str(args.review)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    summary["review_applied"] = bool(args.review)
    summary["aggregate"] = aggregate(rounds)
    summary["rounds"] = [item.model_dump() for item in rounds]
    summary["status"] = summary_status(rounds, summary.get("benchmarks", {}))
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2), flush=True)
    print(f"实验目录: {experiment_dir}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cases_path = Path(args.cases)
    if args.finalize_only:
        return run_finalize(args)
    if args.shard_index is not None:
        cases = precheck(cases_path, Path(args.experiment_root) / args.experiment_id)
        return run_worker(args, cases, cases_path)
    return run_orchestrator(args, cases_path)


if __name__ == "__main__":
    raise SystemExit(main())
