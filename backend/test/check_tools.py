from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.tools.files import WORKSPACE, create_file_tools
from test.regression_support import run_tool, text


def check() -> None:
    tools = create_file_tools()
    assert WORKSPACE == Path(__file__).resolve().parents[1] / "temp"
    assert {
        name: tool.execution_mode for name, tool in tools.items()
    } == {
        "read": "parallel",
        "edit": "sequential",
        "write": "sequential",
        "grep": "parallel",
        "find": "parallel",
        "ls": "parallel",
    }
    with TemporaryDirectory(dir=WORKSPACE) as directory:
        root = Path(directory)

        def call(name: str, **arguments) -> str:
            arguments["path"] = str(
                (root / arguments.get("path", ".")).relative_to(WORKSPACE)
            )
            message = run_tool(tools[name], arguments)
            assert message.is_error is False, text(message)
            return text(message)

        assert set(tools) == {"read", "edit", "write", "grep", "find", "ls"}
        for tool in tools.values():
            assert tool.definition().parameters["type"] == "object"
        call("write", path="nested/example.txt", content="hello\nworld\n")
        assert (
            call("read", path="nested/example.txt", offset=2, limit=1) == "2: world\n"
        )
        call("edit", path="nested/example.txt", old_text="world", new_text="ReAct")
        assert (
            call("grep", pattern="ReAct", glob="**/*.txt")
            == f"{root.name}/nested/example.txt:2: ReAct"
        )
        assert call("find", pattern="**/*.txt") == f"{root.name}/nested/example.txt"
        assert call("ls") == "nested/"
        call("write", path="root.txt", content="top level")
        assert "root.txt" in call("find", pattern="**/*.txt")
        assert "[已达到 limit]" in call("find", pattern="**/*.txt", limit=1)
        assert "[输出截断" in call("ls", limit=1)
        call("write", path="large.txt", content="x" * 50_001)
        assert "[输出截断" in call("read", path="large.txt")
        call("write", path=".venv/hidden.txt", content="excluded")
        assert ".venv" not in call("find", pattern="**/*.txt")

        (root / "outside").symlink_to(
            Path(__file__).resolve().parents[1], target_is_directory=True
        )
        rejected_paths = [
            "../outside.txt",
            f"{root.name}/../root.txt",
            str(root / "root.txt"),
            str(Path(__file__).resolve().parents[1]),
            f"{root.name}/outside/main.py",
        ]
        required_arguments = {
            "read": {},
            "edit": {"old_text": "x", "new_text": "y"},
            "write": {"content": "x"},
            "grep": {"pattern": "x"},
            "find": {"pattern": "**/*"},
            "ls": {},
        }
        for name, arguments in required_arguments.items():
            for path in rejected_paths:
                message = run_tool(tools[name], {**arguments, "path": path})
                assert message.is_error is True, (name, path)
                assert "backend/temp" in text(message), (name, path, text(message))

        message = run_tool(
            tools["edit"],
            {
                "path": f"{root.name}/root.txt",
                "old_text": "absent",
                "new_text": "x",
            },
        )
        assert message.is_error is True and "old_text" in text(message)

        message = run_tool(tools["read"], {"path": "root.txt", "offset": 0})
        assert message.is_error is True and "offset" in text(message)

        message = run_tool(tools["ls"], {"path": str(WORKSPACE.parent)})
        assert message.is_error is True and "backend/temp" in text(message)

        message = run_tool(
            tools["write"], {"path": "root.txt", "content": "x", "extra": True}
        )
        assert message.is_error is True and "extra" in text(message)

        assert (root / "root.txt").read_text(encoding="utf-8") == "top level"
    print("六个工具、输入校验、workspace 边界与输出限制检查通过")


if __name__ == "__main__":
    check()
