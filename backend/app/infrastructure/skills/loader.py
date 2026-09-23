from pathlib import Path, PurePosixPath
from typing import TextIO

from app.application.agent.contracts import SkillMetadata, SkillReference

SKILL_FILE_NAME = "SKILL.md"


class SkillError(ValueError):
    """Skill 加载失败。"""


class UnknownSkillError(SkillError):
    """请求加载的名称不在启动扫描出的元数据清单里。"""


class MissingSkillFileError(SkillError):
    """Skill 目录缺少 ``SKILL.md``，或明确请求的文件不存在。"""


class InvalidSkillMetadataError(SkillError):
    """frontmatter 缺失、未闭合，或 ``name``／``description`` 不是非空文本。"""


class SkillLoader:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._scanned: dict[str, tuple[SkillMetadata, Path]] | None = None

    def list_metadata(self) -> tuple[SkillMetadata, ...]:
        """启动扫描得到名称、描述与可解析的 Skill 位置。"""
        return tuple(entry[0] for entry in self._scan().values())

    def read_skill(self, name: str) -> str:
        """按已扫描名称读取 SKILL.md 正文，不读取 references。"""
        return _read_body(self._entry(name)[1])

    def read_reference(self, name: str, relative_path: str) -> SkillReference:
        """按 Skill 名与明确相对路径读取单个 reference。"""
        skill_file = self._entry(name)[1]
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or "\\" in relative_path
            or not relative_path.startswith("references/")
            or any(part in ("", ".", "..") for part in relative.parts)
            or relative.suffix != ".md"
        ):
            raise ValueError(f"非法 Skill reference 相对路径：{relative_path!r}")
        skill_dir = skill_file.parent.resolve(strict=True)
        target = (skill_dir / Path(*relative.parts)).resolve(strict=True)
        if not target.is_relative_to(skill_dir) or not target.is_file():
            raise ValueError(f"Skill reference 路径越界或不是文件：{relative_path!r}")
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise MissingSkillFileError(
                f"Skill {name!r} 的 reference 不存在：{relative_path}"
            ) from error
        return SkillReference(path=relative_path, text=text)

    def _entry(self, name: str) -> tuple[SkillMetadata, Path]:
        entry = self._scan().get(name)
        if entry is None:
            known = "、".join(sorted(self._scan())) or "（无）"
            raise UnknownSkillError(f"未知 Skill：{name!r}；已扫描到的 Skill：{known}")
        return entry

    def _scan(self) -> dict[str, tuple[SkillMetadata, Path]]:
        if self._scanned is None:
            self._scanned = self._scan_root()
        return self._scanned

    def _scan_root(self) -> dict[str, tuple[SkillMetadata, Path]]:
        if not self._root.is_dir():
            raise MissingSkillFileError(f"Skill 根目录不存在：{self._root}")
        scanned: dict[str, tuple[SkillMetadata, Path]] = {}
        root = self._root.resolve(strict=True)
        for entry in sorted(path for path in root.iterdir() if path.is_dir()):
            directory = entry.resolve(strict=True)
            if not directory.is_relative_to(root):
                raise InvalidSkillMetadataError(f"Skill 目录越界：{entry}")
            skill_file = directory / SKILL_FILE_NAME
            if not skill_file.is_file():
                raise MissingSkillFileError(f"Skill 目录缺少 {SKILL_FILE_NAME}：{directory}")
            skill_file = skill_file.resolve(strict=True)
            if not skill_file.is_relative_to(directory):
                raise InvalidSkillMetadataError(f"{SKILL_FILE_NAME} 路径越界：{entry}")
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
