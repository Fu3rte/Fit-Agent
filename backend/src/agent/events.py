from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentEvent:
    event: Literal["tool_start", "tool_result", "message", "done"]
    data: dict
