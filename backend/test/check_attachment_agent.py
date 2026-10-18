import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from app.agent.tool import run_tool_call
from app.agent.tools.files import create_file_tools
from app.ai.messages import SystemMessage, ToolCall
from app.application.session.service import SessionService
from app.domain.session.attachments import PROJECT_ROOT, TMP_ROOT, attachment_read_refs
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.regression_support import temporary_root, text

ROOT = temporary_root("attachment-agent")
SYSTEM = SystemMessage(role="system", content="阻断二附件读取验收", timestamp=1)


def uid() -> str:
    return str(uuid4())


def upload(data: bytes, name: str, identity: str | None = None) -> dict:
    return {
        "kind": "upload",
        "attachment_id": identity or uid(),
        "file_name": name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def reference(identity: str) -> dict:
    return {"kind": "reference", "attachment_id": identity}


def send(session: str, content: str = "", attachments: list | None = None) -> SendCommand:
    return SendCommand(
        operation_id=uid(),
        session_id=session,
        request=SendRequest(text=content, attachments=attachments or []),
    )


def project(entries):
    return [
        attachment_read_refs(entry.attachments)
        for entry in entries
        if entry.messages[0].role == "user"
    ]


async def tool_result(tool, arguments):
    call = ToolCall(type="toolCall", id="call", name=tool.name, arguments=arguments)
    return await run_tool_call(
        call, tools={tool.name: tool}, declared={tool.name: tool.definition()}
    )


def check_root_independent_of_cwd() -> str:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    code = "from app.domain.session.attachments import TMP_ROOT; print(TMP_ROOT)"
    outputs = []
    for cwd in (Path(__file__).resolve().parents[1], ROOT):
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=cwd, env=env,
            capture_output=True, text=True, check=True,
        )
        outputs.append(result.stdout.strip())
    assert len(set(outputs)) == 1 and outputs[0] == str(TMP_ROOT), outputs
    assert str(TMP_ROOT) == str(PROJECT_ROOT / "tmp")
    return outputs[0]


async def attachments_and_tools(root: Path) -> dict:
    tmp_root = root / "tmp"
    database = await open_database(root / "attachment-agent.db")
    try:
        repository = SqliteSessionRepository(database)
        service = SessionService(repository, project_root=root)
        session, other = uid(), uid()
        await service.create_session(session, "附件读取")
        await service.create_session(other, "另一会话")
        tools = create_file_tools(session, tmp_root=tmp_root)
        reader, writer, editor = tools["read"], tools["write"], tools["edit"]

        async def attach(body: bytes, name: str) -> str:
            item = upload(body, name)
            run = await service.accept_send(
                send(session, "", [item]), system_message=SYSTEM
            )
            await service.runs.finish_run(session, run.run.id, "completed")
            return f"sessions/{session}/attachments/{item['attachment_id']}.txt"

        # 文字加文件、纯文件均进入同一分支并按真实节点投影。
        plan_body = "# 训练计划\n周一 深蹲 3 组\n周二 卧推 3 组\n"
        plan = upload(plan_body.encode("utf-8"), "plan.md")
        notes = upload("备注：膝盖不适\n".encode("utf-8"), "notes.txt")
        first_run = await service.accept_send(
            send(session, "录入这个计划", [plan, notes]), system_message=SYSTEM
        )
        await service.runs.finish_run(session, first_run.run.id, "completed")
        pure_body = "纯文件正文\n第二行\n"
        pure = upload(pure_body.encode("utf-8"), "pure.txt")
        pure_run = await service.accept_send(
            send(session, "", [pure]), system_message=SYSTEM
        )
        await service.runs.finish_run(session, pure_run.run.id, "completed")

        leaf = (await service.queries.get_session(session)).active_leaf_id
        entries = await service.queries.get_branch(session, leaf)
        users = [entry for entry in entries if entry.messages[0].role == "user"]
        projected = project(entries)
        assert len(projected) == 2, projected
        assert [name for name, _ in projected[0]] == ["plan.md", "notes.txt"]
        plan_path = f"sessions/{session}/attachments/{plan['attachment_id']}.md"
        notes_path = f"sessions/{session}/attachments/{notes['attachment_id']}.txt"
        assert [path for _, path in projected[0]] == [plan_path, notes_path]
        assert users[0].messages[0].content == "录入这个计划"
        assert projected[1][0][1] == f"sessions/{session}/attachments/{pure['attachment_id']}.txt"

        # 原附件可定位、可读取，正文与原字节完整。
        read_plan = await tool_result(reader, {"path": plan_path})
        assert not read_plan.is_error and "周一 深蹲 3 组" in text(read_plan)
        assert (tmp_root / plan_path).read_bytes() == plan_body.encode("utf-8")
        read_pure = await tool_result(reader, {"path": projected[1][0][1]})
        assert "纯文件正文" in text(read_pure)

        # 长附件按 offset/limit 连续读取，给出续读信息，不静默截断。
        long_body = "".join(f"第{index:03d}行\n" for index in range(1, 401))
        long_upload = upload(long_body.encode("utf-8"), "long.txt")
        long_run = await service.accept_send(
            send(session, "", [long_upload]), system_message=SYSTEM
        )
        await service.runs.finish_run(session, long_run.run.id, "completed")
        long_path = f"sessions/{session}/attachments/{long_upload['attachment_id']}.txt"
        window = await tool_result(reader, {"path": long_path, "offset": 1, "limit": 2})
        window_text = text(window)
        assert window_text.startswith("1: 第001行\n2: 第002行\n")
        assert "继续读取" in window_text and "offset=3" in window_text
        last = await tool_result(reader, {"path": long_path, "offset": 400, "limit": 2})
        assert text(last) == "400: 第400行\n" and "继续读取" not in text(last)
        assert (tmp_root / long_path).read_bytes().decode("utf-8") == long_body

        # 单行超过 50,000 字符且附件恰为 100,000 字节：完整返回，逐字核对正文与末尾唯一标记。
        single_marker = "END-SINGLE-MARK"
        single_line = "b" * (100_000 - len(single_marker)) + single_marker
        assert len(single_line.encode("utf-8")) == 100_000
        single_path = await attach(single_line.encode("utf-8"), "single.txt")
        single_read = await tool_result(reader, {"path": single_path})
        assert text(single_read) == f"1: {single_line}\n"
        assert (tmp_root / single_path).read_bytes() == single_line.encode("utf-8")

        # UTF-8 中文长行完整返回：上传体积按字节计、正文按字符计，两者正确区分。
        chinese = "中" * 33_000 + "END-中文标记"
        chinese_bytes = chinese.encode("utf-8")
        assert len(chinese_bytes) <= 100_000
        chinese_path = await attach(chinese_bytes, "chinese.txt")
        chinese_read = await tool_result(reader, {"path": chinese_path})
        assert text(chinese_read) == f"1: {chinese}\n"
        assert (tmp_root / chinese_path).read_bytes() == chinese_bytes

        # 多行读取窗口超过 50,000 字符时，窗口内容与续读提示完整，下一页准确衔接。
        lines = [f"窗口行{number:04d}:" + "z" * 985 for number in range(1, 101)]
        multi_body = "".join(f"{line}\n" for line in lines)
        assert sum(len(f"{number}: {lines[number - 1]}\n") for number in range(1, 61)) > 50_000
        multi_path = await attach(multi_body.encode("utf-8"), "window.txt")
        first_page = await tool_result(reader, {"path": multi_path, "offset": 1, "limit": 60})
        expected_first = "".join(
            f"{number}: {lines[number - 1]}\n" for number in range(1, 61)
        )
        assert text(first_page) == (
            expected_first
            + "\n[仅显示到第 60 行，文件仍有后续内容；继续读取请使用 offset=61]"
        )
        second_page = await tool_result(reader, {"path": multi_path, "offset": 61, "limit": 60})
        expected_second = "".join(
            f"{number}: {lines[number - 1]}\n" for number in range(61, 101)
        )
        assert text(second_page) == expected_second
        assert (tmp_root / multi_path).read_bytes().decode("utf-8") == multi_body

        # 当前会话工作文件可写、可编辑、可查询。
        workspace = f"sessions/{session}/workspace"
        assert "已写入" in text(await tool_result(writer, {"path": f"{workspace}/work.txt", "content": "第一版"}))
        assert "已编辑" in text(await tool_result(editor, {"path": f"{workspace}/work.txt", "old_text": "第一版", "new_text": "第二版"}))
        assert "第二版" in text(await tool_result(reader, {"path": f"{workspace}/work.txt"}))

        # write 与 edit 无法修改上传原附件，原字节保持完整。
        for attempt in (
            await tool_result(writer, {"path": plan_path, "content": "覆盖"}),
            await tool_result(editor, {"path": plan_path, "old_text": "深蹲", "new_text": "改动"}),
        ):
            assert attempt.is_error and "workspace" in text(attempt)
        assert (tmp_root / plan_path).read_bytes() == plan_body.encode("utf-8")

        # 跨会话、绝对路径、目录穿越与链接逃逸全部拒绝。
        other_plan = upload(b"other", "other.md")
        other_run = await service.accept_send(
            send(other, "", [other_plan]), system_message=SYSTEM
        )
        await service.runs.finish_run(other, other_run.run.id, "completed")
        cross = f"sessions/{other}/attachments/{other_plan['attachment_id']}.md"
        assert "当前会话" in text(await tool_result(reader, {"path": cross}))
        assert "只允许 tmp" in text(await tool_result(reader, {"path": "../escape.txt"}))
        assert "只允许 tmp" in text(await tool_result(reader, {"path": str(tmp_root / plan_path)}))

        outside = tmp_root / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("leak", encoding="utf-8")
        link = tmp_root / "sessions" / session / "workspace" / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            link = None
        if link is not None:
            assert "链接" in text(await tool_result(reader, {"path": f"{workspace}/link/leak.txt"}))
            link.unlink()

        # 编辑保留原附件、替换为新集合；重新生成沿用目标版本附件。
        edit = EditCommand(
            operation_id=uid(), session_id=session,
            request=EditRequest(
                target_entry_id=users[1].id, text="编辑后保留纯文件",
                attachments=[reference(pure["attachment_id"])],
            ),
        )
        edited = await service.accept_edit(edit)
        await service.runs.finish_run(session, edited.run.id, "completed")
        edited_entry = await service.queries.get_entry(session, edited.run.request_entry_id)
        assert [item.attachment_id for item in edited_entry.attachments] == [pure["attachment_id"]]
        assert project(await service.queries.get_branch(session, edited.run.request_entry_id))[-1][0][1] == projected[1][0][1]

        replacement = upload("替换正文\n".encode("utf-8"), "new.txt")
        replace = EditCommand(
            operation_id=uid(), session_id=session,
            request=EditRequest(
                target_entry_id=edited.run.request_entry_id, text="替换附件",
                attachments=[reference(pure["attachment_id"]), replacement],
            ),
        )
        replaced = await service.accept_edit(replace)
        await service.runs.finish_run(session, replaced.run.id, "completed")
        regenerated = await service.accept_regenerate(
            RegenerateCommand(operation_id=uid(), session_id=session,
                              request=RegenerateRequest(target_entry_id=replaced.run.request_entry_id))
        )
        await service.runs.finish_run(session, regenerated.run.id, "completed")
        regenerated_projection = project(
            await service.queries.get_branch(session, regenerated.run.request_entry_id)
        )[-1]
        assert [path for _, path in regenerated_projection] == [
            f"sessions/{session}/attachments/{pure['attachment_id']}.txt",
            f"sessions/{session}/attachments/{replacement['attachment_id']}.txt",
        ]

        # pending、withdrawn Steering 附件不进入模型上下文；consumed 后按真实节点投影。
        steer_session = uid()
        await service.create_session(steer_session, "Steering 附件")
        steer_run = await service.accept_send(
            send(steer_session, "开始"), system_message=SYSTEM
        )
        pending_upload = upload("steering 正文\n".encode("utf-8"), "steering.txt")
        steering = (await service.accept_steering(SteeringCommand(
            operation_id=uid(), session_id=steer_session,
            request=SteeringRequest(
                target_run_id=steer_run.run.id, text="追加附件",
                attachments=[pending_upload],
            ),
        ))).steering
        pending_branch = project(
            await service.queries.get_branch(steer_session, (await service.queries.get_session(steer_session)).active_leaf_id)
        )
        pending_paths = [path for group in pending_branch for _, path in group]
        assert all(pending_upload["attachment_id"] not in path for path in pending_paths), pending_branch
        consumed = await service.runs.consume_steering(steer_session, steer_run.run.id, steering.id)
        after_consumption = project(
            await service.queries.get_branch(steer_session, consumed.entry.id)
        )
        consumed_projection = after_consumption[-1]
        assert consumed_projection[0][1] == (
            f"sessions/{steer_session}/attachments/{pending_upload['attachment_id']}.txt"
        )
        withdrawn_upload = upload("撤回正文\n".encode("utf-8"), "withdrawn.txt")
        withdrawn = (await service.accept_steering(SteeringCommand(
            operation_id=uid(), session_id=steer_session,
            request=SteeringRequest(
                target_run_id=steer_run.run.id, text="撤回附件",
                attachments=[withdrawn_upload],
            ),
        ))).steering
        await service.runs.withdraw_steering(steer_session, steer_run.run.id, withdrawn.id)
        assert (
            project(await service.queries.get_branch(steer_session, consumed.entry.id))
            == after_consumption
        )
        await service.runs.finish_run(steer_session, steer_run.run.id, "completed")

        return {
            "tmp_root": str(tmp_root),
            "plan_path": plan_path,
            "pure_path": projected[1][0][1],
            "steering_path": consumed_projection[0][1],
        }
    finally:
        await database.close()


def check() -> None:
    root = ROOT / uuid4().hex
    root.mkdir(parents=True)
    source_root = check_root_independent_of_cwd()
    evidence = asyncio.run(attachments_and_tools(root))
    assert evidence["tmp_root"] == str(root / "tmp")
    print(
        "PASS: 项目 tmp 根与启动 cwd 无关；真实附件可定位读取且字节完整；长附件续读；"
        "长行 read 不截断（单行超限/100,000 字节/中文/多行窗口）；"
        "workspace 可写可编辑；原附件只读；跨会话/穿越/链接拒绝；"
        "编辑/重新生成/已消费 Steering 按真实节点投影；pending 与 withdrawn 不进入上下文"
    )
    print(f"evidence: {evidence} ; source TMP_ROOT={source_root}")


if __name__ == "__main__":
    check()
