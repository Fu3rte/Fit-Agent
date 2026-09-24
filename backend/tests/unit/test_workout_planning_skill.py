# workout-planning 的加载契约：真实 SkillLoader 必须按名取到正文与它声明的全部 reference。

from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "workout-planning"
REFERENCE_PATHS: tuple[str, ...] = (
    "references/planning-rules.md",
    "references/goal-content.md",
    "references/training-principles.md",
    "references/program-design.md",
    "references/trainee-classification.md",
    "references/exercise-selection.md",
    "references/few-shots.md",
)


def test_workout_planning_reads_all_references_by_path() -> None:
    loader = SkillLoader(skills_dir())
    metadata = next(item for item in loader.list_metadata() if item.name == SKILL_NAME)
    body = loader.read_skill(SKILL_NAME)
    references = tuple(
        loader.read_reference(SKILL_NAME, path) for path in REFERENCE_PATHS
    )

    assert metadata.description
    assert body.strip()
    assert tuple(reference.path for reference in references) == REFERENCE_PATHS
    assert all(reference.text.strip() for reference in references)
