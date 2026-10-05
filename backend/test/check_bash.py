import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.tools.bash import MAX_BYTES, MAX_LINES, create_bash_tool
from app.agent.tools.files import WORKSPACE, create_file_tools


def check() -> None:
    tool = create_bash_tool()

    def call(command: str, *, is_error: bool = False, **arguments) -> str:
        result = tool.invoke(json.dumps({"command": command, **arguments}))
        assert result.isError is is_error
        return result.content

    assert tool.arguments.model_validate_json('{"command":"true"}').timeout is None
    assert call("printf hello") == "hello"
    assert call("true") == "(no output)"
    started = time.monotonic()
    assert call("sleep 1 & printf done") == "done"
    assert time.monotonic() - started < 1
    assert call("printf '中文'", timeout=0.5) == "中文"
    assert call("printf out; printf err >&2; printf end") == "outerrend"
    assert (
        call("printf failure >&2; exit 7", is_error=True)
        == "failure\n\nCommand exited with code 7"
    )
    direct = tool.execute("printf direct", None)
    assert direct.content == "direct" and direct.isError is False
    limited = replace(tool, max_output_chars=8)
    for exit_code in (0, 7):
        result = limited.invoke(
            json.dumps({"command": f"printf 1234567890; exit {exit_code}"})
        )
        assert result.isError is (exit_code != 0)
        assert result.content == "12345678\n[输出截断：超过 8 字符]"
    assert Path(call("pwd").strip()).name == "temp"
    saved = set(WORKSPACE.glob("bash-*.log"))
    try:
        text = call("for ((i=1;i<=2001;i++)); do printf '%s\\n' \"$i\"; done")
        output, notice = text.split("\n\n", 1)
        assert output.splitlines() == [str(i) for i in range(2, MAX_LINES + 2)]
        path = next(iter(set(WORKSPACE.glob("bash-*.log")) - saved))
        assert path.name in notice
        assert path.read_text().splitlines() == [str(i) for i in range(1, 2002)]
        assert (
            "2001"
            in create_file_tools()["read"]
            .invoke(json.dumps({"path": path.name, "offset": 2001, "limit": 1}))
            .content
        )
        saved.add(path)
        path.unlink()

        assert call("printf '%051200d' 0") == "0" * MAX_BYTES
        assert len(call("printf '%051201d' 0").split("\n\n", 1)[0]) == MAX_BYTES
        text = call("printf '%060000d' 0; printf '中文END'")
        output, notice = text.split("\n\n", 1)
        assert len(output.encode("utf-8")) <= MAX_BYTES
        assert output.endswith("中文END") and "�" not in output
        assert "完整输出" in notice and "50000 字符" not in text
        path = next(iter(set(WORKSPACE.glob("bash-*.log")) - saved))
        assert len(path.read_bytes()) > MAX_BYTES
        saved.add(path)
        path.unlink()

        failed = call("printf '%060000d' 0; exit 7", is_error=True)
        assert "完整输出" in failed and failed.endswith("Command exited with code 7")
        assert len(failed.split("\n\n", 1)[0].encode()) <= MAX_BYTES

        text = call("for ((i=0;i<18000;i++)); do printf '中'; done")
        output = text.split("\n\n", 1)[0]
        assert len(output.encode("utf-8")) <= MAX_BYTES and set(output) == {"中"}

        invocation = "from app.agent.tools.bash import create_bash_tool; import sys; print(create_bash_tool().invoke(sys.argv[1]).content)"
        for arguments in (
            {"command": "true", "timeout": 0},
            {"command": "true", "timeout": -1},
            {"command": "true", "timeout": "1"},
            {"command": "true", "timeout": True},
            {"command": "true", "timeout": float("inf")},
            {"command": "true", "timeout": 2_147_484},
            {"command": "true", "extra": True},
            {"command": ""},
        ):
            result = subprocess.run(
                [sys.executable, "-c", invocation, json.dumps(arguments)],
                capture_output=True,
            )
            assert result.returncode != 0 and b"ValidationError" in result.stderr

        with TemporaryDirectory(dir=WORKSPACE) as directory:
            marker = Path(directory) / "leaked.txt"
            arguments = {
                "command": f"(sleep 2; printf leaked > '{marker.parent.name}/leaked.txt') & wait",
                "timeout": 0.2,
            }
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-c", invocation, json.dumps(arguments)],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode != 0 and b"TimeoutExpired" in result.stderr
            assert time.monotonic() - started < 2
            time.sleep(2.2)
            assert not marker.exists()
    finally:
        for path in set(WORKSPACE.glob("bash-*.log")) - saved:
            path.unlink()
    print("Bash 执行、输出顺序、UTF-8 尾部截断、完整日志、参数与超时进程树检查通过")


if __name__ == "__main__":
    check()
