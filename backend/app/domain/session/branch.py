from collections.abc import Sequence

from app.ai.messages import Message
from app.domain.session.models import SessionEntry, SessionMessageEntry


def build_session_path(
    session_id: str,
    entries: Sequence[SessionEntry],
    leaf_id: str | None,
) -> list[SessionEntry]:
    index: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.session_id != session_id:
            raise ValueError(f"节点 {entry.id} 不属于会话 {session_id}")
        if entry.id in index:
            raise ValueError(f"重复节点 ID: {entry.id}")
        index[entry.id] = entry
    if leaf_id is None:
        return []
    if (leaf := index.get(leaf_id)) is None:
        raise ValueError(f"未知 leaf 节点: {leaf_id}")
    path: list[SessionEntry] = []
    visited: set[str] = set()
    current: SessionEntry | None = leaf
    while current is not None:
        if current.id in visited:
            raise ValueError(f"节点自引用或存在环: {current.id}")
        visited.add(current.id)
        path.append(current)
        if current.parent_id is None:
            break
        parent = index.get(current.parent_id)
        if parent is None:
            raise ValueError(f"缺少父节点: {current.parent_id}")
        current = parent
    path.reverse()
    return path


def message_entries(path: Sequence[SessionEntry]) -> list[SessionMessageEntry]:
    # 业务读取与工具配对只取真实消息节点；压缩节点保持结构身份但不贡献模型正文。
    return [entry for entry in path if isinstance(entry, SessionMessageEntry)]


def build_session_context(
    session_id: str,
    entries: Sequence[SessionEntry],
    leaf_id: str | None,
) -> list[Message]:
    return [
        message
        for entry in message_entries(build_session_path(session_id, entries, leaf_id))
        for message in entry.messages
    ]
