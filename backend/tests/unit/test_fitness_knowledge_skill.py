# ST-04 fitness-knowledge Skill：真实 SkillLoader 校验正文、两个 reference 与 General 装载名称。

from app.application.agent.contracts import SkillReference
from app.application.agent.harness.registry import (
    GENERAL_INTENT_SKILLS,
    GENERAL_INTENT_TOOLS,
)
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "fitness-knowledge"
REFERENCE_PATHS = ("references/knowledge-boundaries.md", "references/few-shots.md")
GENERAL_TOOLS = ("read_active_plan", "read_training_history", "search_exercises")


def _load() -> tuple[str, tuple[SkillReference, ...]]:
    """通过按需读取接口读取该 Skill 正文与声明的 reference。"""
    loader = SkillLoader(skills_dir())
    body = loader.read_skill(SKILL_NAME)
    references = tuple(
        loader.read_reference(SKILL_NAME, path) for path in REFERENCE_PATHS
    )
    return body, references


def test_metadata_and_both_references_load_through_the_real_loader() -> None:
    """General 装载矩阵里的名称唯一，真实加载器按该名命中正文与两个 reference。"""
    loader = SkillLoader(skills_dir())
    metadata = next(item for item in loader.list_metadata() if item.name == SKILL_NAME)
    body, references = _load()

    assert metadata.description.strip()
    names = [name for names in GENERAL_INTENT_SKILLS.values() for name in names]
    assert SKILL_NAME in names
    assert tuple(reference.path for reference in references) == REFERENCE_PATHS
    assert all(reference.text.strip() for reference in references)
    assert body.strip()


def test_skill_text_names_only_the_general_intent_tools() -> None:
    """正文与 reference 只点名本 Intent 可见的三项只读 Tool。"""
    assert {tool.name for tool in GENERAL_INTENT_TOOLS["general"]} == set(GENERAL_TOOLS)
    body, references = _load()
    text = "\n".join((body, *(reference.text for reference in references)))
    invisible = {
        tool.name for tools in GENERAL_INTENT_TOOLS.values() for tool in tools
    } - set(GENERAL_TOOLS)

    assert all(name in text for name in GENERAL_TOOLS)
    assert not [name for name in invisible | {"read_user_profile"} if name in text]
