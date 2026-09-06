# Fit-Agent PydanticAI spike：脱敏证据导出。
# 通过 guard transport + 桩 transport 跑一轮最小对话，
# 把 wire 请求证据（原始字节切片、解析体）写入 evidence/request-capture-sample.json。
# 不含任何凭据（Key 只在 Authorization 头，本脚本不记录头）。

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic_ai import RunContext  # noqa: E402

from spike_lib.capture import ScriptedTransport, deepseek_usage  # noqa: E402
from spike_lib.fee_guard import FeeGuard  # noqa: E402
from spike_lib.real_runner import build_spike_agent  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "evidence" / "request-capture-sample.json"
DUMMY_KEY = "dummy-not-a-credential"  # 合成占位符（非凭据），不进入证据文件


async def lookup_equipment(ctx: RunContext[None], equipment_name: str) -> str:
    """查询器械可用性。"""
    return f"{equipment_name}: available"


lookup_equipment.__name__ = "lookup_equipment"


def main() -> None:
    transport = ScriptedTransport(
        script=[
            {
                "usage": deepseek_usage(prompt_tokens=90, completion_tokens=5),
                "tool_call": {"id": "c1", "name": "lookup_equipment", "arguments": '{"equipment_name":"barbell"}'},
            },
            {"usage": deepseek_usage(prompt_tokens=120, completion_tokens=8), "content": "done"},
            {"usage": deepseek_usage(prompt_tokens=160, completion_tokens=6), "content": "continued"},
        ]
    )
    captured: list = []
    agent = build_spike_agent(
        "deepseek-v4-flash",
        guard=FeeGuard(),
        api_key=DUMMY_KEY,
        tools=[lookup_equipment],
        instructions="常驻层：你是 Fit-Agent 计划助手。" * 2,
        inner_transport=transport,
        captured=captured,
    )
    result1 = asyncio.run(agent.run("查看器械 barbell"))
    result2 = asyncio.run(agent.run("继续", message_history=result1.all_messages()))
    _ = result2  # 第二轮验证消息前缀追加（字节级断言在 tests/test_prefix_stability.py）

    evidence = []
    for req in captured:
        evidence.append(
            {
                "url": req.url,
                "raw_bytes_len": len(req.raw),
                "body": req.body,
                "wire_messages_elements_bytes": [m.decode("utf-8") for m in req.wire_elements("messages")],
                "wire_tools_array_bytes": req.wire_array("tools").decode("utf-8"),
            }
        )
    OUT.write_text(json.dumps({"captured": evidence}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {OUT} ({len(captured)} requests)")


if __name__ == "__main__":
    main()
