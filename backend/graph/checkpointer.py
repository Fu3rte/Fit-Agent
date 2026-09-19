"""LangGraph SQLite Checkpointer 的独立存档与连接生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def open_checkpointer(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """在指定存档文件上打开 saver；退出时可靠关闭连接。"""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(resolved)) as saver:
        yield saver


def thread_config(conversation_id: str) -> dict[str, dict[str, str]]:
    """Checkpointer 的 RunnableConfig：``thread_id`` 直接用 conversation id。"""
    return {"configurable": {"thread_id": conversation_id}}
