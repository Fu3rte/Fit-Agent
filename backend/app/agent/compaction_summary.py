from typing import Literal

from app.ai.messages import Model

# 与 Pi 的 convertToLlm 摘要标记保持一致：历史摘要转换为标准 UserMessage 时携带该前缀与后缀。
COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following summary:\n\n"
    "<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"


class CompactionSummaryMessage(Model):
    role: Literal["compactionSummary"]
    summary: str
    tokens_before: int
    timestamp: int | float
