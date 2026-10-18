import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

SKILLS_DIRECTORY = "skills"
# 以源码位置定位固定根目录，不依赖进程工作目录。
SKILLS_ROOT = Path(__file__).resolve().parents[2] / SKILLS_DIRECTORY
SKILL_ENTRY = "SKILL.md"
SKILLS_NAMESPACE = "skills"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class SkillLoadError(ValueError):
    pass


class SkillPathError(PermissionError):
    pass


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: str


def load_skills(root: Path = SKILLS_ROOT) -> tuple[SkillMetadata, ...]:
    directory = Path(root)
    if not directory.is_absolute():
        raise SkillLoadError("Skills 根目录必须为绝对路径")
    if not directory.is_dir():
        raise SkillLoadError(f"Skills 根目录不存在或不是目录：{directory}")
    _reject_link(directory, "Skills 根目录")
    resolved_root = directory.resolve(strict=True)

    skills: list[SkillMetadata] = []
    names: set[str] = set()
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        _reject_link(child, "Skill 目录")
        if child.is_file():
            continue
        if not child.is_dir():
            raise SkillLoadError(f"Skill 路径不是目录：{child}")
        if any(character in child.name for character in (":", "\\", "\x00")):
            raise SkillLoadError(f"Skill 目录名无效：{child.name}")
        entry = child / SKILL_ENTRY
        if not entry.exists():
            raise SkillLoadError(f"Skill 目录缺少 {SKILL_ENTRY}：{child}")
        if not entry.is_file():
            raise SkillLoadError(f"Skill 入口不是文件：{entry}")
        _reject_link(entry, "Skill 入口")
        resolved_entry = entry.resolve(strict=True)
        if resolved_entry.parent != (resolved_root / child.name).resolve():
            raise SkillLoadError(f"Skill 入口不在固定根目录内：{entry}")

        name, description = _read_metadata(entry)
        if name in names:
            raise SkillLoadError(f"Skill 名称重复：{name}")
        names.add(name)
        skills.append(
            SkillMetadata(
                name=name,
                description=description,
                path=f"{SKILLS_NAMESPACE}/{child.name}/{SKILL_ENTRY}",
            )
        )
    return tuple(sorted(skills, key=lambda skill: skill.name))


def is_skill_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    return normalized == SKILLS_NAMESPACE or normalized.startswith(
        f"{SKILLS_NAMESPACE}/"
    )


def resolve_skill_path(path: str, root: Path = SKILLS_ROOT) -> Path:
    path = path.strip()
    if "\x00" in path or ":" in path:
        raise SkillPathError("Skills 路径非法")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        raise SkillPathError("Skills 路径必须为相对路径")
    parts = PurePosixPath(normalized).parts
    if len(parts) < 2 or parts[0] != SKILLS_NAMESPACE:
        raise SkillPathError(f"Skills 路径必须位于固定命名空间内：{SKILLS_NAMESPACE}/")
    relative_parts = parts[1:]
    if any(part in {".", ".."} for part in relative_parts):
        raise SkillPathError("Skills 路径不得包含路径穿越")
    if PurePosixPath(*relative_parts).suffix.lower() != ".md":
        raise SkillPathError("Skills 命名空间只允许读取 Markdown 文件")

    directory = Path(root)
    if not directory.is_absolute():
        raise SkillLoadError("Skills 根目录必须为绝对路径")
    _reject_link(directory, "Skills 根目录", SkillPathError)
    resolved_root = directory.resolve(strict=True)
    current = resolved_root
    for part in relative_parts:
        current = current / part
        _reject_link(current, "Skills 路径", SkillPathError)
    resolved = current.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise SkillPathError("Skills 路径越过固定根目录")
    return resolved


def _read_metadata(entry: Path) -> tuple[str, str]:
    content = entry.read_text(encoding="utf-8-sig")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillLoadError(f"Skill 缺少 YAML frontmatter：{entry}")
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}),
        None,
    )
    if end is None:
        raise SkillLoadError(f"Skill frontmatter 未闭合：{entry}")
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise SkillLoadError(f"Skill frontmatter 无法解析：{entry}") from error
    if not isinstance(data, dict):
        raise SkillLoadError(f"Skill frontmatter 必须为映射：{entry}")
    return (
        _required_text(data, "name", entry),
        _required_text(data, "description", entry),
    )


def _required_text(data: dict, field: str, entry: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SkillLoadError(f"Skill 的 {field} 必须为非空字符串：{entry}")
    return value.strip()


def _reject_link(
    path: Path, label: str, error: type[Exception] = SkillLoadError
) -> None:
    if _is_link(path):
        raise error(f"{label}不得为链接或 reparse point：{path}")


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(attributes, "st_file_attributes", 0) & _REPARSE_POINT)
