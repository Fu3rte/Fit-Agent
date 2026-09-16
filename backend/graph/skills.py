"""Skill 启动元数据扫描与按名加载（讨论总结 §6、REFACTOR_PLAN §8.3、stage3.md §6）。

渐进披露的两个阶段（讨论总结 §6.2、REFACTOR_PLAN §8.3）：

- **启动只给元数据**：``SkillLoader.discover`` 只流式读 ``skills/*/SKILL.md`` 的 frontmatter，
  解析并暴露 ``name`` 与 ``description``；正文与 ``references/`` 下的文件在启动阶段既不读入也
  不进入上下文。
- **命中才加载**：``SkillLoader.load(name)`` 只读命中那一个 Skill 的正文，以及正文里明确引用的
  reference 文件；不全量加载两个 Skill，也不按名字猜别的知识来源。

三条显式失败边界（stage3.md §6／§7）：未知 Skill、缺失文件（Skill 目录没有 ``SKILL.md``，或正文
引用的 reference 不存在）、frontmatter 元数据非法。三者都在加载期抛出 ``SkillError``，不静默跳过、
不换成另一个 Skill。

本模块只是加载器：Router／节点接线与实际工具绑定随 Stage 4 完成（stage3.md §2.4）。本层不调模型、
不碰数据库、不解释 Skill 正文的语义。
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

#: Skill 定义文件名（讨论总结 §6.1 目录结构：``skills/<skill-name>/SKILL.md``）。
SKILL_FILE_NAME = "SKILL.md"

#: 本仓库内置 Skill 根目录：``backend/skills``（讨论总结 §6.1、REFACTOR_PLAN §4）。
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

#: 正文里显式引用的 reference 文件：只认 ``references/<name>.md`` 形式（Skill 目录内相对路径，
#: 不以 ``/`` 开头、不带上级目录）的引用，且前面不能是路径字符——因此 ``<其他仓库>/references/x.md``
#: 这种跨仓库来源不会被误当成待加载文件。
_REFERENCE_PATTERN = re.compile(r"(?<![\w./-])references/[0-9A-Za-z._\-/]*\.md")


class SkillError(ValueError):
    """Skill 加载失败（未知 Skill、缺文件、元数据非法）：显式失败，不静默兜底。"""


class UnknownSkillError(SkillError):
    """请求加载的名称不在启动扫描出的元数据清单里。"""


class MissingSkillFileError(SkillError):
    """Skill 目录缺少 ``SKILL.md``，或正文引用的 reference 文件不存在。"""


class InvalidSkillMetadataError(SkillError):
    """frontmatter 缺失、未闭合，或 ``name``／``description`` 不是非空文本。"""


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """启动阶段暴露的全部 Skill 信息：只有 ``name`` 与 ``description``。"""

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SkillReference:
    """命中后加载的一个 reference 文件：``path`` 是 Skill 目录内的相对路径。"""

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """一次 ``load`` 的结果：命中的元数据、正文与正文明确引用的 reference 文件。"""

    metadata: SkillMetadata
    body: str
    references: tuple[SkillReference, ...]


class SkillLoader:
    """扫描 ``skills/*/SKILL.md`` 元数据，并在命中后按名加载正文与引用文件。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = SKILLS_DIR if root is None else Path(root)
        self._scanned: dict[str, tuple[SkillMetadata, Path]] | None = None

    def discover(self) -> tuple[SkillMetadata, ...]:
        """启动扫描（每个实例只扫描一次）：返回全部 Skill 的 ``name`` 与 ``description``。"""
        return tuple(metadata for metadata, _ in self._scan().values())

    def load(self, name: str) -> LoadedSkill:
        """按名加载命中的 Skill 正文与它明确引用的 reference 文件。

        未知名称即 :class:`UnknownSkillError`；引用的 reference 缺失即
        :class:`MissingSkillFileError`——两者都不退化成「加载别的 Skill」。
        """
        scanned = self._scan()
        entry = scanned.get(name)
        if entry is None:
            known = "、".join(sorted(scanned)) or "（无）"
            raise UnknownSkillError(f"未知 Skill：{name!r}；已扫描到的 Skill：{known}")
        metadata, skill_file = entry
        body = _read_body(skill_file)
        return LoadedSkill(
            metadata=metadata,
            body=body,
            references=tuple(_load_references(skill_file.parent, body)),
        )

    def _scan(self) -> dict[str, tuple[SkillMetadata, Path]]:
        """扫描一次并缓存：Skill 名称 →（元数据，``SKILL.md`` 路径）。"""
        if self._scanned is None:
            self._scanned = self._scan_root()
        return self._scanned

    def _scan_root(self) -> dict[str, tuple[SkillMetadata, Path]]:
        if not self._root.is_dir():
            raise MissingSkillFileError(f"Skill 根目录不存在：{self._root}")
        scanned: dict[str, tuple[SkillMetadata, Path]] = {}
        for directory in sorted(path for path in self._root.iterdir() if path.is_dir()):
            skill_file = directory / SKILL_FILE_NAME
            if not skill_file.is_file():
                raise MissingSkillFileError(f"Skill 目录缺少 {SKILL_FILE_NAME}：{directory}")
            metadata = parse_skill_metadata(skill_file)
            if metadata.name in scanned:
                raise InvalidSkillMetadataError(f"Skill 名称重复：{metadata.name!r}")
            scanned[metadata.name] = (metadata, skill_file)
        return scanned


def parse_skill_metadata(path: Path) -> SkillMetadata:
    """只解析 ``SKILL.md`` frontmatter 的 ``name``／``description``；正文不读入、不暴露。"""
    fields = _frontmatter_fields(path)
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name or not description:
        raise InvalidSkillMetadataError(
            f"{path} 的 frontmatter 必须含非空 name 与 description"
        )
    return SkillMetadata(name=name, description=description)


def _frontmatter_fields(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as handle:
        lines = _frontmatter_lines(handle, path)
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise InvalidSkillMetadataError(f"{path} 的 frontmatter 行无法解析：{line.rstrip()!r}")
        fields[key.strip()] = value.strip()
    return fields


def _read_body(path: Path) -> str:
    """读正文：先跳过（并顺带校验）frontmatter，再取其余文本。"""
    with path.open("r", encoding="utf-8") as handle:
        _frontmatter_lines(handle, path)
        return handle.read()


def _frontmatter_lines(handle: TextIO, path: Path) -> list[str]:
    """流式读出 frontmatter 的行并停在闭合分隔符，正文留给调用方决定是否读取。"""
    if handle.readline().strip() != "---":
        raise InvalidSkillMetadataError(f"{path} 缺少 frontmatter 起始分隔符 '---'")
    lines: list[str] = []
    for line in handle:
        if line.strip() == "---":
            return lines
        lines.append(line)
    raise InvalidSkillMetadataError(f"{path} 的 frontmatter 未闭合")


def _load_references(skill_dir: Path, body: str) -> Iterator[SkillReference]:
    """按正文出现顺序加载显式引用的 reference 文件；缺失即显式失败。"""
    for relative in _referenced_paths(body):
        target = skill_dir / relative
        if not target.is_file():
            raise MissingSkillFileError(f"Skill {skill_dir.name!r} 引用的文件不存在：{relative}")
        yield SkillReference(path=relative, text=target.read_text(encoding="utf-8"))


def _referenced_paths(body: str) -> tuple[str, ...]:
    """正文里出现的 reference 相对路径，按首次出现顺序去重。"""
    seen: dict[str, None] = {}
    for match in _REFERENCE_PATTERN.finditer(body):
        seen.setdefault(match.group(0), None)
    return tuple(seen)
