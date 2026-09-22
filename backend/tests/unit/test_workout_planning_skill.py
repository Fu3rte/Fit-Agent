# workout-planning 的最小加载契约：真实 SkillLoader 必须按名取到正文与它明确引用的两份 reference。

from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "workout-planning"
REFERENCE_PATHS: tuple[str, ...] = (
    "references/planning-rules.md",
    "references/few-shots.md",
)


def test_workout_planning_loads_both_references() -> None:
    loaded = SkillLoader(skills_dir()).load(SKILL_NAME)

    assert loaded.metadata.name == SKILL_NAME
    assert loaded.metadata.description
    assert loaded.body.strip()
    assert tuple(reference.path for reference in loaded.references) == REFERENCE_PATHS
    assert all(reference.text.strip() for reference in loaded.references)
