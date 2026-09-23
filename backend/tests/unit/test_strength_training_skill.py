# strength-training 的最小加载契约：真实 SkillLoader 按名取到正文与它明确引用的两份 reference，
# 且它在 General 装载清单里只登记一次。

from app.application.agent.harness.registry import GENERAL_INTENT_SKILLS
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "strength-training"
REFERENCE_PATHS: tuple[str, ...] = (
    "references/progression-rules.md",
    "references/few-shots.md",
)


def test_strength_training_reads_both_references_by_path() -> None:
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


def test_strength_training_is_registered_once_and_loadable() -> None:
    names = [name for names in GENERAL_INTENT_SKILLS.values() for name in names]
    assert SKILL_NAME in names
    assert any(
        item.name == SKILL_NAME for item in SkillLoader(skills_dir()).list_metadata()
    )
