from collections.abc import Sequence
from typing import Protocol

from app.domain.session.models import SessionEntry, SessionRun
from app.domain.session.repository import SessionRepository


class SnapshotStore(Protocol):
    # 会话内容替换与删除时的画像快照协作接口，由业务服务实现。
    async def remove_for_entries(
        self, session_id: str, entry_ids: set[str]
    ) -> None: ...

    async def remove_for_session(self, session_id: str) -> None: ...


def subtree_ids(
    entries: Sequence[SessionEntry], root_id: str, *, include_root: bool
) -> set[str]:
    children: dict[str | None, list[str]] = {}
    for entry in entries:
        children.setdefault(entry.parent_id, []).append(entry.id)
    ids: set[str] = set()
    stack = list(children.get(root_id, []))
    if include_root:
        stack.append(root_id)
    while stack:
        node = stack.pop()
        if node in ids:
            continue
        ids.add(node)
        stack.extend(children.get(node, []))
    return ids


class ContentRemoval:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        self._snapshots: SnapshotStore | None = None

    def attach_snapshots(self, snapshots: SnapshotStore) -> None:
        self._snapshots = snapshots

    async def remove_content(
        self,
        session_id: str,
        target_id: str,
        entries: list[SessionEntry],
        *,
        include_target: bool,
    ) -> None:
        # 删除目标子树，并清理被移除内容关联的历史运行、Steering 与操作记录。
        deleted_ids = subtree_ids(entries, target_id, include_root=include_target)
        if not deleted_ids:
            return
        await self._repository.defer_foreign_keys()
        if self._snapshots is not None:
            # 随消息移除画像快照；已完成保存的幂等记录保留。
            await self._snapshots.remove_for_entries(session_id, deleted_ids)
        runs = await self._repository.list_runs(session_id)
        entries_by_run: dict[str, list[SessionEntry]] = {}
        for entry in entries:
            if entry.run_id is not None:
                entries_by_run.setdefault(entry.run_id, []).append(entry)
        runs_to_delete: list[SessionRun] = []
        runs_to_fix: list[tuple[SessionRun, str]] = []
        for run in runs:
            if (
                run.request_entry_id not in deleted_ids
                and run.last_entry_id not in deleted_ids
            ):
                continue
            retained = [
                entry
                for entry in entries_by_run.get(run.id, [])
                if entry.id not in deleted_ids
            ]
            if not retained:
                # 没有留存节点引用的运行随内容一并清理。
                runs_to_delete.append(run)
                continue
            # 被留存节点引用的运行保留，进度回退到留存链上最深的节点。
            parent_ids = {entry.parent_id for entry in retained}
            deepest = max(
                (entry for entry in retained if entry.id not in parent_ids),
                key=lambda item: (item.created_at, item.id),
            )
            runs_to_fix.append((run, deepest.id))
        deleted_run_ids = {run.id for run in runs_to_delete}

        steering_to_delete: list[str] = []
        for run in runs:
            for steering in await self._repository.list_steering(session_id, run.id):
                if (
                    steering.run_id in deleted_run_ids
                    or steering.entry_id in deleted_ids
                ):
                    steering_to_delete.append(steering.id)
        deleted_steering_ids = set(steering_to_delete)

        operations = await self._repository.list_operations(session_id)
        operations_to_delete = [
            operation
            for operation in operations
            if operation.run_id in deleted_run_ids
            or (
                operation.steering_id is not None
                and operation.steering_id in deleted_steering_ids
            )
        ]
        if operations_to_delete:
            await self._repository.delete_operations(
                [operation.operation_id for operation in operations_to_delete]
            )
            for operation in operations_to_delete:
                await self._repository.insert_operation_invalidation(
                    operation.operation_id, session_id
                )
        if deleted_steering_ids:
            await self._repository.delete_steering(
                session_id, sorted(deleted_steering_ids)
            )
        for run, new_last in runs_to_fix:
            await self._repository.update_run(
                run.model_copy(update={"last_entry_id": new_last})
            )
        if deleted_run_ids:
            await self._repository.delete_runs(
                session_id, sorted(deleted_run_ids)
            )
        await self._repository.delete_entries(session_id, sorted(deleted_ids))
