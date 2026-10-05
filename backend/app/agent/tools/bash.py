import os
import shutil
import signal
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryFile
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tool import Tool, ToolExecutionResult
from app.agent.tools.files import WORKSPACE

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


def format_output(stream: BinaryIO, root: Path) -> str:
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
        prefix="bash-", suffix=".log", dir=root, delete=False
    ) as full_output:
        stream.seek(0)
        shutil.copyfileobj(stream, full_output)
        name = Path(full_output.name).name
    return (
        f"{output}\n\n[输出截断：保留末尾最多 {MAX_LINES} 行或 50KB。完整输出：{name}]"
    )


def create_bash_tool() -> Tool:
    if WORKSPACE.is_symlink() or WORKSPACE.is_junction():
        raise PermissionError("backend/temp 不能是链接")
    WORKSPACE.mkdir(exist_ok=True)
    root = WORKSPACE.resolve(strict=True)
    shell = resolve_bash()
    legacy_wsl = os.name == "nt" and Path(shell).parent.name.lower() in {
        "system32",
        "sysnative",
    }

    def execute(command: str, timeout: float | None) -> ToolExecutionResult:
        with TemporaryFile(dir=root) as output:
            with subprocess.Popen(
                [shell, "-s"] if legacy_wsl else [shell, "-c", command],
                cwd=root,
                stdin=subprocess.PIPE if legacy_wsl else subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            ) as process:
                try:
                    if legacy_wsl:
                        assert process.stdin is not None
                        process.stdin.write(command.encode("utf-8"))
                        process.stdin.close()
                    exit_code = process.wait(timeout=timeout)
                finally:
                    if process.poll() is None:
                        if os.name == "nt":
                            subprocess.run(
                                [
                                    str(
                                        Path(os.environ["SystemRoot"])
                                        / "System32"
                                        / "taskkill.exe"
                                    ),
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
            text = format_output(output, root)
        if exit_code < 0:
            exit_code = 128 - exit_code
        if exit_code != 0:
            text += f"\n\nCommand exited with code {exit_code}"
        return ToolExecutionResult(text, exit_code != 0)

    return Tool(
        "bash",
        "在 backend/temp 中执行 Bash 命令，按写入顺序合并 stdout 和 stderr。"
        "保留末尾最多 2000 行或 50KB，截断时完整输出保存到 workspace 文件。"
        "timeout 为可选的秒数，默认无超时；Bash 命令以宿主进程权限运行。",
        BashArguments,
        execute,
        max_output_chars=None,
    )
