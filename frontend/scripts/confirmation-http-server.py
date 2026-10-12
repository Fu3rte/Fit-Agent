import asyncio
import json
import os
import sys
from pathlib import Path

# 隔离数据目录，保护 backend/data；待确认快照由真实准备流程写入同一 SQLite 文件。
BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

import uvicorn

from app import model_config
from app.application.business.service import business_date
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_structured_confirmation import Fixture, PROFILE, WORKOUT, now_ms

DATA = Path(os.environ["CONFIRM_PROBE_DATA"])
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "probe.db"
model_config.DATA_ROOT = DATA
database_module.default_database_path = lambda: DB


async def seed_primary() -> dict:
    # 两份待确认快照：画像与训练记录，等待前端按钮确认。
    fixture = await Fixture(DB).seeded()
    try:
        session_id = await fixture.session()
        profile_ids = await fixture.stage(
            session_id,
            "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        workout_ids = await fixture.stage(
            session_id,
            "workout",
            {
                "performed_on": business_date(now_ms()),
                "base_workout_id": None,
                "base_workout_version": None,
                "payload": WORKOUT,
            },
            "prepare_workout",
        )
        return {
            "session": session_id,
            "profile": profile_ids,
            "workout": workout_ids,
        }
    finally:
        await fixture.close()


async def stage_conflict() -> dict:
    # 画像版本为全局状态。两张同版本快照分属两个会话：另一会话确认使版本前移后，
    # 本会话的快照仍是有效待确认引用，点击即触发真实版本冲突。
    fixture = await Fixture(DB).open()
    try:
        arguments = {"profile_id": 1, "base_profile_version": 1, "payload": PROFILE}
        stale_session = await fixture.session("版本冲突会话")
        stale = await fixture.stage(
            stale_session, "profile", arguments, "prepare_profile_update"
        )
        bump_session = await fixture.session("版本推进会话")
        bump = await fixture.stage(
            bump_session, "profile", arguments, "prepare_profile_update"
        )
        await fixture.confirmations.confirm(
            bump_session, await fixture.request(bump_session, "profile", bump)
        )
        return {"session": stale_session, "stale": stale}
    finally:
        await fixture.close()


if __name__ == "__main__":
    if os.environ.get("CONFIRM_PROBE_ACTION") == "stage":
        print(json.dumps(asyncio.run(stage_conflict()), ensure_ascii=False))
    else:
        identity = asyncio.run(seed_primary())
        Path(os.environ["CONFIRM_PROBE_OUT"]).write_text(
            json.dumps(identity, ensure_ascii=False), encoding="utf-8"
        )
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=int(os.environ["CONFIRM_PROBE_PORT"]),
            log_level="info",
        )
