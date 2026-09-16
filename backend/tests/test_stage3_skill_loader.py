"""Stage 3 子任务 04：Skill 启动元数据扫描与按名加载（渐进披露）。

依据：``refactor-log/stage3.md`` §6／§7；讨论总结 §6；REFACTOR_PLAN §8.3。

覆盖：启动只暴露 ``name``／``description``（元数据结构冻结、不读正文与 reference 文件、每个实例只扫描
一次，且扫描出的元数据里不夹带正文措辞或 reference 文件名）；命中后只加载目标 Skill 的正文与正文明确引用的 reference 文件，不全量加载两个 Skill、不加载
跨仓库来源路径；未知 Skill、缺失文件（目录缺 ``SKILL.md``、引用的 reference 不存在）与非法 frontmatter
元数据都显式失败；两个真实 Skill 的正文可定位「可用工具／所需记忆／禁止事项／结构化输出要求／训练知识
来源」五节与各自的硬边界措辞。

本文件不调模型、不碰数据库；只用 pytest ``tmp_path`` 下的临时 Skill 目录与仓库内真实
``backend/skills`` 目录。
"""

from dataclasses import fields
from pathlib import Path

import pytest

from graph.skills import (
    SKILL_FILE_NAME,
    SKILLS_DIR,
    InvalidSkillMetadataError,
    MissingSkillFileError,
    SkillLoader,
    SkillMetadata,
    UnknownSkillError,
)

PLANNING = "workout-planning"
ADJUSTMENT = "plan-adjustment"

#: 每个 Skill 必须能定位的五节（stage3.md §6 第 3 项、§7 Skill 段）。
REQUIRED_SECTIONS = ("可用工具", "所需记忆", "禁止事项", "结构化输出要求", "训练知识来源")

ALPHA_BODY = "\n\n# alpha 正文\n\n[规则](references/rules.md)\n"


def _write_skill(
    root: Path, directory: str, text: str, references: dict[str, str] | None = None
) -> Path:
    """在临时根下写一个 Skill 目录：``SKILL.md`` ＋（可选）被引用的 reference 文件。"""
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_FILE_NAME).write_text(text, encoding="utf-8")
    for relative, content in (references or {}).items():
        target = skill_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def _skill_text(name: str, body: str) -> str:
    """一个合法 frontmatter ＋ 指定正文的 ``SKILL.md`` 文本。"""
    return f"---\nname: {name}\ndescription: {name} 描述\n---{body}"


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """记录调用期间被打开的文件路径（``Path.read_text`` 内部也走 ``Path.open``）。"""
    opened: list[Path] = []
    original = Path.open

    def tracked(self: Path, *args: object, **kwargs: object) -> object:
        opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked)
    return opened


def test_metadata_structure_is_frozen_to_name_and_description() -> None:
    """启动暴露的元数据只有两个字段：正文与 reference 内容没有承载位置。"""
    assert {field.name for field in fields(SkillMetadata)} == {"name", "description"}


def test_startup_scan_exposes_only_metadata_of_the_two_real_skills() -> None:
    """真实 ``backend/skills`` 目录扫描出恰好两个 Skill，且只有非空 name／description。"""
    metadata = SkillLoader(SKILLS_DIR).discover()

    assert {item.name for item in metadata} == {PLANNING, ADJUSTMENT}
    assert all(item.description.strip() for item in metadata)


def test_startup_scan_reads_frontmatter_only_and_caches_the_scan(
    tmp_path: Path, opened_paths: list[Path]
) -> None:
    """启动扫描只读 ``SKILL.md`` 的 frontmatter：不读正文引用到的 reference，且每个实例只扫描一次。"""
    _write_skill(
        tmp_path,
        "alpha",
        _skill_text("alpha", ALPHA_BODY),
        {"references/rules.md": "ALPHA-REF"},
    )
    loader = SkillLoader(tmp_path)
    opened_paths.clear()

    assert loader.discover() == (SkillMetadata(name="alpha", description="alpha 描述"),)
    assert [path.name for path in opened_paths] == [SKILL_FILE_NAME]

    opened_paths.clear()
    loader.discover()
    assert opened_paths == []


def test_real_startup_scan_opens_no_body_or_reference_file(
    opened_paths: list[Path],
) -> None:
    """真实 ``backend/skills`` 的启动扫描只打开两个 ``SKILL.md``：正文与 ``references/`` 都没被读，
    且扫描出的元数据里不夹带正文措辞或 reference 文件名。"""
    metadata = SkillLoader(SKILLS_DIR).discover()

    assert {(path.parent.name, path.name) for path in opened_paths} == {
        (PLANNING, SKILL_FILE_NAME),
        (ADJUSTMENT, SKILL_FILE_NAME),
    }
    for item in metadata:
        assert "references" not in item.description
        assert "planning-rules.md" not in item.description
        # 正文首句只可能出现在正文里，不进入启动元数据。
        assert "本 Skill 是**模型指令**" not in item.name + item.description


def test_loading_one_skill_loads_only_its_body_and_referenced_files(
    tmp_path: Path, opened_paths: list[Path]
) -> None:
    """命中 ``alpha`` 时只加载 alpha 正文与其引用的 reference，beta 的正文与 reference 都不落盘读取。"""
    alpha_dir = _write_skill(
        tmp_path,
        "alpha",
        _skill_text("alpha", ALPHA_BODY),
        {"references/rules.md": "ALPHA-REF"},
    )
    beta_dir = _write_skill(
        tmp_path,
        "beta",
        _skill_text("beta", ALPHA_BODY.replace("alpha", "beta")),
        {"references/rules.md": "BETA-REF"},
    )
    loader = SkillLoader(tmp_path)
    loader.discover()
    opened_paths.clear()

    loaded = loader.load("alpha")

    assert "alpha 正文" in loaded.body
    assert [reference.path for reference in loaded.references] == ["references/rules.md"]
    assert loaded.references[0].text == "ALPHA-REF"
    assert opened_paths == [alpha_dir / SKILL_FILE_NAME, alpha_dir / "references/rules.md"]
    assert beta_dir / SKILL_FILE_NAME not in opened_paths
    assert beta_dir / "references/rules.md" not in opened_paths


def test_only_self_relative_reference_paths_are_loaded(
    tmp_path: Path, opened_paths: list[Path]
) -> None:
    """只有 ``references/…md`` 形式的自身引用会被加载：跨仓库路径与目录名不算引用。"""
    body = (
        "\n\n# alpha 正文\n\n"
        "[本 Skill 规则](references/rules.md)\n"
        "[其他仓库来源](Lzheng-fitness/skills/other/references/rolling.md)\n"
        "见 `references/` 目录\n"
    )
    skill_dir = _write_skill(
        tmp_path, "alpha", _skill_text("alpha", body), {"references/rules.md": "R"}
    )
    loader = SkillLoader(tmp_path)
    loader.discover()
    opened_paths.clear()

    assert [reference.path for reference in loader.load("alpha").references] == [
        "references/rules.md"
    ]
    assert opened_paths == [skill_dir / SKILL_FILE_NAME, skill_dir / "references/rules.md"]


def test_unknown_skill_fails_explicitly(tmp_path: Path) -> None:
    """未知 Skill 名称显式失败，并列出已扫描到的名称，不静默换 Skill。"""
    _write_skill(tmp_path, "alpha", _skill_text("alpha", "\n\n正文\n"))

    with pytest.raises(UnknownSkillError) as excinfo:
        SkillLoader(tmp_path).load("beta")

    assert "beta" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)


def test_missing_referenced_file_fails_explicitly(tmp_path: Path) -> None:
    """正文引用的 reference 文件不存在时显式失败，不退化成空引用。"""
    _write_skill(
        tmp_path,
        "alpha",
        _skill_text("alpha", "\n\n[规则](references/gone.md)\n"),
    )

    with pytest.raises(MissingSkillFileError) as excinfo:
        SkillLoader(tmp_path).load("alpha")

    assert "references/gone.md" in str(excinfo.value)


def test_skill_directory_without_skill_file_fails_explicitly(tmp_path: Path) -> None:
    """Skill 目录缺少 ``SKILL.md`` 时扫描显式失败，不静默跳过该目录。"""
    (tmp_path / "broken").mkdir()

    with pytest.raises(MissingSkillFileError) as excinfo:
        SkillLoader(tmp_path).discover()

    assert "broken" in str(excinfo.value)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# 没有 frontmatter\n",
        "---\nname: alpha\n---\n",
        "---\ndescription: 描述\n---\n",
        "---\nname:\ndescription: 描述\n---\n",
        "---\nname alpha\ndescription: 描述\n---\n",
        "---\nname: alpha\ndescription: 描述\n",
    ],
)
def test_invalid_metadata_fails_explicitly(tmp_path: Path, text: str) -> None:
    """frontmatter 缺失、未闭合、缺字段、空值或无法解析都显式失败。"""
    _write_skill(tmp_path, "alpha", text)

    with pytest.raises(InvalidSkillMetadataError):
        SkillLoader(tmp_path).discover()


def test_real_skills_locatable_sections_and_hard_boundaries() -> None:
    """两个真实 Skill：planning 引用自己的 planning-rules，adjustment 不引用别人的文件，且各自声明硬边界。"""
    loader = SkillLoader(SKILLS_DIR)

    planning = loader.load(PLANNING)
    adjustment = loader.load(ADJUSTMENT)

    assert [reference.path for reference in planning.references] == [
        "references/planning-rules.md"
    ]
    assert adjustment.references == ()
    assert "训练知识来源" in planning.body and "来源登记" in planning.references[0].text
    assert "本地来源文件" in adjustment.body

    for loaded in (planning, adjustment):
        for section in REQUIRED_SECTIONS:
            assert section in loaded.body, (loaded.metadata.name, section)

    planning_text = planning.body + planning.references[0].text
    for phrase in ("待校准", "不从 PB 反推", "一次修订", "用户确认"):
        assert phrase in planning_text, phrase

    for phrase in ("不脱离旧计划重新生成", "用户确认"):
        assert phrase in adjustment.body, phrase
