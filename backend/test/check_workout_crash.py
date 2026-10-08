import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

from app.domain.business.models import BusinessContext, WorkoutListArguments, WorkoutSaveArguments
from test.check_profile_confirmation import Fixture, new_id
from test.check_profile_crash import terminate_owned
from test.check_workout_service_http import prepare
from test.regression_support import temporary_root

ROOT = temporary_root("workout-crash") / uuid4().hex


async def worker(root: Path, phase: str):
    f = await Fixture(root / "crash.db").seeded()
    session = await f.session("训练进程中断")
    day = f.context(new_id(), new_id(), new_id()).business_date
    context, arguments, _ = await prepare(f, session, day)
    (root / "identity.json").write_text(json.dumps({"context": context.model_dump(),
        "arguments": arguments.model_dump()}), encoding="utf-8")
    blocked = Event()
    commits = 0

    def trace(sql):
        nonlocal commits
        if sql == "COMMIT":
            commits += 1
            if phase == "before_commit" and commits == 2:
                (root / "ready.json").write_text(json.dumps({"phase": phase, "pid": os.getpid()}), encoding="utf-8")
                blocked.wait(60)

    await f.database.connection.set_trace_callback(trace)
    saved = await f.business.save_workout(context, arguments)
    assert phase == "after_commit"
    (root / "saved.json").write_text(saved.model_dump_json(), encoding="utf-8")
    (root / "ready.json").write_text(json.dumps({"phase": phase, "pid": os.getpid()}), encoding="utf-8")
    await asyncio.to_thread(blocked.wait, 60)


async def recover(root: Path, phase: str):
    identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
    context = BusinessContext.model_validate(identity["context"])
    arguments = WorkoutSaveArguments.model_validate(identity["arguments"])
    f = await Fixture(root / "crash.db").open(recovered=True)
    try:
        await f.repository.recover_interrupted_workout_saves()
        expected = "pending" if phase == "before_commit" else "saved"
        assert (await f.business.get_workout_save_status(context, arguments.proposal_id)).status == expected
        before = await f.business.list_workouts(WorkoutListArguments())
        assert before.total == (0 if phase == "before_commit" else 1)
        result = await f.business.save_workout(context, arguments)
        assert result.version == 1
        assert await f.business.save_workout(context, arguments) == result
        assert (await f.business.list_workouts(WorkoutListArguments())).total == 1
        assert (await f.business.get_workout_save_status(context, arguments.proposal_id)).result == result
        if phase == "after_commit":
            assert result.model_dump() == json.loads((root / "saved.json").read_text(encoding="utf-8"))
        return {"phase": phase, "recovered": expected, "result": result.model_dump()}
    finally:
        await f.close()


def check():
    ROOT.mkdir()
    results = []
    for phase in ("before_commit", "after_commit"):
        directory = ROOT / phase
        directory.mkdir()
        with (directory / "worker.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen([sys.executable, "-m", "test.check_workout_crash", str(directory), phase],
                                       stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while not (directory / "ready.json").exists():
                    assert process.poll() is None, directory / "worker.log"
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                marker = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
                assert marker["phase"] == phase and marker["pid"] > 0
                terminate_owned(process)
            finally:
                if process.poll() is None:
                    terminate_owned(process)
        results.append(asyncio.run(recover(directory, phase)))
    (ROOT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: real workout process termination before/after COMMIT, pending/saved recovery, fixed-result idempotency:", ROOT)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        asyncio.run(worker(Path(sys.argv[1]), sys.argv[2]))
    else:
        check()
