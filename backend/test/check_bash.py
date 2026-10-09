import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

from app.agent.tools.bash import MAX_BYTES, MAX_LINES, create_bash_tool
from app.agent.tools.files import create_file_tools
from test.regression_support import TEST_SESSION, run_tool, session_workspace, text


def check() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tool = create_bash_tool(TEST_SESSION, tmp_root=root)
        workspace, prefix = session_workspace(root)
        reader = create_file_tools(TEST_SESSION, tmp_root=root)["read"]

        def invoke(command: str, *, timeout=None, expect_error: bool = False) -> str:
            arguments: dict = {"command": command}
            if timeout is not None:
                arguments["timeout"] = timeout
            message = run_tool(tool, arguments)
            assert message.is_error is expect_error, text(message)
            return text(message)

        def call(command: str, *, timeout=None) -> str:
            return invoke(command, timeout=timeout)

        assert tool.arguments.model_validate_json('{"command":"true"}').timeout is None
        assert call("printf hello") == "hello"
        assert call("true") == "(no output)"
        started = time.monotonic()
        assert call("sleep 1 & printf done") == "done"
        assert time.monotonic() - started < 1
        assert call("printf '中文'", timeout=0.5) == "中文"
        assert call("printf out; printf err >&2; printf end") == "outerrend"
        assert (
            invoke("printf failure >&2; exit 7", expect_error=True)
            == "failure\n\nCommand exited with code 7"
        )
        assert call("printf direct") == "direct"

        limited = replace(tool, max_output_chars=8)
        for exit_code in (0, 7):
            message = run_tool(
                limited, {"command": f"printf 1234567890; exit {exit_code}"}
            )
            assert message.is_error is (exit_code != 0)
            assert text(message) == "12345678\n[输出截断：超过 8 字符]"

        for arguments, clue in (
            ({"command": "true", "timeout": 0}, "timeout"),
            ({"command": "true", "timeout": -1}, "timeout"),
            ({"command": "true", "timeout": "1"}, "timeout"),
            ({"command": "true", "timeout": True}, "timeout"),
            ({"command": "true", "timeout": 2_147_484}, "timeout"),
            ({"command": "true", "extra": True}, "extra"),
            ({"command": ""}, "command"),
        ):
            message = run_tool(tool, arguments)
            assert message.is_error is True, arguments
            assert clue in text(message), (arguments, text(message))

        # Bash 的真实工作目录是统一 tmp 根，不是会话 workspace。
        assert Path(call("pwd").strip()).name == root.name
        assert call("pwd").strip().replace("\\", "/").endswith(root.name)
        saved = set(workspace.glob("bash-*.log"))
        try:
            body = call("for ((i=1;i<=2001;i++)); do printf '%s\\n' \"$i\"; done")
            output, notice = body.split("\n\n", 1)
            assert output.splitlines() == [str(i) for i in range(2, MAX_LINES + 2)]
            path = next(iter(set(workspace.glob("bash-*.log")) - saved))
            assert path.name in notice and f"{prefix}/{path.name}" in notice
            assert path.read_text().splitlines() == [str(i) for i in range(1, 2002)]
            excerpt = run_tool(
                reader,
                {"path": f"{prefix}/{path.name}", "offset": 2001, "limit": 1},
            )
            assert "2001" in text(excerpt)
            saved.add(path)
            path.unlink()

            assert call("printf '%051200d' 0") == "0" * MAX_BYTES
            assert len(call("printf '%051201d' 0").split("\n\n", 1)[0]) == MAX_BYTES
            body = call("printf '%060000d' 0; printf '中文END'")
            output, notice = body.split("\n\n", 1)
            assert len(output.encode("utf-8")) <= MAX_BYTES
            assert output.endswith("中文END") and "�" not in output
            assert "完整输出" in notice and "50000 字符" not in body
            path = next(iter(set(workspace.glob("bash-*.log")) - saved))
            assert len(path.read_bytes()) > MAX_BYTES
            saved.add(path)
            path.unlink()

            failed = invoke("printf '%060000d' 0; exit 7", expect_error=True)
            assert "完整输出" in failed and failed.endswith("Command exited with code 7")
            assert len(failed.split("\n\n", 1)[0].encode()) <= MAX_BYTES

            body = call("for ((i=0;i<18000;i++)); do printf '中'; done")
            output = body.split("\n\n", 1)[0]
            assert len(output.encode("utf-8")) <= MAX_BYTES and set(output) == {"中"}

            with TemporaryDirectory(dir=workspace) as directory:
                marker = Path(directory) / "leaked.txt"
                started = time.monotonic()
                body = invoke(
                    f"(sleep 2; printf leaked > '{marker.parent.name}/leaked.txt') & wait",
                    timeout=0.2,
                    expect_error=True,
                )
                assert "Command timed out after 0.2 seconds" in body, body
                assert time.monotonic() - started < 2
                time.sleep(2.2)
                assert not marker.exists()

            cancel = Event()

            def trigger() -> None:
                time.sleep(0.4)
                cancel.set()

            thread = Thread(target=trigger)
            thread.start()
            started = time.monotonic()
            message = run_tool(tool, {"command": "printf partial; sleep 5"}, signal=cancel)
            thread.join()
            assert message.is_error is True
            assert "partial" in text(message) and "Command aborted" in text(message)
            assert time.monotonic() - started < 5
        finally:
            for path in set(workspace.glob("bash-*.log")) - saved:
                path.unlink()
    print("Bash 执行、tmp 工作目录、输出顺序、UTF-8 尾部截断、完整日志、超时中止与参数检查通过")


if __name__ == "__main__":
    check()
