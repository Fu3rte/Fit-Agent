import os
import re
from itertools import islice
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tool import Tool, ToolExecutionResult

WORKSPACE = Path(__file__).resolve().parents[3] / "temp"


class PathArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = "."


class ReadArguments(PathArguments):
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=200, ge=1, le=2000)


class WriteArguments(PathArguments):
    content: str


class EditArguments(PathArguments):
    old_text: str = Field(min_length=1)
    new_text: str


class FindArguments(PathArguments):
    pattern: str
    limit: int = Field(default=100, ge=1, le=1000)


class GrepArguments(FindArguments):
    glob: str = "**/*"


class LsArguments(PathArguments):
    limit: int = Field(default=100, ge=1, le=1000)


def create_file_tools() -> dict[str, Tool]:
    if WORKSPACE.is_symlink() or WORKSPACE.is_junction():
        raise PermissionError("backend/temp 不能是链接")
    WORKSPACE.mkdir(exist_ok=True)
    root = WORKSPACE.resolve(strict=True)

    def resolve(path: str) -> Path:
        relative = Path(path)
        if relative.anchor or ".." in relative.parts:
            raise PermissionError("只允许 backend/temp 内的相对路径")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise PermissionError("路径超出 backend/temp")
        return target

    def files(path: str):
        target = resolve(path)
        if target.is_file():
            yield target
            return
        if not target.is_dir():
            raise NotADirectoryError(target)
        for directory, dirs, names in os.walk(target, onerror=raise_walk_error):
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in {".git", ".venv", "node_modules", "__pycache__"}
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                candidate = Path(directory) / name
                if not candidate.is_symlink():
                    yield resolve(str(candidate.relative_to(root)))

    def read(path: str, offset: int, limit: int) -> ToolExecutionResult:
        with resolve(path).open(encoding="utf-8") as stream:
            lines = islice(enumerate(stream, 1), offset - 1, offset + limit - 1)
            return ToolExecutionResult(
                "".join(f"{number}: {line.rstrip()}\n" for number, line in lines), False
            )

    def write(path: str, content: str) -> ToolExecutionResult:
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolExecutionResult(
            f"已写入 {target.relative_to(root).as_posix()}", False
        )

    def edit(path: str, old_text: str, new_text: str) -> ToolExecutionResult:
        target = resolve(path)
        content = target.read_text(encoding="utf-8")
        if content.count(old_text) != 1:
            raise ValueError("old_text 必须在文件中精确匹配一次")
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return ToolExecutionResult(
            f"已编辑 {target.relative_to(root).as_posix()}", False
        )

    def find(path: str, pattern: str, limit: int) -> ToolExecutionResult:
        matches = []
        for candidate in files(path):
            relative = candidate.relative_to(root)
            if relative.full_match(pattern):
                matches.append(relative.as_posix())
                if len(matches) == limit:
                    return ToolExecutionResult(
                        "\n".join(matches) + "\n[已达到 limit]", False
                    )
        return ToolExecutionResult("\n".join(matches), False)

    def grep(path: str, pattern: str, limit: int, glob: str) -> ToolExecutionResult:
        expression = re.compile(pattern)
        matches = []
        for candidate in files(path):
            relative = candidate.relative_to(root)
            if not relative.full_match(glob):
                continue
            with candidate.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if expression.search(line):
                        matches.append(
                            f"{relative.as_posix()}:{number}: {line.rstrip()}"
                        )
                        if len(matches) == limit:
                            return ToolExecutionResult(
                                "\n".join(matches) + "\n[已达到 limit]", False
                            )
        return ToolExecutionResult("\n".join(matches), False)

    def ls(path: str, limit: int) -> ToolExecutionResult:
        target = resolve(path)
        entries = sorted(target.iterdir(), key=lambda item: item.name)
        result = "\n".join(
            item.name + ("/" if item.is_dir() else "") for item in entries[:limit]
        )
        if len(entries) > limit:
            result += "\n[输出截断：已达到 limit]"
        return ToolExecutionResult(result, False)

    tools = [
        Tool(
            "read",
            "读取 UTF-8 文本；offset 从 1 开始，输出带行号。",
            ReadArguments,
            read,
        ),
        Tool("edit", "精确替换唯一一处 old_text；直接写入文件。", EditArguments, edit),
        Tool(
            "write",
            "写入 UTF-8 文本，创建父目录，覆盖已有文件。",
            WriteArguments,
            write,
        ),
        Tool(
            "grep", "用 regex 搜索 UTF-8 文件；glob 用于筛选文件。", GrepArguments, grep
        ),
        Tool(
            "find",
            "按 workspace 相对路径的 glob 查找文件，如 **/*.py。",
            FindArguments,
            find,
        ),
        Tool("ls", "列出目录的直接子项。", LsArguments, ls),
    ]
    return {tool.name: tool for tool in tools}


def raise_walk_error(error: OSError) -> None:
    raise error
