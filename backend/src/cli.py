import argparse
import sys
from copy import deepcopy
from functools import partial

from src.agent.loop import run_turn
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.providers.openai import complete
from src.agent.tools.bash import create_bash_tool
from src.agent.tools.files import WORKSPACE, create_file_tools
from src.model_config import load_model_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit-Agent 最小 ReAct Agent Loop")
    parser.add_argument("--prompt", help="执行单次任务并退出")
    parser.add_argument(
        "--max-steps", type=int, default=64, help="每次任务的模型调用上限"
    )
    args = parser.parse_args()
    if args.max_steps < 1:
        parser.error("--max-steps 必须大于 0")
    tools = create_file_tools()
    tools["bash"] = create_bash_tool()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    config = load_model_config()
    with config.create_client() as client:
        request = partial(complete, client, config.OPENAI_MODEL)
        if args.prompt is not None:
            run(args.prompt, messages, request, tools, args.max_steps)
            return
        print(f"Workspace: {WORKSPACE}；输入 /exit 退出", flush=True)
        for line in terminal_lines():
            prompt = line.strip()
            if prompt == "/exit" or prompt == "q":
                return
            if prompt:
                run(prompt, messages, request, tools, args.max_steps)


def terminal_lines():
    while True:
        print("You> ", end="", flush=True)
        line = sys.stdin.readline()
        if not line:
            return
        yield line


def run(prompt, messages, request, tools, max_steps) -> None:
    pending = deepcopy(messages)
    pending.append({"role": "user", "content": prompt})
    for event in run_turn(pending, request, tools, max_steps):
        if event.event == "tool_start":
            print(
                f"Action> {event.data['name']} {event.data['arguments']}", flush=True
            )
        elif event.event == "tool_result":
            print(f"Observation> {event.data['content']}", flush=True)
        elif event.event == "message":
            print(f"Agent> {event.data['text']}", flush=True)
    messages[:] = pending
