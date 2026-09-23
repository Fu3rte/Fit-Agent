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


def test_strength_training_loads_both_references() -> None:
    loaded = SkillLoader(skills_dir()).load(SKILL_NAME)

    assert loaded.metadata.name == SKILL_NAME
    assert loaded.metadata.description
    assert loaded.body.strip()
    assert tuple(reference.path for reference in loaded.references) == REFERENCE_PATHS
    assert all(reference.text.strip() for reference in loaded.references)


def test_strength_training_is_registered_once_and_loadable() -> None:
    names = [name for names in GENERAL_INTENT_SKILLS.values() for name in names]
    assert SKILL_NAME in names
    assert SkillLoader(skills_dir()).load(SKILL_NAME).metadata.name == SKILL_NAME
