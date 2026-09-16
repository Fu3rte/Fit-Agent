"""LangGraph SQLite Checkpointer 的独立存档与连接生命周期（讨论总结 §5.1／§9、REFACTOR_PLAN
§5.6、stage3.md §5）。

- **用已安装的 saver**：``langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver``。checkpoint 表由 saver
  自己懒建在独立 SQLite 文件里，本层不自研 checkpoint repo，也不写业务迁移。
- **与业务库分离**：独立文件（``config.checkpoint_database_path``）＋独立连接，不与业务写锁竞争；
  业务库表集合因此不因接入 checkpointer 而变化。
- **生命周期**：连接在 FastAPI 异步生命周期内创建并关闭（``api/app.py`` lifespan），本层只提供
  上下文管理器，测试用临时文件。
- **线程身份**：``thread_id`` 就是 ``WorkflowState.conversation_id``（:func:`thread_config`），
  不维护第二套线程映射。
- **只负责存档**：确认、拒绝、激活与「checkpoint 优先／业务 draft 兜底」的唯一提交入口留 Stage 5。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def open_checkpointer(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """在指定存档文件上打开 saver；退出时可靠关闭连接，不留悬挂的 SQLite 连接。

    路径由调用方独立给出（默认取 ``config.checkpoint_database_path``），父目录不存在时创建。
    """
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(resolved)) as saver:
        yield saver


def thread_config(conversation_id: str) -> dict[str, dict[str, str]]:
    """Checkpointer 的 RunnableConfig：``thread_id`` 直接用 conversation id，无第二套映射。"""
    return {"configurable": {"thread_id": conversation_id}}
