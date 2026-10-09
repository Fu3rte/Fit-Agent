from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.agent.tools.files import create_file_tools
from test.regression_support import TEST_SESSION, run_tool, session_workspace, text


def check() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = create_file_tools(TEST_SESSION, tmp_root=root)
        workspace, prefix = session_workspace(root)

        def rel(sub: str = "") -> str:
            return f"{prefix}/{sub}" if sub else prefix

        assert set(tools) == {"read", "edit", "write", "grep", "find", "ls"}
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
        for tool in tools.values():
            assert tool.definition().parameters["type"] == "object"

        def call(name: str, path: str, **arguments) -> str:
            message = run_tool(tools[name], {"path": path, **arguments})
            assert message.is_error is False, text(message)
            return text(message)

        def rejected(name: str, path: str, **arguments) -> str:
            message = run_tool(tools[name], {"path": path, **arguments})
            assert message.is_error is True, (name, path, text(message))
            return text(message)

        call("write", rel("nested/example.txt"), content="hello\nworld\n")
        assert call("read", rel("nested/example.txt"), offset=2, limit=1) == "2: world\n"
        call("edit", rel("nested/example.txt"), old_text="world", new_text="ReAct")
        assert (
            call("grep", rel(), pattern="ReAct", glob="**/*.txt")
            == f"{prefix}/nested/example.txt:2: ReAct"
        )
        assert call("find", rel(), pattern="**/*.txt") == f"{prefix}/nested/example.txt"
        assert call("ls", rel()) == "nested/"
        call("write", rel("root.txt"), content="top level")
        assert rel("root.txt") in call("find", rel(), pattern="**/*.txt")
        assert "[已达到 limit]" in call("find", rel(), pattern="**/*.txt", limit=1)
        assert "[输出截断" in call("ls", rel(), limit=1)
        call("write", rel("large.txt"), content="x" * 50_001)
        assert "[输出截断" in call("read", rel("large.txt"))
        call("write", rel(".venv/hidden.txt"), content="excluded")
        assert ".venv" not in call("find", rel(), pattern="**/*.txt")

        multi = "".join(f"line-{number}\n" for number in range(1, 6))
        call("write", rel("multi.txt"), content=multi)
        window = call("read", rel("multi.txt"), offset=1, limit=2)
        assert window.startswith("1: line-1\n2: line-2\n") and "继续读取" in window
        tail = call("read", rel("multi.txt"), offset=5, limit=2)
        assert tail == "5: line-5\n" and "继续读取" not in tail
        assert "末尾之后" in call("read", rel("multi.txt"), offset=99)

        # 会话原附件只读：可读、可搜索，write 与 edit 均越界。
        attachments = root / "sessions" / TEST_SESSION / "attachments"
        attachments.mkdir(parents=True)
        plan = attachments / "plan.md"
        plan.write_text("原始计划正文", encoding="utf-8")
        attachment_path = f"sessions/{TEST_SESSION}/attachments/plan.md"
        assert call("read", attachment_path) == "1: 原始计划正文\n"
        assert call("find", f"sessions/{TEST_SESSION}", pattern="**/*.md") == attachment_path
        assert "workspace" in rejected("write", attachment_path, content="x")
        assert "workspace" in rejected(
            "edit", attachment_path, old_text="原始计划正文", new_text="x"
        )
        assert plan.read_text(encoding="utf-8") == "原始计划正文"

        # 跨会话、绝对路径、目录穿越与链接逃逸全部拒绝。
        other = str(uuid4())
        other_workspace = root / "sessions" / other / "workspace"
        other_workspace.mkdir(parents=True)
        (other_workspace / "secret.txt").write_text("secret", encoding="utf-8")
        assert "当前会话" in rejected(
            "read", f"sessions/{other}/workspace/secret.txt"
        )
        assert "只允许 tmp" in rejected("read", "../escape.txt")
        assert "只允许 tmp" in rejected("read", f"{prefix}/../../escape.txt")
        assert "只允许 tmp" in rejected("read", str(workspace / "root.txt"))
        assert "只允许 tmp" in rejected("ls", str(root))
        assert "当前会话" in rejected("ls", f"sessions/{other}/workspace")

        outside = root / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("leak", encoding="utf-8")
        link = workspace / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            link = None
        if link is not None:
            assert "链接" in rejected("read", f"{prefix}/link/leak.txt")
            assert "链接" in rejected("write", f"{prefix}/link/new.txt", content="x")
            # 遍历遇到链接目录直接剪枝，不越界读取也不报错。
            assert "leak.txt" not in call("find", rel(), pattern="**/*.txt")
            link.unlink()

        message = run_tool(
            tools["edit"],
            {"path": rel("root.txt"), "old_text": "absent", "new_text": "x"},
        )
        assert message.is_error is True and "old_text" in text(message)

        message = run_tool(tools["read"], {"path": rel("root.txt"), "offset": 0})
        assert message.is_error is True and "offset" in text(message)

        message = run_tool(
            tools["write"], {"path": rel("root.txt"), "content": "x", "extra": True}
        )
        assert message.is_error is True and "extra" in text(message)

        assert (workspace / "root.txt").read_text(encoding="utf-8") == "top level"
    print("六个工具、会话边界、附件只读、路径安全、续读提示与输出限制检查通过")


if __name__ == "__main__":
    check()
