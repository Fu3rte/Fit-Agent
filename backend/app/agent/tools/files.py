import os
import re
from itertools import islice
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tool import AgentTool, AgentToolResult
from app.ai.messages import TextContent

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


def create_file_tools() -> dict[str, AgentTool]:
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

    def read(tool_call_id, params: ReadArguments, signal, on_update) -> AgentToolResult:
        with resolve(params.path).open(encoding="utf-8") as stream:
            lines = islice(
                enumerate(stream, 1),
                params.offset - 1,
                params.offset + params.limit - 1,
            )
            text = "".join(f"{number}: {line.rstrip()}\n" for number, line in lines)
        return AgentToolResult([TextContent(type="text", text=text)])

    def write(tool_call_id, params: WriteArguments, signal, on_update) -> AgentToolResult:
        target = resolve(params.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(params.content, encoding="utf-8")
        text = f"已写入 {target.relative_to(root).as_posix()}"
        return AgentToolResult([TextContent(type="text", text=text)])

    def edit(tool_call_id, params: EditArguments, signal, on_update) -> AgentToolResult:
        target = resolve(params.path)
        content = target.read_text(encoding="utf-8")
        if content.count(params.old_text) != 1:
            raise ValueError("old_text 必须在文件中精确匹配一次")
        target.write_text(
            content.replace(params.old_text, params.new_text, 1), encoding="utf-8"
        )
        text = f"已编辑 {target.relative_to(root).as_posix()}"
        return AgentToolResult([TextContent(type="text", text=text)])

    def find(tool_call_id, params: FindArguments, signal, on_update) -> AgentToolResult:
        matches = []
        for candidate in files(params.path):
            relative = candidate.relative_to(root)
            if relative.full_match(params.pattern):
                matches.append(relative.as_posix())
                if len(matches) == params.limit:
                    text = "\n".join(matches) + "\n[已达到 limit]"
                    return AgentToolResult([TextContent(type="text", text=text)])
        text = "\n".join(matches)
        return AgentToolResult([TextContent(type="text", text=text)])

    def grep(tool_call_id, params: GrepArguments, signal, on_update) -> AgentToolResult:
        expression = re.compile(params.pattern)
        matches = []
        for candidate in files(params.path):
            relative = candidate.relative_to(root)
            if not relative.full_match(params.glob):
                continue
            with candidate.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if expression.search(line):
                        matches.append(
                            f"{relative.as_posix()}:{number}: {line.rstrip()}"
                        )
                        if len(matches) == params.limit:
                            text = "\n".join(matches) + "\n[已达到 limit]"
                            return AgentToolResult([TextContent(type="text", text=text)])
        text = "\n".join(matches)
        return AgentToolResult([TextContent(type="text", text=text)])

    def ls(tool_call_id, params: LsArguments, signal, on_update) -> AgentToolResult:
        target = resolve(params.path)
        entries = sorted(target.iterdir(), key=lambda item: item.name)
        text = "\n".join(
            item.name + ("/" if item.is_dir() else "") for item in entries[: params.limit]
        )
        if len(entries) > params.limit:
            text += "\n[输出截断：已达到 limit]"
        return AgentToolResult([TextContent(type="text", text=text)])

    tools = [
        AgentTool(
            "read",
            "读取 UTF-8 文本；offset 从 1 开始，输出带行号。",
            ReadArguments,
            read,
            execution_mode="parallel",
        ),
        AgentTool(
            "edit",
            "精确替换唯一一处 old_text；直接写入文件。",
            EditArguments,
            edit,
            execution_mode="sequential",
        ),
        AgentTool(
            "write",
            "写入 UTF-8 文本，创建父目录，覆盖已有文件。",
            WriteArguments,
            write,
            execution_mode="sequential",
        ),
        AgentTool(
            "grep",
            "用 regex 搜索 UTF-8 文件；glob 用于筛选文件。",
            GrepArguments,
            grep,
            execution_mode="parallel",
        ),
        AgentTool(
            "find",
            "按 workspace 相对路径的 glob 查找文件，如 **/*.py。",
            FindArguments,
            find,
            execution_mode="parallel",
        ),
        AgentTool(
            "ls",
            "列出目录的直接子项。",
            LsArguments,
            ls,
            execution_mode="parallel",
        ),
    ]
    return {tool.name: tool for tool in tools}


def raise_walk_error(error: OSError) -> None:
    raise error
