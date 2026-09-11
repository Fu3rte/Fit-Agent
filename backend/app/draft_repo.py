"""草稿业务表手写 SQL：``business_drafts`` 的最小持久化与事务内读取（正本 architecture/01 1.3）。

草稿表职责归第 1 章「共用应用层」（07 章责任边界已明确：``business_drafts`` 归第 1 章），
因此本 repo 与草稿生命周期代码同层放在 ``app/``，不新建 ``domain/`` 业务模块。本模块只做
存储访问：不判定生命周期、不计算 Diff、不做领域复查、不编排确认事务（那些归
``app/drafts.py`` 与 ``app/confirm.py`` 的 Stage 2 接线）。

三条硬边界：

- **草稿写入不碰正式业务数据**：``create_pending`` 在单一事务内只插入草稿行并读回，
  不改档案、不改 ``context_version``（推进只归确认事务，01 1.4）；任一步失败或被取消
  整体回滚，不留半条草稿（``under_lock`` 回调只允许单语句，多语句原子性必须走
  ``transaction()``，storage/db.py 契约）。
- **生成基线与拟议结果成对保存**：``base_profile_json`` 是生成输入读取时的正式档案快照，
  ``proposed_profile_json`` 是最终草稿内容；``base_business_version`` 来自同一业务快照，
  保存与纠错都不得改取最新版本（stage2.md §4.1）。
- **事务内访问复用外层连接**：``get_in_transaction`` 与 ``record_commit_in_transaction``
  只接受外层 ``transaction()`` 交出的连接，不自行取锁（锁不可重入；stage2.md §2），
  因此在确认事务内与其他步骤共享同一快照。

提交凭据（01 1.3「提交凭据」）是草稿行上的 ``committed_revision`` 与
``committed_business_version``；确认成功后的原样返回由 Stage 2 确认事务（S2-05）读取，
本层保证凭据只在 Pending → Committed 这一条路径上写入。纠错与丢弃（S2-04）同样只走
外层事务的条件更新：纠错只写拟议内容与 revision（身份、来源、生成基线、状态与凭据不在
SET 列表，结构上改不到），丢弃只写状态（丢弃后不能提交，由条件更新的 WHERE 保证）。
快照以序列化文本存取，不在本层解释档案结构：编解码与结构校验归应用层与档案领域模块
（与 ``run_repo`` 只存 ``payload_json``、不解释消息负载同口径），本层只保证存储层面
可判定的最小约束（状态取值、来源引用、快照 JSON 合法性由库内 CHECK 保证）。

计划草稿列（S3-04）：``proposed_plan_json`` 是计划草稿的完整拟议信封（scope 行字段 +
D9 payload + 取消预览，编解码归 ``domain/plan/schema``），``proposed_profile_patch_json``
是受限组合的拟议长期档案补丁（编解码归 ``domain/profile/schema``）；档案草稿这两列为
NULL，计划草稿的 ``proposed_profile_json`` 存拟议条件（基线应用补丁后的档案）。
安排草稿列（S3-08）：``proposed_arrangement_json`` 是当次目标的**完整**快照（绑定计划版本
与训练日 + 完整动作目标，编解码归 ``domain/plan/schema``），其余载荷列为 NULL。记录草稿列
（S3-10）：``proposed_record_json`` 是打卡草稿的完整拟议载荷（编解码归
``domain/records/schema.RecordDraftPayload``），其余载荷列为 NULL。写入
方法固定列的选择（``create_pending`` = 档案形状，``create_plan_pending`` = 计划形状，
``create_arrangement_pending`` = 安排形状，``create_record_pending`` = 记录形状），本层不解释
JSON 内容。
SQL 一律以字面量书写并参数化（README 硬规则 3）。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import aiosqlite

from storage.db import Database, require_outer_transaction

DraftStatus = Literal["pending", "committed", "discarded"]
DRAFT_STATUSES: tuple[DraftStatus, ...] = ("pending", "committed", "discarded")

# 草稿 kind（stage3.md §4.1 已拍集合）。既有档案草稿的 kind 由 006 迁移回填为
# profile_update；计划／记录／安排草稿的载荷写入与读取归 S3-04/S3-08/S3-10。
DraftKind = Literal["profile_update", "plan", "training_record", "arrangement"]
DRAFT_KINDS: tuple[DraftKind, ...] = (
    "profile_update",
    "plan",
    "training_record",
    "arrangement",
)
# 本阶段唯一经 create_pending 创建的草稿类型（其余 kind 由后续任务接入）。
PROFILE_UPDATE_KIND: DraftKind = "profile_update"

# 新建草稿的修订版本起点（01 1.4：用户所见并准备确认的草稿内容版本）
INITIAL_REVISION = 1


class InvalidDraftRow(ValueError):
    """``business_drafts`` 行无法解析为草稿结构：草稿数据损坏，不静默吞掉。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Draft:
    """一条已持久化草稿（含生成基线、拟议结果、来源与提交凭据）。

    ``base_profile_json is None`` 表示生成时未建档（对应 ``profile_json IS NULL``），与
    「全未知档案基线」的序列化文本不是同一语义：前者是「未收集」，后者是显式全未知基线。
    ``committed_revision`` / ``committed_business_version`` 仅在 ``status='committed'`` 时有值。
    """

    id: str
    kind: DraftKind
    conversation_id: str
    run_id: str | None
    base_profile_json: str | None
    proposed_profile_json: str
    proposed_plan_json: str | None
    proposed_profile_patch_json: str | None
    proposed_arrangement_json: str | None
    proposed_record_json: str | None
    base_business_version: int
    revision: int
    status: DraftStatus
    committed_revision: int | None
    committed_business_version: int | None
    created_at: str
    updated_at: str

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


def _row_to_draft(row: aiosqlite.Row) -> Draft:
    """行 → 草稿；状态在此收敛类型，非法值视为草稿数据损坏。"""
    status = str(row["status"])
    if status not in DRAFT_STATUSES:
        raise InvalidDraftRow(f"草稿状态非法：{status!r}")
    kind = str(row["kind"])
    if kind not in DRAFT_KINDS:
        raise InvalidDraftRow(f"草稿 kind 非法：{kind!r}")
    run_id = row["run_id"]
    raw_base = row["base_profile_json"]
    raw_plan = row["proposed_plan_json"]
    raw_patch = row["proposed_profile_patch_json"]
    raw_arrangement = row["proposed_arrangement_json"]
    raw_record = row["proposed_record_json"]
    committed_revision = row["committed_revision"]
    committed_business_version = row["committed_business_version"]
    return Draft(
        id=str(row["id"]),
        kind=cast(DraftKind, kind),
        conversation_id=str(row["conversation_id"]),
        run_id=None if run_id is None else str(run_id),
        base_profile_json=None if raw_base is None else str(raw_base),
        proposed_profile_json=str(row["proposed_profile_json"]),
        proposed_plan_json=None if raw_plan is None else str(raw_plan),
        proposed_profile_patch_json=None if raw_patch is None else str(raw_patch),
        proposed_arrangement_json=(
            None if raw_arrangement is None else str(raw_arrangement)
        ),
        proposed_record_json=None if raw_record is None else str(raw_record),
        base_business_version=int(row["base_business_version"]),
        revision=int(row["revision"]),
        status=cast(DraftStatus, status),
        committed_revision=(
            None if committed_revision is None else int(committed_revision)
        ),
        committed_business_version=(
            None
            if committed_business_version is None
            else int(committed_business_version)
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def _select_row(
    conn: aiosqlite.Connection, draft_id: str
) -> aiosqlite.Row | None:
    async with conn.execute(
        "SELECT id, kind, conversation_id, run_id, base_profile_json,"
        " proposed_profile_json, proposed_plan_json, proposed_profile_patch_json,"
        " proposed_arrangement_json, proposed_record_json, base_business_version,"
        " revision, status, committed_revision, committed_business_version,"
        " created_at, updated_at"
        " FROM business_drafts WHERE id = ?",
        (draft_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _require_row(conn: aiosqlite.Connection, draft_id: str) -> Draft:
    """读回刚写入的草稿行；写成功却读不到即存储状态异常，显式失败。"""
    row = await _select_row(conn, draft_id)
    if row is None:
        raise RuntimeError(f"草稿写入后读回失败：{draft_id}")
    return _row_to_draft(row)


class DraftRepo:
    """``business_drafts`` 的最小读写；生命周期判定与 Diff 计算不在本层。"""

    def __init__(self, db: Database):
        self._db = db

    async def create_pending(
        self,
        *,
        draft_id: str,
        conversation_id: str,
        run_id: str | None,
        base_profile_json: str | None,
        proposed_profile_json: str,
        base_business_version: int,
    ) -> Draft:
        """保存一条 Pending 档案草稿（``kind='profile_update'``，revision 从 1 起）并返回落库后的行。

        ``base_profile_json``／``base_business_version`` 取生成输入读取时的同一业务快照
        （未建档基线传 None：不得当成「无限制／无症状」）；``proposed_profile_json`` 是最终
        草稿内容。``run_id`` 可空——本阶段没有真实模型执行，不得凭空伪造一次 Run
        （stage2.md §4.1）。本方法只写档案形状草稿：kind 恒为 ``profile_update``，计划载荷列
        为 NULL，计划草稿写入归 :meth:`create_plan_pending`（S3-04）。本方法不写正式档案、
        不推进 ``context_version``、不校验领域规则。

        插入与读回在单一事务内原子完成（``under_lock`` 回调契约只允许单语句，多语句
        原子性必须走 ``transaction()``）：任一步失败或被取消，整体回滚，库里不留半条
        草稿。来源配对（Run 必须属于同一会话）由 004 迁移的组合外键在库层拒绝。
        """
        return await self._insert_pending(
            draft_id=draft_id,
            kind=PROFILE_UPDATE_KIND,
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=base_profile_json,
            proposed_profile_json=proposed_profile_json,
            proposed_plan_json=None,
            proposed_profile_patch_json=None,
            proposed_arrangement_json=None,
            proposed_record_json=None,
            base_business_version=base_business_version,
        )

    async def create_plan_pending(
        self,
        *,
        draft_id: str,
        conversation_id: str,
        run_id: str | None,
        base_profile_json: str | None,
        proposed_profile_json: str,
        proposed_plan_json: str,
        proposed_profile_patch_json: str | None,
        base_business_version: int,
    ) -> Draft:
        """保存一条 Pending 计划草稿（``kind='plan'``，revision 从 1 起）并返回落库后的行。

        与 :meth:`create_pending` 同口径：基线（``base_profile_json``／
        ``base_business_version``）只取生成输入读取时的同一业务快照；``proposed_plan_json``
        是完整拟议信封（必填，含取消预览），``proposed_profile_patch_json`` 是可空拟议档案
        补丁（受限组合）；``proposed_profile_json`` 存拟议条件（基线应用补丁后的档案，编解码
        归调用方）。本方法只写草稿行：不写正式计划、不写正式档案、不取消任何正式日程、
        不推进 ``context_version``，也不解释 JSON 内容（结构校验归领域层与应用层）。

        ``proposed_plan_json`` 必填（库内 CHECK 只保证 JSON 合法性）；计划/记录/安排列
        的 kind 与列配对由写入方法固定，不在本层靠运行期约定。
        """
        return await self._insert_pending(
            draft_id=draft_id,
            kind="plan",
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=base_profile_json,
            proposed_profile_json=proposed_profile_json,
            proposed_plan_json=proposed_plan_json,
            proposed_profile_patch_json=proposed_profile_patch_json,
            proposed_arrangement_json=None,
            proposed_record_json=None,
            base_business_version=base_business_version,
        )

    async def create_arrangement_pending(
        self,
        *,
        draft_id: str,
        conversation_id: str,
        run_id: str | None,
        base_profile_json: str | None,
        proposed_profile_json: str,
        proposed_arrangement_json: str,
        base_business_version: int,
    ) -> Draft:
        """保存一条 Pending 安排草稿（``kind='arrangement'``，revision 从 1 起）并返回落库后的行。

        与 :meth:`create_plan_pending` 同口径：基线（``base_profile_json``／
        ``base_business_version``）只取准备输入读取时的同一业务快照；
        ``proposed_arrangement_json`` 是当次目标**完整**快照（绑定计划版本与训练日 + 完整
        动作目标，编解码归 ``domain/plan/schema``），不存差异补丁；``proposed_profile_json``
        存准备时的正式档案（安排不携带档案意图，该列只承载 NOT NULL 的形状）。本方法只写
        草稿行：不写 ``arrangement_revisions``、不写计划／档案、不推进 ``context_version``，
        也不解释 JSON 内容（结构与绑定校验归领域层 S3-08）。
        """
        return await self._insert_pending(
            draft_id=draft_id,
            kind="arrangement",
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=base_profile_json,
            proposed_profile_json=proposed_profile_json,
            proposed_plan_json=None,
            proposed_profile_patch_json=None,
            proposed_arrangement_json=proposed_arrangement_json,
            proposed_record_json=None,
            base_business_version=base_business_version,
        )

    async def create_record_pending(
        self,
        *,
        draft_id: str,
        conversation_id: str,
        run_id: str | None,
        base_profile_json: str | None,
        proposed_profile_json: str,
        proposed_record_json: str,
        base_business_version: int,
    ) -> Draft:
        """保存一条 Pending 训练记录草稿（``kind='training_record'``，revision 从 1 起）。

        与 :meth:`create_arrangement_pending` 同口径：基线（``base_profile_json``／
        ``base_business_version``）只取准备输入读取时的同一业务快照；
        ``proposed_record_json`` 是打卡草稿的完整拟议载荷（含显式训练身份归属与可空安排关联，
        编解码归 ``domain/records/schema``），不存差异补丁；``proposed_profile_json`` 存准备时
        的正式档案（记录草稿不携带档案意图，该列只承载 NOT NULL 的形状）。本方法只写草稿行：
        不写 ``training_sessions``／``session_revisions``／``exercise_logs``／``training_sets``，
        不推进 ``context_version``，也不解释 JSON 内容（结构与事实校验归领域层 S3-10）。
        """
        return await self._insert_pending(
            draft_id=draft_id,
            kind="training_record",
            conversation_id=conversation_id,
            run_id=run_id,
            base_profile_json=base_profile_json,
            proposed_profile_json=proposed_profile_json,
            proposed_plan_json=None,
            proposed_profile_patch_json=None,
            proposed_arrangement_json=None,
            proposed_record_json=proposed_record_json,
            base_business_version=base_business_version,
        )

    async def _insert_pending(
        self,
        *,
        draft_id: str,
        kind: DraftKind,
        conversation_id: str,
        run_id: str | None,
        base_profile_json: str | None,
        proposed_profile_json: str,
        proposed_plan_json: str | None,
        proposed_profile_patch_json: str | None,
        proposed_arrangement_json: str | None,
        proposed_record_json: str | None,
        base_business_version: int,
    ) -> Draft:
        """单一事务内插入 Pending 草稿并读回；列与 kind 的配对由调用方法固定。"""
        now = _now()

        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
                " base_profile_json, proposed_profile_json, base_business_version,"
                " revision, status, committed_revision, committed_business_version,"
                " created_at, updated_at, proposed_plan_json,"
                " proposed_profile_patch_json, proposed_arrangement_json,"
                " proposed_record_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, ?, ?, ?,"
                " ?)",
                (
                    draft_id,
                    kind,
                    conversation_id,
                    run_id,
                    base_profile_json,
                    proposed_profile_json,
                    base_business_version,
                    INITIAL_REVISION,
                    now,
                    now,
                    proposed_plan_json,
                    proposed_profile_patch_json,
                    proposed_arrangement_json,
                    proposed_record_json,
                ),
            )
            return await _require_row(conn, draft_id)

    async def get(self, draft_id: str) -> Draft | None:
        """按身份读取当前草稿状态；不存在返回 None（不创建新草稿）。"""

        async def op(conn: aiosqlite.Connection) -> Draft | None:
            row = await _select_row(conn, draft_id)
            return None if row is None else _row_to_draft(row)

        return await self._db.under_lock(op)

    async def get_in_transaction(
        self, conn: aiosqlite.Connection, draft_id: str
    ) -> Draft | None:
        """在**外层事务**内按身份读取草稿（同一快照，不嵌套取锁）。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：确认事务（S2-05）要先读草稿
        再判定幂等／状态／revision，这些判定必须与后续写入在同一事务快照里。
        """
        require_outer_transaction(conn, "草稿读取")
        row = await _select_row(conn, draft_id)
        return None if row is None else _row_to_draft(row)

    async def list_for_conversation(
        self, conversation_id: str, *, kind: DraftKind | None = None
    ) -> tuple[Draft, ...]:
        """该会话已持久化草稿的当前状态（不依赖历史通知，01 1.2）。

        ``kind`` 给出时只返回该 kind 的草稿（计划草稿查询用）；缺省返回全部 kind，保持
        Stage 2 档案草稿列表语义。单条字面量 SQL：kind 过滤用参数化条件表达，不拼接语句。
        """
        params: tuple[object, ...] = (conversation_id, kind, kind)

        async def op(conn: aiosqlite.Connection) -> tuple[Draft, ...]:
            async with conn.execute(
                "SELECT id, kind, conversation_id, run_id, base_profile_json,"
                " proposed_profile_json, proposed_plan_json,"
                " proposed_profile_patch_json, proposed_arrangement_json,"
                " proposed_record_json, base_business_version, revision, status,"
                " committed_revision, committed_business_version, created_at, updated_at"
                " FROM business_drafts WHERE conversation_id = ?"
                " AND (? IS NULL OR kind = ?)"
                " ORDER BY created_at, id",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(_row_to_draft(row) for row in rows)

        return await self._db.under_lock(op)

    async def record_commit_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        draft_id: str,
        committed_revision: int,
        committed_business_version: int,
    ) -> Draft:
        """在**外层事务**内把草稿置为 Committed 并保存提交凭据，返回落库后的行。

        条件更新只对仍为 Pending 且 revision 未被改动的草稿生效：不命中即拒绝（并发确认、
        已丢弃或已提交的草稿不会被重复写入凭据，01 1.4）。正式业务事实写入与
        ``context_version +1`` 由确认事务的其余步骤负责，本方法只写草稿行。
        """
        require_outer_transaction(conn, "草稿提交凭据写入")
        cursor = await conn.execute(
            "UPDATE business_drafts SET status = 'committed', committed_revision = ?,"
            " committed_business_version = ?, updated_at = ?"
            " WHERE id = ? AND status = 'pending' AND revision = ?",
            (
                committed_revision,
                committed_business_version,
                _now(),
                draft_id,
                committed_revision,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "草稿不存在、已非 Pending 或所见 revision 已过期："
                    f"{draft_id}（revision={committed_revision}）"
                )
        finally:
            await cursor.close()
        return await _require_row(conn, draft_id)

    async def update_proposal_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        draft_id: str,
        proposed_profile_json: str,
        expected_revision: int,
    ) -> Draft:
        """在**外层事务**内纠错 Pending 草稿：CAS 更新拟议内容并递增 revision。

        纠错只写拟议内容、revision 与更新时间（stage2.md §4.1：成功后 revision 递增并
        重算拟议结果；保留原业务基线，不自动重基到最新正式数据）：身份、来源、基线快照、
        ``base_business_version``、状态与提交凭据都不在 SET 列表里，结构上不可能被纠错
        改写。条件更新只对仍为 Pending 且 revision 仍等于所见值的草稿生效：不命中即拒绝
        （两个页面不得用同一所见 revision 静默互相覆盖，S2-04 并发约束）。确认事务
        （S2-05）与丢弃共用唯一锁与连接，最终状态判定串行。Diff 不单独存储，由读取方
        按基线与拟议快照重算，与 revision 同源、无第二份可失步的副本。
        """
        require_outer_transaction(conn, "草稿纠错写入")
        next_revision = expected_revision + 1
        cursor = await conn.execute(
            "UPDATE business_drafts SET proposed_profile_json = ?, revision = ?,"
            " updated_at = ?"
            " WHERE id = ? AND status = 'pending' AND revision = ?",
            (
                proposed_profile_json,
                next_revision,
                _now(),
                draft_id,
                expected_revision,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "草稿不存在、已非 Pending 或所见 revision 已过期："
                    f"{draft_id}（revision={expected_revision}）"
                )
        finally:
            await cursor.close()
        return await _require_row(conn, draft_id)

    async def update_plan_proposal_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        draft_id: str,
        proposed_plan_json: str,
        expected_revision: int,
    ) -> Draft:
        """在**外层事务**内纠错 Pending 计划草稿：CAS 更新拟议信封并递增 revision。

        与 :meth:`update_proposal_in_transaction` 同口径，只是写计划形状的列
        （``proposed_plan_json``）且固定列选择不变：草稿身份、来源、``base_profile_json``／
        ``base_business_version``／``proposed_profile_json``／``proposed_profile_patch_json``、
        状态与提交凭据都不在 SET 列表里，结构上不可能被纠错改写（stage3.md §5 S3-05：
        不能借纠错改身份／来源／基线／状态／凭据）。条件更新只对仍为 Pending 且 revision
        仍等于所见值的草稿生效：不命中即拒绝（S3-05 旧 revision 拒绝）；确认事务与丢弃
        共用唯一锁与连接，最终状态判定串行（01 1.4）。
        """
        require_outer_transaction(conn, "计划草稿纠错写入")
        next_revision = expected_revision + 1
        cursor = await conn.execute(
            "UPDATE business_drafts SET proposed_plan_json = ?, revision = ?,"
            " updated_at = ?"
            " WHERE id = ? AND status = 'pending' AND revision = ?",
            (
                proposed_plan_json,
                next_revision,
                _now(),
                draft_id,
                expected_revision,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "草稿不存在、已非 Pending 或所见 revision 已过期："
                    f"{draft_id}（revision={expected_revision}）"
                )
        finally:
            await cursor.close()
        return await _require_row(conn, draft_id)

    async def update_record_proposal_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        draft_id: str,
        proposed_record_json: str,
        expected_revision: int,
    ) -> Draft:
        """在**外层事务**内纠错 Pending 记录草稿：CAS 更新拟议载荷并递增 revision。

        与 :meth:`update_plan_proposal_in_transaction` 同口径，只写记录形状的列
        （``proposed_record_json``）：草稿身份、来源、``base_profile_json``／
        ``base_business_version``／``proposed_profile_json`` 与状态、提交凭据都不在 SET 列表里，
        结构上不可能被纠错改写（S3-05 同口径）；归属与安排关联的「不可改」在领域层白名单
        （``rules.validate_record_draft_correction``）强制。条件更新只对仍为 Pending 且 revision
        仍等于所见值的草稿生效：不命中即拒绝（旧 revision 拒绝）；确认事务与丢弃共用唯一锁。
        """
        require_outer_transaction(conn, "记录草稿纠错写入")
        next_revision = expected_revision + 1
        cursor = await conn.execute(
            "UPDATE business_drafts SET proposed_record_json = ?, revision = ?,"
            " updated_at = ?"
            " WHERE id = ? AND status = 'pending' AND revision = ?",
            (
                proposed_record_json,
                next_revision,
                _now(),
                draft_id,
                expected_revision,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "草稿不存在、已非 Pending 或所见 revision 已过期："
                    f"{draft_id}（revision={expected_revision}）"
                )
        finally:
            await cursor.close()
        return await _require_row(conn, draft_id)

    async def record_discard_in_transaction(
        self, conn: aiosqlite.Connection, *, draft_id: str
    ) -> Draft:
        """在**外层事务**内把 Pending 草稿置为 Discarded：丢弃只改变草稿状态。

        只写 status 与更新时间：revision、拟议内容、生成基线、来源与提交凭据一律不动
        （Pending 本就无凭据，库内 CHECK 同进同退）；正式档案与 ``context_version`` 由
        调用方保证不碰——丢弃不新增任何正式副作用（01 1.3）。条件更新只对仍为 Pending
        的草稿生效：已 Committed 的草稿不可被丢弃撤销；重复丢弃由服务层幂等返回，不走
        本方法（不重复写入、不刷新更新时间）。
        """
        require_outer_transaction(conn, "草稿丢弃写入")
        cursor = await conn.execute(
            "UPDATE business_drafts SET status = 'discarded', updated_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (_now(), draft_id),
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(f"草稿不存在或已非 Pending，无法丢弃：{draft_id}")
        finally:
            await cursor.close()
        return await _require_row(conn, draft_id)
