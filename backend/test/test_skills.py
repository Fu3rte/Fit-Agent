import asyncio
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.permissions import create_before_tool_call
from app.agent.skills import (
    SKILLS_ROOT,
    SkillLoadError,
    SkillPathError,
    load_skills,
    resolve_skill_path,
)
from app.agent.tool import run_tool_call
from app.agent.tools.files import create_file_tools
from app.ai.messages import ToolCall
from test.regression_support import TEST_SESSION, run_tool, temporary_root, text

EXPECTED_SKILLS = {
    "exercise-guidance",
    "fitness-knowledge",
    "plan-adjustment",
    "plan-evaluation",
    "strength-training",
    "training-theory",
    "workout-logging",
    "workout-planning",
}


def new_root() -> Path:
    root = temporary_root("skills-check") / uuid4().hex
    root.mkdir(parents=True)
    return root


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = "skill",
    description: str = "说明",
) -> Path:
    target = root / directory
    target.mkdir(parents=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    lines.extend([f"description: {description}", "---", "# 正文", ""])
    entry = target / "SKILL.md"
    entry.write_text("\n".join(lines), encoding="utf-8")
    return entry


def write_raw_skill(root: Path, directory: str, frontmatter: str) -> Path:
    target = root / directory
    target.mkdir(parents=True)
    entry = target / "SKILL.md"
    entry.write_text(f"---\n{frontmatter}---\n# 正文\n", encoding="utf-8")
    return entry


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        link.symlink_to(target, target_is_directory=True)


def read(tools: dict, path: str, **arguments):
    return run_tool(tools["read"], {"path": path, **arguments})


def invoke(tools: dict, name: str, arguments: dict):
    return asyncio.run(
        run_tool_call(
            ToolCall(type="toolCall", id=str(uuid4()), name=name, arguments=arguments),
            tools=tools,
            declared={key: value.definition() for key, value in tools.items()},
            before_tool_call=create_before_tool_call(),
        )
    )


def test_real_skill_catalog_is_stable_and_readable(monkeypatch):
    monkeypatch.chdir(new_root())
    first = load_skills()
    second = load_skills()
    assert first == second
    assert {skill.name for skill in first} == EXPECTED_SKILLS
    assert [skill.name for skill in first] == sorted(skill.name for skill in first)
    assert all(skill.description.strip() for skill in first)
    assert {skill.path for skill in first} == {
        f"skills/{name}/SKILL.md" for name in EXPECTED_SKILLS
    }
    assert SKILLS_ROOT.is_absolute() and SKILLS_ROOT.is_dir()

    tools = create_file_tools(TEST_SESSION, tmp_root=new_root())
    for skill in first:
        message = read(tools, skill.path)
        assert message.is_error is False, text(message)
        assert "description:" in text(message)

    theory = next(skill for skill in first if skill.name == "training-theory")
    window = read(tools, theory.path, offset=1, limit=2)
    assert window.is_error is False, text(window)
    assert text(window).startswith("1: ---\n2: name: training-theory\n")
    assert "继续读取请使用 offset=3" in text(window)
    beyond = read(tools, theory.path, offset=100000)
    assert beyond.is_error is False and "末尾之后" in text(beyond)

    shared = "skills/training-theory/references/methodology.md"
    message = read(tools, shared, limit=1)
    assert message.is_error is False, text(message)
    assert text(message).startswith("1: ")

    planning = read(tools, "skills/workout-planning/SKILL.md")
    assert planning.is_error is False
    assert "../training-theory/references/methodology.md" in text(planning)


def test_loader_orders_by_name_and_rejects_duplicates():
    root = new_root()
    write_skill(root, "zeta", name="zeta")
    write_skill(root, "alpha", name="alpha")
    assert [skill.name for skill in load_skills(root)] == ["alpha", "zeta"]

    duplicate = new_root()
    write_skill(duplicate, "one", name="same")
    write_skill(duplicate, "two", name="same")
    with pytest.raises(SkillLoadError, match="重复"):
        load_skills(duplicate)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "description: 只有描述\n",
        'name: ""\ndescription: 空名称\n',
        "name: [列表]\ndescription: 错误类型\n",
        "name: 缺少描述\n",
        'name: 空描述\ndescription: "   "\n',
        "name: 错误描述\ndescription: [列表]\n",
        "name: [未闭合\n",
    ],
)
def test_loader_rejects_invalid_frontmatter(frontmatter):
    root = new_root()
    write_raw_skill(root, "broken", frontmatter)
    with pytest.raises(SkillLoadError):
        load_skills(root)


def test_loader_rejects_missing_entry():
    root = new_root()
    (root / "broken").mkdir()
    with pytest.raises(SkillLoadError, match="缺少 SKILL.md"):
        load_skills(root)


def test_loader_rejects_invalid_entry_paths():
    root = new_root()
    (root / "directory-entry" / "SKILL.md").mkdir(parents=True)
    with pytest.raises(SkillLoadError, match="入口不是文件"):
        load_skills(root)

    with pytest.raises(SkillLoadError, match="不存在"):
        load_skills(root / "missing")

    with pytest.raises(SkillLoadError, match="绝对路径"):
        load_skills(Path("skills"))


def test_loader_rejects_linked_skill_directory_and_root():
    outside = new_root()
    write_skill(outside, "external", name="external")
    root = new_root()
    directory_link(root / "external", outside / "external")
    with pytest.raises(SkillLoadError, match="链接"):
        load_skills(root)

    linked_root = new_root()
    directory_link(linked_root / "skills", root)
    with pytest.raises(SkillLoadError, match="链接"):
        load_skills(linked_root / "skills")


def test_read_namespace_boundaries():
    root = new_root()
    skills_root = root / "skills"
    write_skill(skills_root, "alpha", name="alpha", description="动作说明")
    references = skills_root / "alpha" / "references"
    references.mkdir()
    (references / "note.md").write_text("# 引用\n", encoding="utf-8")
    outside = new_root()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    directory_link(skills_root / "linked", outside)

    tools = create_file_tools(
        TEST_SESSION, tmp_root=root / "tmp", skills_root=skills_root
    )
    assert read(tools, "skills/alpha/SKILL.md").is_error is False
    assert read(tools, "skills\\alpha\\references\\note.md").is_error is False
    assert read(tools, " skills/alpha/SKILL.md\n  ").is_error is False

    rejected = {
        "skills/../outside/secret.md": "穿越",
        "skills/alpha/../../outside/secret.md": "穿越",
        "skills/linked/secret.md": "链接",
        "skills/alpha/SKILL.txt": "Markdown",
        "skills": "命名空间",
        "/etc/passwd": "只允许",
        "C:/Windows/win.ini": "只允许",
        "\\\\server\\share\\x.md": "只允许",
    }
    for path, fragment in rejected.items():
        message = read(tools, path)
        assert message.is_error is True, path
        assert fragment in text(message), (path, text(message))


def test_resolve_skill_path_stays_within_root():
    root = new_root()
    write_skill(root, "alpha", name="alpha")
    resolved = resolve_skill_path("skills/alpha/SKILL.md", root)
    assert resolved.is_relative_to(root.resolve())
    assert resolve_skill_path(" skills/alpha/SKILL.md\n", root) == resolved
    for path in ("skills/../outside.md", "skills/alpha/../../outside.md"):
        with pytest.raises(SkillPathError):
            resolve_skill_path(path, root)


def test_skills_are_read_only_and_session_boundaries_hold():
    root = new_root()
    skills_root = root / "skills"
    entry = write_skill(skills_root, "alpha", name="alpha")
    tools = create_file_tools(
        TEST_SESSION, tmp_root=root / "tmp", skills_root=skills_root
    )
    message = read(tools, "skills/alpha/SKILL.md")
    assert message.is_error is False, text(message)
    assert "正文" in text(message)
    original = entry.read_text(encoding="utf-8")

    for message in (
        invoke(tools, "write", {"path": "skills/alpha/SKILL.md", "content": "changed"}),
        invoke(
            tools,
            "edit",
            {
                "path": "skills/alpha/SKILL.md",
                "old_text": "正文",
                "new_text": "changed",
            },
        ),
        invoke(tools, "write", {"path": "skills/alpha/new.md", "content": "new"}),
    ):
        assert message.is_error is True, text(message)
    assert entry.read_text(encoding="utf-8") == original
    assert not (skills_root / "alpha" / "new.md").exists()

    workspace = f"sessions/{TEST_SESSION}/workspace"
    message = invoke(tools, "write", {"path": f"{workspace}/note.md", "content": "note"})
    assert message.is_error is False, text(message)
    workspace_file = root / "tmp" / workspace / "note.md"
    assert workspace_file.read_text(encoding="utf-8") == "note"

    attachments = root / "tmp" / "sessions" / TEST_SESSION / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "plan.md").write_text("原始计划", encoding="utf-8")
    message = read(tools, f"sessions/{TEST_SESSION}/attachments/plan.md")
    assert message.is_error is False and "原始计划" in text(message)

    other = str(uuid4())
    other_workspace = root / "tmp" / "sessions" / other / "workspace"
    other_workspace.mkdir(parents=True)
    (other_workspace / "secret.md").write_text("secret", encoding="utf-8")
    message = read(tools, f"sessions/{other}/workspace/secret.md")
    assert message.is_error is True and "当前会话" in text(message)
