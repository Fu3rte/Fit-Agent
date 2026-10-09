import os
import re
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tool import (
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from app.ai.messages import TextContent
from app.domain.session.attachments import TMP_ROOT, validate_uuid

SESSION_DIRECTORY = "sessions"
WORKSPACE_DIRECTORY = "workspace"
EXCLUDED = {".git", ".venv", "node_modules", "__pycache__"}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


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


class WorkspaceAccessDenied(PermissionError):
    pass


def is_linked(path: Path) -> bool:
    # 符号链接、Windows junction 及 reparse point 都不能作为路径组件。
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(attributes, "st_file_attributes", 0) & _REPARSE_POINT)


def reject_links(root: Path, relative: Path) -> None:
    current = root
    if is_linked(current):
        raise WorkspaceAccessDenied("路径不得包含链接或 reparse point")
    for part in relative.parts:
        current = current / part
        if is_linked(current):
            raise WorkspaceAccessDenied("路径不得包含链接或 reparse point")


class Workspace:
    """当前会话在统一 tmp 根下的读写边界。

    相对路径以 tmp 根为基准；读取限于当前会话附件与工作文件，写入限于当前会话 workspace。
    会话身份来自后端可信上下文，不通过工具参数传入。
    """

    def __init__(self, session_id: str, tmp_root: Path = TMP_ROOT) -> None:
        self._session_id = validate_uuid(session_id)
        root = Path(tmp_root)
        if root.is_symlink() or root.is_junction():
            raise PermissionError("tmp 根目录不得为链接")
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve(strict=True)
        reject_links(self._root, Path())
        self.session_root = self._root / SESSION_DIRECTORY / self._session_id
        self.workspace_root = self.session_root / WORKSPACE_DIRECTORY

    @property
    def root(self) -> Path:
        return self._root

    def relative(self, path: str) -> Path:
        relative = Path(path)
        if relative.anchor or ".." in relative.parts:
            raise WorkspaceAccessDenied("只允许 tmp 根目录内的相对路径")
        return relative

    def read_path(self, path: str) -> Path:
        return self._within(self.relative(path), self.session_root, "只允许访问当前会话的附件与工作文件")

    def write_path(self, path: str) -> Path:
        return self._within(self.relative(path), self.workspace_root, "write 和 edit 只允许操作当前会话 workspace")

    def _within(self, relative: Path, allowed: Path, message: str) -> Path:
        reject_links(self._root, relative)
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(allowed):
            raise WorkspaceAccessDenied(message)
        return resolved

    def relative_to_root(self, target: Path) -> str:
        return target.relative_to(self._root).as_posix()


class FilePermissionContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    workspace: Workspace


def check_file_permission(context: BeforeToolCallContext) -> BeforeToolCallResult | None:
    if not isinstance(context.trusted_context, FilePermissionContext):
        raise TypeError("文件工具缺少可信 Workspace 绑定")
    if not isinstance(context.arguments, (WriteArguments, EditArguments)):
        raise TypeError("文件写入参数类型不一致")
    try:
        context.trusted_context.workspace.write_path(context.arguments.path)
    except WorkspaceAccessDenied as error:
        return BeforeToolCallResult(block=True, reason=str(error))
    return None


def iter_files(workspace: Workspace, path: str):
    target = workspace.read_path(path)
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        raise NotADirectoryError(target)
    for directory, dirs, names in os.walk(target, onerror=raise_walk_error):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in EXCLUDED and not is_linked(Path(directory) / name)
        )
        for name in sorted(names):
            candidate = Path(directory) / name
            if is_linked(candidate):
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(workspace.session_root):
                raise PermissionError("搜索结果超出当前会话")
            yield resolved


def create_file_tools(
    session_id: str, *, tmp_root: Path = TMP_ROOT
) -> dict[str, AgentTool]:
    workspace = Workspace(session_id, tmp_root)

    def read(tool_call_id, params: ReadArguments, signal, on_update) -> AgentToolResult:
        target = workspace.read_path(params.path)
        collected: list[tuple[int, str]] = []
        following = False
        with target.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if number < params.offset:
                    continue
                if len(collected) == params.limit:
                    following = True
                    break
                collected.append((number, line))
        if not collected:
            text = f"[无内容：offset {params.offset} 位于文件末尾之后]"
        else:
            text = "".join(f"{number}: {line.rstrip()}\n" for number, line in collected)
            if following:
                text += (
                    f"\n[仅显示到第 {collected[-1][0]} 行，文件仍有后续内容；"
                    f"继续读取请使用 offset={collected[-1][0] + 1}]"
                )
        return AgentToolResult([TextContent(type="text", text=text)])

    def write(tool_call_id, params: WriteArguments, signal, on_update) -> AgentToolResult:
        target = workspace.write_path(params.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = workspace.write_path(params.path)
        with target.open("w", encoding="utf-8") as stream:
            stream.write(params.content)
        text = f"已写入 {workspace.relative_to_root(target)}"
        return AgentToolResult([TextContent(type="text", text=text)])

    def edit(tool_call_id, params: EditArguments, signal, on_update) -> AgentToolResult:
        target = workspace.write_path(params.path)
        content = target.read_text(encoding="utf-8")
        if content.count(params.old_text) != 1:
            raise ValueError("old_text 必须在文件中精确匹配一次")
        target.write_text(
            content.replace(params.old_text, params.new_text, 1), encoding="utf-8"
        )
        text = f"已编辑 {workspace.relative_to_root(target)}"
        return AgentToolResult([TextContent(type="text", text=text)])

    def find(tool_call_id, params: FindArguments, signal, on_update) -> AgentToolResult:
        matches = []
        for candidate in iter_files(workspace, params.path):
            relative = candidate.relative_to(workspace.root)
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
        for candidate in iter_files(workspace, params.path):
            relative = candidate.relative_to(workspace.root)
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
        target = workspace.read_path(params.path)
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
            "读取 UTF-8 文本；offset 从 1 开始，输出带行号并提示是否仍有后续内容。"
            "相对路径以项目根目录 tmp 为根，只允许读取当前会话的附件与工作文件。",
            ReadArguments,
            read,
            execution_mode="parallel",
        ),
        AgentTool(
            "edit",
            "精确替换唯一一处 old_text；直接写入文件。"
            "相对路径以项目根目录 tmp 为根，只允许编辑当前会话 workspace 内的文件。",
            EditArguments,
            edit,
            execution_mode="sequential",
            trusted_context=FilePermissionContext(workspace=workspace),
        ),
        AgentTool(
            "write",
            "写入 UTF-8 文本，创建父目录，覆盖已有文件。"
            "相对路径以项目根目录 tmp 为根，只允许写入当前会话 workspace。",
            WriteArguments,
            write,
            execution_mode="sequential",
            trusted_context=FilePermissionContext(workspace=workspace),
        ),
        AgentTool(
            "grep",
            "用 regex 搜索 UTF-8 文件；glob 用于筛选文件；只允许访问当前会话的附件与工作文件。",
            GrepArguments,
            grep,
            execution_mode="parallel",
        ),
        AgentTool(
            "find",
            "按相对项目根目录 tmp 的 glob 查找文件；只允许访问当前会话的附件与工作文件。",
            FindArguments,
            find,
            execution_mode="parallel",
        ),
        AgentTool(
            "ls",
            "列出目录的直接子项；只允许访问当前会话的附件与工作文件。",
            LsArguments,
            ls,
            execution_mode="parallel",
        ),
    ]
    return {tool.name: tool for tool in tools}


def raise_walk_error(error: OSError) -> None:
    raise error
