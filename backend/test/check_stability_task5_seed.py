import argparse
import asyncio
import json
from pathlib import Path

from test.check_structured_confirmation import PROFILE, Fixture


async def main_async(database: Path) -> dict:
    fixture = await Fixture(database).seeded()
    try:
        session = await fixture.session("任务五中断确认")
        payload = {**PROFILE, "health_notes": "x" * 50_000_000}
        staged = await fixture.stage(
            session,
            "profile",
            {"profile_id": 1, "base_profile_version": 2, "payload": payload},
            "prepare_profile_update",
        )
        return staged
    finally:
        await fixture.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    staged = asyncio.run(main_async(Path(args.db)))
    Path(args.out).write_text(json.dumps(staged, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(staged, ensure_ascii=False))


if __name__ == "__main__":
    main()
