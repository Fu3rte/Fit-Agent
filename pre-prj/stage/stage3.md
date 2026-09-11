# Stage 3 后端详细开发计划：计划、训练记录与统计复盘

> 状态：**D1–D9 全部已拍（2026-09-11）**；可按本文件实施，本文只撰写计划、不自动启动实现。
> 本阶段验收仅要求 **Windows 全量自动化测试**，不列 Linux 验证或浏览器 E2E。自动化仍使用临时数据库，不操作真实用户库。
> 不修改 `PLAN.md`、设计正本或 Stage 1/2；本阶段门槛不替代项目最终发布验收要求。未拍事项仍以 PLAN 唯一索引及架构各章为准。

## 1. 目标与阶段关系

Stage 3 按 PLAN 粗略阶段路线实现核心业务流：**计划与训练指导 → 训练记录与更正 → 统计与复盘数据**。与 Stage 2 同构：先用内部应用层准备草稿，再走真实 HTTP 查询/纠错/确认；**无真实对话生成入口**，不得声称聊天闭环已打通。

| 链路 | 本阶段交付 | 依赖 |
|---|---|---|
| 计划 | 版本只追加、D9 payload、`calendar_cycle` 投影日程、到期即锁（规则判定）、替换时同事务取消旧版未来未锁定日程、受限组合（档案补丁＋计划）一次确认 | Stage 2 草稿确认事务、档案与限制 |
| 安排 | 当次安排接受即落盘（真实 `accepted_at`）、与日程/处方锁定分离 | 计划日程 |
| 记录 | 稳定训练身份、同日多练、完整修订留痕、转正式/作废、人工辅助为单组信息 | 计划安排快照（可空） |
| 统计 | 完成率（Wn）、三桶、PR 现算、复盘正文与 stale 标记 | 日程分母 + 最新有效修订 |

**完成 Stage 3 不等于完成第 04–06 章全部验收项，也不等于 Agent 闭环。** Stage 4 才接对话生成草稿、Run、SSE 与一键重算。

## 2. 依据与当前基线

| 依据 | 采用内容 |
|---|---|
| [PLAN.md](../../PLAN.md) | 单进程异步单体、对话唯一发起、页面只读、阶段 3 范围 |
| [01 共用应用层](../architecture/01-shared-transaction.md) 1.4–1.6 | 确认事务、受限组合、过期拦截；重算归 Stage 4 |
| [02 档案与安全限制](../architecture/02-profile-security.md) | 限制/红旗驱动计划生成与使用时复核 |
| [03 动作目录](../architecture/03-action-catalog.md) | 稳定身份、`record_type`、可推荐标记 |
| [04 计划与训练指导](../architecture/04-plan-training.md) | 版本、日程锁定、安排接受、安全复核、中断接回、动作替代 |
| [05 训练记录与更正](../architecture/05-training-records.md) | 训练身份、转正式、修订、同日多练、人工辅助 |
| [06 统计与复盘](../architecture/06-stats-reviews.md) | 完成率、三桶、PR、复盘快照与 stale |
| [07 数据与持久化](../architecture/07-data-persistence.md) | 单连接单锁、编号迁移、固定业务时区 |
| [设计决策](../design-decisions.md)「架构不变量」 | 正式事实只经确认；不新增第二套版本计数器 |
| [Stage 2 证据](evidence/S2-evidence-windows.md) §4 | 唯一写入入口、事务内约束、版本负责人 |
| [前端契约](../../frontend/src/lib/contract.ts)、[前端 Stage2 计划 mock](../../frontend/plans/stage2.md) | 计划/日程/安排/记录/统计 DTO 形状与已拍校准口径；mock 不是后端正本 |
| [test/business-table](../../test/business-table/) spike | 表职责探索参考；**不作为生产 schema 正本**，字段以本阶段迁移与架构章节为准 |

本轮只读检查基线（工作区 `5a30760` 附近，开工时重新核对）：

- 迁移已到 `004`（`business_drafts`）；`plan_versions`／`scheduled_sessions`／`arrangement_revisions`／`training_sessions`／`session_revisions`／`exercise_logs`／`training_sets`／`reviews`／`pr_candidates` **均未建**。
- `business_drafts` 仅有档案草稿形状（无 `kind`、无计划载荷、无 `proposed_profile_patch_json`、无 `parent_draft_id`）；Stage 2 明确不预建，由本阶段/Stage 4 增量迁移。
- `backend/domain/plan|records|stats` 仅为占位 docstring，不视为功能已实现。
- `exercises.recommendable` 种子恒为 0；本阶段用**系统迁移**将已核对 24 项置 1（D2 已拍 A）。
- 计划处方 `record_type`（`external_load_reps` / `bodyweight_reps` / `timed`）与目录 `record_type`（`reps_weight` / `reps_bodyweight` / `time`）及 `load_notation` 词表**在领域层做映射**，不改目录已拍词表、不发明目录外口径。
- `context_version` 唯一推进点：`ProfileRepo.bump_context_version_in_transaction`，唯一调用方 `app/confirm.py`。
- 锁不可重入；事务内必须走 `*_in_transaction`（`require_outer_transaction`）。
- Stage 1/2 Windows 证据：244 / 396 passed（既有证据；身体情况合并后阶段级全量重新取证尚未做）。开工记录实际基线，不沿用旧数字。

## 3. 范围与非目标

### 本阶段交付

1. 计划/日程/安排/记录/复盘相关业务表的连续编号迁移（及草稿表 `kind` 与计划/记录载荷列）。
2. 内部计划草稿创建：PPL 首版模板 + 档案/限制过滤 + 校准标记 + 按 `calendar_cycle` 投影日程；含可选长期档案补丁（受限组合）。
3. 计划草稿查询、结构化 Diff、轻量纠错、丢弃；确认事务：启用/替换、日程原子切换、安排接受落盘、版本恰好 +1、幂等。
4. 到期锁定的**规则判定**读取路径；使用时整份计划安全复核（限制冲突与红旗独立阻断）。
5. 记录草稿：结构化打卡事实、同日多练身份、完整修订、转正式/作废、人工辅助字段。
6. 确定性统计：完成率 Wn、三桶、`pr_candidates` 视图与 PR 现算；复盘正文存取与依据变更 stale 标记。
7. 业务只读 API 与草稿操作 API 扩展、前端契约映射交接说明。
8. Windows 全量自动化与 Stage 4 接入说明。

### 不做

- 不接 Agent、模型、聊天生成、Run 调度、SSE、Harness、一键重算（Stage 4）。
- 不做后台巡检任务、自动续期、自动跨周期切换、复杂周期系统、模板插件系统。
- 不建完整计划编辑器、任意拖拽编排、通用组合草稿引擎、第二套业务版本计数器。
- 不实现基于可信历史的自动加重/负荷建议算法（有历史后仍只提供确定性数据；生成侧建议归后续拍板）。
- 不做 Agent 解释性复盘正文的模型生成（只存 Markdown 与快照；触发：**仅显式请求时生成**，D6 已拍 A）。
- 不发布公开建草稿/直写正式计划或记录的 HTTP 入口；不改造前端页面（交接后另阶段切换）。
- 不实现「计划工作组」「已完成工作组」（D5 已拍 A：首版不进 DTO）。
- 不自动续期、不在复核日后生成新日程（D7：到期后走「新计划草稿 → 人工确认」）。
- 病后未获专业允许：**只转介，不生成任何结构化训练处方**（D4 已拍 A）。

## 4. 核心实现约束

### 4.1 草稿扩展（沿用 Stage 2 生命周期）

- 生命周期仍仅 Pending／Committed／Discarded；过期是基线冲突。
- 本阶段增量迁移为草稿增加：`kind`（至少 `profile_update` / `plan` / `training_record` / `arrangement`）、计划拟议 JSON、可空 `proposed_profile_patch_json`、记录拟议 JSON；既有档案草稿行 `kind` 回填为 `profile_update`（读旧写新兼容或一次性 UPDATE，见迁移验收）。
- `parent_draft_id` 仍不预建（Stage 4 重算）。
- 组合提交：仅「同一意图中有直接依赖的档案补丁 + 计划」；补丁保存在计划草稿内，确认前不改正式档案；「今天只能用…」不进长期补丁。
- Diff 由后端按快照计算；不信任客户端 before/after/版本/状态。

### 4.2 确认事务（扩展 §4.2 / S2-05 顺序）

顺序不变：幂等已提交 → 拒绝已丢弃 → 基线/revision → 事务内领域复查 → 原子写入 → COMMIT 后响应。

计划草稿确认额外步骤（仍在同一事务）：

1. 若有档案补丁：先校验补丁，再按**应用补丁后的拟议条件**复查计划（不能只按旧条件校验）。
2. 复查计划结构、目录引用、频率/时长/器械/限制/同日重复动作、日程与生效范围一致。
3. 替换计划：归档旧版（保留历史）→ 取消旧版未来**未锁定**日程 → 写入新版 `plan_versions` 与 `scheduled_sessions`。
4. 若有补丁：写正式档案。
5. `context_version` 恰好 +1；草稿 Committed + 凭据。
6. 任一步失败全部回滚；重复确认返回原凭据，不重复建版本/日程/安排。

安排接受：用户确认接受即写 `arrangement_revisions`（完整目标快照 + 真实 `accepted_at`），与草稿提交、版本 +1 同事务；不得会话内先当已接受、打卡时补写；接受时间不得倒填。

记录确认：追加完整 `session_revisions`（及 `exercise_logs`/`training_sets`），切换当前修订指针；更正/作废不新增训练身份、不物理删除。

事务内只做本地确定性计算与 DB 操作；不调模型/SSE；版本只由确认编排推进。

### 4.3 日程锁定与「今天」

- 锁定按**固定业务时区**的日期规则强制判定：`business_date(now) >= scheduled_on` 或该次已确认完成/漏练时视为已锁定；存储锁定字段不是唯一依据，**不需要后台锁定任务**（04；停机跨过训练日仍禁止改期/删除）。
- 读取 API 必须能同时返回存储状态与「按日期规则已锁定」的判定结果，避免字段未写导致漏锁。
- 休息槽不生成日程；完成率分母只来自应训练日程（06）。

### 4.4 计划 payload 与日程生成（D9 已拍）

**关系字段在 `plan_versions` 行上，不进 `payload_json`**：

```text
id, version, source_plan_version_id?, starts_on, review_on,
mode: regular | return, payload_json, source_draft_id, confirmed_at
```

当前计划由 `user_profile.current_plan_version_id`（或等价唯一指针）确定；`active/archived` 为展示状态，不是 payload 内容。日程区间为 `[starts_on, review_on)`。

**`payload_json` 契约（schema_version=1）**要点：

| 构件 | 语义 |
|---|---|
| `template_key?` | 来源说明（如 `ppl`），不决定结构、不是插件系统 |
| `plan_workouts[]` | 可被日历引用的训练处方；`workout_key` 版本内唯一；`name` 仅展示；`estimated_minutes` 为确认时认可的估计（不前端重估） |
| `exercises[]` | 顺序即执行顺序；`item_key` 所属 workout 内唯一；`exercise_id` 引用目录；`display_snapshot` 确认时冻结展示副本（校验仍以目录与最新限制为准） |
| `record_type` | 三类处方：`external_load_reps` / `bodyweight_reps` / `timed` |
| `prescription` | 次数型：`work_sets` + `reps_range{min,max}` + 可选 `target_rir{min,max}`；计时型：`work_sets` + `duration_seconds_range`（秒；首版不强制 RIR） |
| `load`（仅外加负重） | **互斥**：`verified`（`value/unit/load_notation/basis_record_revision_id?`）或 `needs_calibration`（`steps/pass_criteria/stop_criteria`，不得带 value、不得猜重） |
| `progression` | `method` + 明确 `rule`；与 `record_type` 匹配（见下表）；`custom` 也必须有 rule |
| `calendar_cycle` | `anchor_date` + `slots[]`（`workout`→`plan_workout_key` 或 `rest`）；**长度即循环长度**；按日历日推进，**是否完成不影响推进**；只负责首次投影日程，不负责事后重排 |
| weekday | **不进 payload**（避免与 cycle 双写）；API/UI 可派生；具体日程 weekday 由 `scheduled_on` 按业务时区计算 |

渐进 × 处方类型：

| record_type | 可用 method |
|---|---|
| external_load_reps | double_progression / repetition_progression / custom |
| bodyweight_reps | repetition_progression / custom |
| timed | duration_progression / custom |

**日程投影**：确认事务内在 `[starts_on, review_on)` 逐日取  
`slot_index = floor(date - anchor_date) mod slots.length`；workout 槽生成一条 `scheduled_sessions`（带 `plan_workout_key`），rest 槽不生成；与计划版本同事务写入。漏练保留、不移动日程、不改后续循环；补练属额外训练。

**生成输入（无 Agent）**：正式档案 + 有效限制 + 可推荐候选（D2）；缺档案/红旗 fail-closed；无可信记录一律 `needs_calibration`。候选 `starts_on`/`review_on`/`anchor_date`/slots 在内部准备时给出明确值（测试固定；通用排程算法不建）。PPL 每周三练示例 slots：`push, rest, pull, rest, legs, rest, rest`。

### 4.5 校准（D3 已拍）

- **RIR 不作校准硬性指标**（计划仍可展示 `target_rir` 供参考）。
- 通过：能稳定完成该组处方的**次数下限**（如 6–8 则下限为 6）。
- 停止：疼痛/不适、动作明显失稳，或加重后完不成次数下限；停止后不继续加重。
- 计时型无次数下限时，停止条件为疼痛/失稳/明显无法维持动作；通过标准在计时型模板中以稳定完成最短时长表达（实现写入校准文案，不另拍医学阈值）。

### 4.6 记录与统计边界

- 日期不唯一；同日多练靠稳定 `training_sessions.id`；歧义在草稿前询问（Stage 3 内部准备时显式指定归属，不模拟 NL）。
- 转正式：必填事实完整才可确认为有效修订；可确认 `incomplete` 修订承载已明确事实（统计侧整条 incomplete 不进 PR，见 05/06）。
- 三桶与 PR 只消费最新有效修订；辅助组排除普通 PR；回归期排除常规对比与 PR。
- 完成率分母锁定后不倒改；同次安排最多贡献一次完成。
- 复盘：当前统计现算；复盘保存生成时数值与来源修订引用；依据变化标记 stale，不静默改写正文。

## 5. 任务拆分与逐项验收

执行顺序：S3-01 → S3-02 → … → S3-14。优先「计划启用」一条真实链路，再记录，再统计；不为并行先造通用框架。

### S3-01：开工基线与传输契约核对

- **工作**：记录代码版本与工作区差异；核对 Stage 2 接口、迁移 004、前端 contract 中计划/记录/统计形状；列出草稿 kind 与 DTO 映射；确认 §8 待拍是否已收口。
- **依赖**：本计划 + §8 拍板完成。
- **验收**：字段/错误码/路径映射表；无把三态档案、限制稳定身份、计划/日程状态压成假完整数据的问题。
- **验证**：Windows 既有全量测试，记录实际基线（不沿用 396）。

### S3-02：业务表迁移与草稿 kind 扩展

- **工作**：连续编号迁移建 `plan_versions`、`scheduled_sessions`、`arrangement_revisions`；草稿表增加 kind/计划载荷/档案补丁列；索引与外键最小集；**系统迁移将已核对 24 项 `exercises.recommendable=1`（D2 A）**。
- **依赖**：S3-01。
- **验收**：空库、Stage 2 库升级、重复启动、失败迁移回滚、高版本拒绝；旧档案草稿仍可读可确认；不新增第二套版本计数器；仅 24 项且 id 与 Stage 1 清单一致（不多置、不静默扩目录）。
- **边界**：本任务只建计划侧表；记录侧表归 S3-09（编号连续，表集合断言显式扩展、不放宽既有旁路扫描）。
- **验证**：临时库升级与关闭重开；事务内读取不嵌套取锁；`list_recommendable` 返回恰为 24 项。

### S3-03：计划领域 schema 与生成

- **工作**：`domain/plan`：D9 payload 结构、目录 `record_type`/`load_notation` 映射、PPL 模板（`plan_workouts`+`calendar_cycle`）、器械/限制过滤、校准（D3：次数下限，RIR 非硬性）、安全前置校验、`[starts_on, review_on)` 投影日程。
- **依赖**：S3-02；可推荐集合 = 迁移置 1 的 24 项（D2 A）。
- **验收**：计划只引用可推荐且条件匹配的目录动作；同一 workout 内 `item_key` 唯一；slots 引用存在的 `workout_key`；红旗/缺档案不给处方；无可信记录无猜重；投影只含 workout 日、不含 rest 与 `review_on` 当日。
- **验证**：领域单测覆盖生成、过滤、阻断、cycle 投影与日期边界（含练三休一非 7 日循环）。

### S3-04：计划草稿创建、查询与 Diff

- **工作**：内部应用层从档案/限制/版本同一快照准备生成输入；保存 Pending 计划草稿（含可选档案补丁）；按身份/会话查询并返回结构化 Diff（计划字段 + 可选档案字段）。
- **依赖**：S3-02、S3-03。
- **验收**：建草稿前后正式计划/档案/版本不变；基线绑定读取时刻；来源不混淆；替换场景能表达旧日程取消清单预览（仅拟议，不落正式取消）。
- **验证**：读版本 V → 另一草稿提交 → 本草稿仍 V；重开后内容一致。

### S3-05：计划草稿纠错与丢弃

- **工作**：仅允许纠正计划载荷允许字段（日期/训练日/动作候选/组次/RIR 等，与前端 F2-03 对齐）；复查后 revision+1；丢弃只改状态。
- **依赖**：S3-04。
- **验收**：不能借纠错改身份/来源/基线/状态/凭据；终态不可纠错；Discarded 不可确认；重复丢弃幂等。
- **验证**：旧 revision 拒绝；非法引用/越界日期拒绝；与确认并发串行。

### S3-06：计划确认事务与日程原子切换

- **工作**：扩展确认编排支持计划草稿（含受限组合）：启用 v1、替换归档旧版、取消旧版未来未锁定日程、写新日程、可选写档案补丁、版本 +1、幂等凭据。
- **依赖**：S3-02–05；事务约束复用 S2-05/S2 证据 §4.2。
- **验收**：对应 04 验收 1–3（含 cycle 投影：rest 不生成、完成无关推进）；失败注入全回滚；重复确认不重复建版本/日程；组合一次仅 +1；「今天只能用」不进长期档案；替换只取消旧版未来未锁定日程。
- **验证**：并发确认仅一次生效；关闭重开后凭据仍在。

### S3-07：到期锁定、安全复核与只读投影

- **工作**：按业务时区规则判定锁定；当前计划/日程/安全复核只读 API（或并入既有 `/api/profile` 投影扩展，交接时统一）；请求「基于计划的指导」前置复核整份计划。
- **依赖**：S3-06。
- **验收**：04 验收 3、7、8；停机跨日后仍禁止改期/删除未锁定以外的日程；限制冲突整份阻断；红旗独立阻断；旧计划可查看。
- **验证**：固定时钟/注入业务日期的自动化（复用 S0 时区设施），不依赖真实「今天」。

### S3-08：当次安排接受落盘

- **工作**：`arrangement` 草稿与确认：写 `arrangement_revisions`（完整目标快照、真实 `accepted_at`、来源草稿）；临时调整不改长期计划版本。
- **依赖**：S3-02、S3-06。
- **验收**：04 验收 4–5；接受后立即重启仍在；版本仅 +1；重复确认幂等；事后不得倒改当次目标为事后提出的标准。
- **验证**：接受时间与记录确认时间字段分离。

### S3-09：记录表迁移与领域 schema

- **工作**：迁移建 `training_sessions`、`session_revisions`、`exercise_logs`、`training_sets`（及必要索引）；`record_type` 对齐目录三类；辅助/热身/RIR 可空语义。
- **依赖**：S3-02 编号连续；05 表职责。
- **验收**：升级兼容同 S3-02；表集合旁路扫描显式扩展；不同负重口径不混比的存储字段齐备（原始值+单位+换算键策略在领域层约定并测试）。
- **验证**：迁移与约束测试。

### S3-10：记录草稿创建、查询与纠错

- **工作**：内部准备打卡结构化草稿（动作、组次、热身摘要、安排关联可空）；Diff；允许纠错允许字段；歧义场景在内部准备时显式给出（不模拟 NL 询问 UI）。
- **依赖**：S3-09。
- **验收**：允许确认为 `incomplete` 修订（已明确事实落盘）；补全后经更正修订变 `valid`；整条 incomplete 不进 PR/完成率分子（D8 已拍 A）；未明确 RIR/质量保持空；无安排不强行套用计划。
- **验证**：隔离测试；重开一致。

### S3-11：记录确认、更正与作废

- **工作**：确认写入完整修订并切换指针；更正追加修订；作废整次退出统计；辅助标记为单组信息。
- **依赖**：S3-10。
- **验收**：05 验收 1–5；同日多练不同身份；补充同次不增加次数；作废不物理删除。
- **验证**：幂等；旧修订不重复参与统计（与 S3-12 联测）。

### S3-12：统计确定性计算

- **工作**：`pr_candidates` 视图；完成率 Wn、三桶判定（已知未满足优先）、PR 现算；回归期排除口径；分母来自已锁定应训练日程。
- **依赖**：S3-07、S3-11。
- **验收**：06 验收 1–9；分母为零显示「暂无」；同重量 PR 不累计多组；辅助/热身/草稿/作废/旧修订/回归期不进 PR。
- **验证**：固定样例表驱动测试（含更正后重算）。

### S3-13：复盘存取与 stale

- **工作**：`reviews` 表；保存 Markdown、生成时统计快照与来源修订引用；依据变化标记 stale；重生成追加不覆盖。
- **依赖**：S3-12；生成触发见 §8 D6。
- **验收**：06 验收 10；历史快照不污染当前统计。
- **验证**：更正后旧复盘标记 stale；当前统计仍现算。

### S3-14：业务 API、契约交接与 Windows 全量

- **工作**：扩展只读/草稿 API（计划、日程、安排、记录、统计、复盘查询；计划/记录草稿 revise/confirm/discard）；DTO 映射与前端待改清单；合并自动化跑 Windows 全量；Stage 4 接入说明。
- **依赖**：S3-01–13。
- **验收**：错误形状沿用 S2-07；无堆栈/凭据泄漏；无公开建草稿/直写正式事实/假重算路由；旧用例+新增全过、无新增 skip/xfail。
- **验证**：命令同 Stage 2；证据写 `pre-prj/stage/evidence/S3-evidence-windows.md`（执行时创建）。

## 6. Windows 自动化验收矩阵

复用 pytest 与临时文件库；不新增测试框架、浏览器 E2E 或模型请求。

| 组 | 必须覆盖 |
|---|---|
| 升级兼容 | 空库、Stage 0–2 升级、重复启动、失败迁移、高版本拒绝、旧档案草稿可用 |
| 草稿隔离 | 计划/记录/安排草稿创建·纠错·丢弃不写正式事实、不推进版本 |
| 计划生成 | PPL/cycle 过滤、红旗/缺档案阻断、校准无猜重、`[starts_on,review_on)` 投影与 rest 槽不生成 |
| 计划确认 | 启用/替换/组合补丁原子切换、旧未来未锁定取消、已锁定与历史保留、幂等 |
| 锁定与复核 | 业务日期规则锁定、停机跨日、限制整份阻断、红旗独立阻断 |
| 安排 | 接受即落盘、时间字段分离、重复确认、不倒填 |
| 记录 | 同日多练、转正式/更正/作废、辅助、热身摘要、无安排不套计划 |
| 统计 | 完成率分母/分子、Wn 归属、三桶顺序、PR 排除集、更正后重算 |
| 复盘 | 快照保存、stale 标记、不覆盖旧正文、不污染当前统计 |
| HTTP | 已交付端点有效/无效输入、错误码、Host/Origin 边界、无泄漏 |
| 旁路扫描 | 正式写入仅确认事务；无公开建草稿/直写/假 recalc；表集合按本阶段扩展 |

拟执行命令（实施后 Windows PowerShell，`backend/`；**本轮未执行**）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

退出码 0 且完整收集全过为门槛；不得只跑 Stage 3 新测试。证据：`pre-prj/stage/evidence/S3-evidence-windows.md`。

## 7. 第 04–06 章验收责任分配

| 验收项 | Stage 3 | 后续 |
|---|---|---|
| 04 #1–3、6–8 计划/日程/恢复/锁定 | 用真实计划链路完整验收 | — |
| 04 #4–5 安排与历史对照 | 用安排+记录对照验收 | Stage 4 对话发起 |
| 04 中断接回/动作替代完整流程 | 可先落确定性数据结构与校验；完整交互归 Stage 4+ | Stage 4 |
| 05 #1–5 记录身份/修订/辅助 | 完整验收 | Stage 4 打卡对话 |
| 06 #1–9 统计 | 完整验收 | Stage 4 解释性复盘 |
| 06 #10 复盘快照 | 验收存储与 stale | Stage 4 模型生成正文 |
| 01 #5 组合提交 | 真实档案＋计划验收（Stage 2 未完成项） | — |
| 01 #6 一键重算 | 不实现 | Stage 4 |
| 01 #7 安排接受 | 实现并验收 | — |

## 8. 决策记录（2026-09-11 会话拍板）

> 实现细节（函数名、索引、DTO 命名）不在此列。拍板后同步任务验收。

### D1 阶段粒度 —— **已拍 A**

Stage 3 一次覆盖 04→05→06（任务 S3-01–14）。若中途需暂停，可在 S3-07/S3-11 后收半程证据，不强制改契约。

### D2 可推荐动作集合 —— **已拍 A（2026-09-11）**

检查人是**开发者/数据维护**（目录策展），不是产品用户。Stage 3 增加**系统迁移**，将 Stage 1 已核对的 24 项置 `recommendable=1`（系统写入，不走草稿）。生成只查 `active=1 AND recommendable=1`，再按档案器械/限制过滤。

### D3 校准 —— **已拍（次数下限；RIR 非硬性）**

- **RIR 不作校准硬性指标**；计划仍可展示 `target_rir` 供参考。
- 通过：稳定完成该组处方**次数下限**（`reps_range.min`）。
- 停止：疼痛/不适、动作明显失稳，或完不成次数下限；停止后不继续加重。
- 计时型：稳定完成最短时长；停止为疼痛/失稳/无法维持（文案进模板，不拍医学阈值）。
- 详见 §4.5。

### D4 病后未获专业允许 —— **已拍 A**

只转介，不生成任何结构化训练处方（含最低版）。

### D5 「计划/已完成工作组」 —— **已拍 A**

首版不实现、DTO 不出现。

### D6 复盘触发 —— **已拍 A**

仅显式请求时生成并保存；不自动按周生成。Stage 3 只做存取与 stale。

### D7 复核日到达后 —— **已拍（等同 A，强调人工确认）**

不自动续期、不自动在复核日后生成新日程。流程：内部/Stage 4 生成**新的计划草稿** → 用户**人工确认**后启用新版本并投影新日程；完成率分母在旧版本区间结束后不再增加。

### D8 记录 incomplete —— **已拍 A**

允许确认为 `incomplete` 修订（已明确事实落盘）；补全后经更正修订变 `valid`；整条 incomplete 不进 PR/完成率分子。

### D9 计划 payload —— **已拍（用户提供的完整契约）**

采用 §4.4 结构：关系字段不进 payload；`plan_workouts` + `calendar_cycle`；三类 `record_type`；`load` 的 `verified`/`needs_calibration` 互斥；`display_snapshot` 冻结；weekday 仅派生；目录词表映射在领域层。**spike 细粒度行表不作正本。**

## 9. 决策边界与完成清单

- [x] D1–D9 已拍（2026-09-11）。
- [ ] S3-01–13 已实现，计划/安排/记录/统计链路真实落盘（非 API 空壳）。
- [ ] 组合提交、日程原子切换（含 calendar_cycle 投影）、安排接受即落盘、记录修订与统计现算自动化通过。
- [ ] Windows 全量自动化通过并有同版本证据（`S3-evidence-windows.md`）。
- [ ] 唯一正式写入入口仍为确认编排；无第二套版本计数器、无公开直写路由。
- [ ] 未改项目契约与设计正本；未把 Agent/重算冒充本阶段交付。

本阶段结项只表示「计划、记录与统计核心业务达到本阶段门槛」，不是完整产品或真实对话闭环。
