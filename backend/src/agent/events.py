from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentEvent:
    event: Literal[
        "message_start", "message_update", "message_end", "tool_start",
        "tool_result", "steering_status", "done", "error",
    ]
    data: dict
