"""真实评测场景集成检查：默认跳过，只有显式设置环境变量才外呼模型。"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from evaluation.dataset import load_for_evaluation  # noqa: E402
from evaluation.generation_scoring import validate_timeout  # noqa: E402
from evaluation.scenarios import (  # noqa: E402
    DEFAULT_JUDGE_TIMEOUT,
    DEFAULT_VERSION_DIR,
    SCENARIO_ROOT,
    _judge_config,
    run_selected,
    select_scenarios,
)

ENABLE_VARIABLE = "FIT_AGENT_EVALUATION_SCENARIOS_INTEGRATION"
VALIDATION_SCENARIOS = (
    "s-plan-generation-01",
    "s-plan-import-file-01",
    "s-plan-import-file-02",
    "s-profile-update-01",
    "s-profile-update-08",
    "s-confirmation-cancellation-state-04",
    "s-confirmation-cancellation-state-08",
)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实评测场景定向验收；未设置 "
        f"{ENABLE_VARIABLE}=1 时直接跳过。"
    )
    parser.add_argument("--scenario-id", action="append", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--root", type=Path, default=SCENARIO_ROOT)
    parser.add_argument("--judge-timeout", type=float, default=DEFAULT_JUDGE_TIMEOUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if os.environ.get(ENABLE_VARIABLE) != "1":
        print(
            f"未启用真实场景集成检查：设置 {ENABLE_VARIABLE}=1 后执行。",
            flush=True,
        )
        return 0
    load_dotenv(BACKEND / ".env", override=False)
    dataset = load_for_evaluation(DEFAULT_VERSION_DIR)
    scenario_ids = args.scenario_id or list(VALIDATION_SCENARIOS)
    scenarios = select_scenarios(dataset, scenario_ids)
    run_id = args.run_id or (
        "validation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-" + uuid4().hex[:8]
    )
    timeout = validate_timeout(args.judge_timeout)
    judge, _ = _judge_config()
    summary = asyncio.run(run_selected(
        dataset,
        scenarios,
        run_id=run_id,
        root=args.root,
        enable_model=True,
        judge=judge,
        judge_timeout=timeout,
    ))
    print(json.dumps(
        summary.model_dump(mode="json"), ensure_ascii=False, indent=2
    ))
    if (
        summary.blocked
        or summary.failed
        or summary.incomplete
        or summary.coverage.observed_scenarios
        != summary.coverage.expected_scenarios
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
