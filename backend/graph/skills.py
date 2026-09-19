import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

SKILL_FILE_NAME = "SKILL.md"

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

_REFERENCE_PATTERN = re.compile(r"(?<![\w./-])references/[0-9A-Za-z._\-/]*\.md")


class SkillError(ValueError):
    """Skill 加载失败。"""


class UnknownSkillError(SkillError):
    """请求加载的名称不在启动扫描出的元数据清单里。"""


class MissingSkillFileError(SkillError):
    """Skill 目录缺少 ``SKILL.md``，或正文引用的 reference 文件不存在。"""


class InvalidSkillMetadataError(SkillError):
    """frontmatter 缺失、未闭合，或 ``name``／``description`` 不是非空文本。"""


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SkillReference:
    path: str
    text: str


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    metadata: SkillMetadata
    body: str
    references: tuple[SkillReference, ...]


class SkillLoader:
    def __init__(self, root: Path | None = None) -> None:
        self._root = SKILLS_DIR if root is None else Path(root)
        self._scanned: dict[str, tuple[SkillMetadata, Path]] | None = None

    def load(self, name: str) -> LoadedSkill:
        """按名加载命中的 Skill 正文与它明确引用的 reference 文件。"""
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
    """只解析 ``SKILL.md`` frontmatter 的 ``name``／``description``。"""
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
    with path.open("r", encoding="utf-8") as handle:
        _frontmatter_lines(handle, path)
        return handle.read()


def _frontmatter_lines(handle: TextIO, path: Path) -> list[str]:
    if handle.readline().strip() != "---":
        raise InvalidSkillMetadataError(f"{path} 缺少 frontmatter 起始分隔符 '---'")
    lines: list[str] = []
    for line in handle:
        if line.strip() == "---":
            return lines
        lines.append(line)
    raise InvalidSkillMetadataError(f"{path} 的 frontmatter 未闭合")


def _load_references(skill_dir: Path, body: str) -> Iterator[SkillReference]:
    for relative in _referenced_paths(body):
        target = skill_dir / relative
        if not target.is_file():
            raise MissingSkillFileError(f"Skill {skill_dir.name!r} 引用的文件不存在：{relative}")
        yield SkillReference(path=relative, text=target.read_text(encoding="utf-8"))


def _referenced_paths(body: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for match in _REFERENCE_PATTERN.finditer(body):
        seen.setdefault(match.group(0), None)
    return tuple(seen)
