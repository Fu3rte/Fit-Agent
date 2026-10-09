import os
import shutil
import signal
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryFile
from threading import Event
from time import monotonic
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tool import AgentTool, AgentToolResult
from app.agent.tools.files import Workspace
from app.ai.messages import TextContent
from app.domain.session.attachments import TMP_ROOT

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


class BashArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1)
    timeout: float | None = Field(default=None, gt=0, le=2_147_483.647)


def resolve_bash() -> str:
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if directory := os.environ.get(variable):
                candidate = Path(directory) / "Git" / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
    shell = shutil.which("bash")
    if shell is None:
        raise FileNotFoundError("找不到 Bash，请安装 Bash 并配置 PATH")
    return shell


def format_output(stream: BinaryIO, output_dir: Path, reference_dir: str) -> str:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - MAX_BYTES - 2))
    data = stream.read(MAX_BYTES + 2)
    lines = data.split(b"\n") if data else []
    if data.endswith(b"\n"):
        lines.pop()
    truncated = size > MAX_BYTES or len(lines) > MAX_LINES
    if not truncated:
        return data.decode("utf-8", errors="replace") or "(no output)"

    tail = []
    used = 0
    for line in reversed(lines):
        length = len(line) + bool(tail)
        if used + length > MAX_BYTES:
            if not tail:
                line = line[-MAX_BYTES:]
                while line and line[0] & 0xC0 == 0x80:
                    line = line[1:]
                tail.append(line)
            break
        tail.append(line)
        used += length
        if len(tail) == MAX_LINES:
            break
    output = b"\n".join(reversed(tail)).decode("utf-8", errors="replace")
    with NamedTemporaryFile(
        prefix="bash-", suffix=".log", dir=output_dir, delete=False
    ) as full_output:
        stream.seek(0)
        shutil.copyfileobj(stream, full_output)
        name = Path(full_output.name).name
    return (
        f"{output}\n\n[输出截断：保留末尾最多 {MAX_LINES} 行或 50KB。"
        f"完整输出：{reference_dir}/{name}]"
    )


def kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            [
                str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                "/F",
                "/T",
                "/PID",
                str(process.pid),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_command(
    shell: str,
    legacy_wsl: bool,
    root: Path,
    output_dir: Path,
    reference_dir: str,
    command: str,
    timeout: float | None,
    cancel: Event | None,
) -> AgentToolResult:
    with TemporaryFile(dir=output_dir) as output:
        with subprocess.Popen(
            [shell, "-s"] if legacy_wsl else [shell, "-c", command],
            cwd=root,
            stdin=subprocess.PIPE if legacy_wsl else subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        ) as process:
            aborted = False
            timed_out = False
            exit_code: int | None = None
            deadline = None if timeout is None else monotonic() + timeout
            try:
                if legacy_wsl:
                    assert process.stdin is not None
                    process.stdin.write(command.encode("utf-8"))
                    process.stdin.close()
                while True:
                    if cancel is not None and cancel.is_set():
                        aborted = True
                        break
                    if deadline is not None and monotonic() >= deadline:
                        timed_out = True
                        break
                    try:
                        exit_code = process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    kill_tree(process)
        text = format_output(output, output_dir, reference_dir)
    if aborted:
        text += "\n\nCommand aborted"
    elif timed_out:
        text += f"\n\nCommand timed out after {timeout} seconds"
    else:
        if exit_code is not None and exit_code < 0:
            exit_code = 128 - exit_code
        if exit_code != 0:
            text += f"\n\nCommand exited with code {exit_code}"
    return AgentToolResult(
        [TextContent(type="text", text=text)],
        is_error=aborted or timed_out or bool(exit_code),
    )


def create_bash_tool(session_id: str, *, tmp_root: Path = TMP_ROOT) -> AgentTool:
    workspace = Workspace(session_id, tmp_root)
    root = workspace.root
    output_dir = workspace.ensure_workspace()
    reference_dir = workspace.relative_to_root(output_dir)
    shell = resolve_bash()
    legacy_wsl = os.name == "nt" and Path(shell).parent.name.lower() in {
        "system32",
        "sysnative",
    }

    def execute(
        tool_call_id, params: BashArguments, signal, on_update
    ) -> AgentToolResult:
        return run_command(
            shell, legacy_wsl, root, output_dir, reference_dir,
            params.command, params.timeout, signal,
        )

    return AgentTool(
        "bash",
        "在项目根目录 tmp 中执行 Bash 命令，按写入顺序合并 stdout 和 stderr。"
        "保留末尾最多 2000 行或 50KB，截断时完整输出保存到当前会话 workspace 文件。"
        "timeout 为可选的秒数，默认无超时；Bash 以宿主进程权限运行，cwd 不构成文件系统隔离。",
        BashArguments,
        execute,
        execution_mode="sequential",
        max_output_chars=None,
    )
