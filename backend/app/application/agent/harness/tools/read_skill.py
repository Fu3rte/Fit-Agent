from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from app.application.agent.contracts import SkillMetadata
from app.application.ports import SkillSource


class ReadSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(min_length=1, max_length=64)


class ReadSkillReferenceArgs(ReadSkillArgs):
    relative_path: str = Field(min_length=1, max_length=256)


def skill_reading_tools(
    source: SkillSource, allowed: tuple[SkillMetadata, ...]
) -> tuple[BaseTool, BaseTool]:
    """闭包将模型参数约束在本 Intent 授权的 Skill 名称集合内。"""
    allowed_names = frozenset(item.name for item in allowed)

    @tool(args_schema=ReadSkillArgs)
    def read_skill(skill_name: str) -> str:
        """读取指定允许 Skill 的 SKILL.md 正文；引用文档需单独读取。"""
        if skill_name not in allowed_names:
            raise ValueError(f"当前 Intent 不允许读取 Skill：{skill_name!r}")
        return source.read_skill(skill_name)

    @tool(args_schema=ReadSkillReferenceArgs)
    def read_skill_reference(skill_name: str, relative_path: str) -> str:
        """读取 Skill 正文明确引用且任务需要的单个 references/*.md 文件。"""
        if skill_name not in allowed_names:
            raise ValueError(f"当前 Intent 不允许读取 Skill：{skill_name!r}")
        return source.read_reference(skill_name, relative_path).text

    return read_skill, read_skill_reference
