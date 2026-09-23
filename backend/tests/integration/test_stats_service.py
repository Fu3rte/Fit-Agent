from datetime import date
from pathlib import Path

from app.application.services.stats_service import StatsService
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from app.infrastructure.database.repositories.stats_repository import StatsRepo


async def test_calendar_range_reads_the_exact_closed_interval(tmp_path: Path) -> None:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, NULL)",
                ("2026-05-31",),
            )
            await conn.execute(
                "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, NULL)",
                ("2026-06-02",),
            )
            await conn.execute(
                "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, NULL)",
                ("2026-06-04",),
            )

        days = await StatsService(StatsRepo(db), PlanRepo(db)).calendar_range(
            date(2026, 6, 1), date(2026, 6, 3)
        )

        assert [day.date for day in days] == [date(2026, 6, 2)]
        assert [fact.performed_on for fact in days[0].workouts] == [date(2026, 6, 2)]
        assert days[0].plan_sessions == ()
    finally:
        await db.close()
