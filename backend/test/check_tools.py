import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.tools.files import WORKSPACE, create_file_tools


def check() -> None:
    tools = create_file_tools()
    assert WORKSPACE == Path(__file__).resolve().parents[1] / "temp"
    with TemporaryDirectory(dir=WORKSPACE) as directory:
        root = Path(directory)

        def call(name: str, **arguments) -> str:
            arguments["path"] = str(
                (root / arguments.get("path", ".")).relative_to(WORKSPACE)
            )
            result = tools[name].invoke(json.dumps(arguments))
            assert result.isError is False
            return result.content

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

        backend = Path(__file__).resolve().parents[1]
        invocation = (
            "import sys; "
            f"sys.path.insert(0, {str(backend)!r}); "
            "from app.agent.tools.files import create_file_tools; "
            "print(create_file_tools()[sys.argv[1]].invoke(sys.argv[2]).content)"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                invocation,
                "read",
                json.dumps({"path": f"{root.name}/root.txt"}),
            ],
            cwd=root,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout.decode().strip() == "1: top level"

        (root / "outside").symlink_to(backend, target_is_directory=True)
        rejected_paths = [
            "../outside.txt",
            f"{root.name}/../root.txt",
            str(root / "root.txt"),
            str(backend),
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
        failures = [
            (name, {**arguments, "path": path}, "PermissionError")
            for name, arguments in required_arguments.items()
            for path in rejected_paths
        ]
        failures += [
            ("read", {"path": "../outside.txt"}, "PermissionError"),
            (
                "edit",
                {
                    "path": f"{root.name}/root.txt",
                    "old_text": "absent",
                    "new_text": "x",
                },
                "ValueError",
            ),
            ("read", {"path": "root.txt", "offset": 0}, "ValidationError"),
            ("ls", {"path": str(WORKSPACE.parent)}, "PermissionError"),
            (
                "write",
                {"path": "root.txt", "content": "x", "extra": True},
                "ValidationError",
            ),
        ]
        for name, arguments, error in failures:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    invocation,
                    name,
                    json.dumps(arguments),
                ],
                cwd=root,
                capture_output=True,
            )
            assert result.returncode != 0 and error.encode("ascii") in result.stderr
        assert (root / "root.txt").read_text(encoding="utf-8") == "top level"
    print("六个工具、输入校验、workspace 边界与输出限制检查通过")


if __name__ == "__main__":
    check()
