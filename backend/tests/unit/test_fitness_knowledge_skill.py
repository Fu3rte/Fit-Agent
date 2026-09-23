# ST-04 fitness-knowledge Skill：真实 SkillLoader 校验正文、两个 reference 与 General 装载名称。

from app.application.agent.contracts import LoadedSkill
from app.application.agent.harness.registry import (
    GENERAL_INTENT_SKILLS,
    GENERAL_INTENT_TOOLS,
)
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir

SKILL_NAME = "fitness-knowledge"
REFERENCE_PATHS = ("references/knowledge-boundaries.md", "references/few-shots.md")
GENERAL_TOOLS = ("read_active_plan", "read_training_history", "search_exercises")


def _load() -> LoadedSkill:
    """生产装配使用的同一份 Skill 根目录与同一个加载器。"""
    return SkillLoader(skills_dir()).load(SKILL_NAME)


def test_metadata_and_both_references_load_through_the_real_loader() -> None:
    """General 装载矩阵里的名称唯一，真实加载器按该名命中正文与两个 reference。"""
    skill = _load()

    assert skill.metadata.name == SKILL_NAME and skill.metadata.description.strip()
    names = [name for names in GENERAL_INTENT_SKILLS.values() for name in names]
    assert SKILL_NAME in names
    assert tuple(ref.path for ref in skill.references) == REFERENCE_PATHS
    assert all(ref.text.strip() for ref in skill.references)


def test_skill_text_names_only_the_general_intent_tools() -> None:
    """正文与 reference 只点名本 Intent 可见的三项只读 Tool。"""
    assert {tool.name for tool in GENERAL_INTENT_TOOLS["general"]} == set(GENERAL_TOOLS)
    skill = _load()
    text = "\n".join((skill.body, *(ref.text for ref in skill.references)))
    invisible = {
        tool.name for tools in GENERAL_INTENT_TOOLS.values() for tool in tools
    } - set(GENERAL_TOOLS)

    assert all(name in text for name in GENERAL_TOOLS)
    assert not [name for name in invisible | {"read_user_profile"} if name in text]
