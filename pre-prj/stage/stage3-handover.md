# Stage 3 交接（S3-14）：业务 API、前端契约映射与 Stage 4 接入说明

> 本文件只做 S3-14 的交接汇总，不新增功能、不修改 `PLAN.md` / `design-decisions.md` / `architecture/01–10`
> / `stage3.md` / Stage 1–2 工件。前端源码本轮**未改**（并发所有权归页面/styling owner），
> 本文的「前端待改清单」只是映射说明。契约形状以 `frontend/src/lib/contract.ts` 与会话内 DTO 为准；
> 冲突时以后端本轮实现 + 本文映射为准，需要前端改的项见第 3 节。

## 1. Stage 3 HTTP 面（S3-14 交付）

全部沿用既有路由位置（`api/routes_readonly.py`、`api/routes_drafts.py`）与 S2-07 错误形状
`{"http_status", "error_code", "message", "detail"?}`；错误码只用前端契约已登记值
（`invalid_request` / `draft_stale` / `draft_modified`），不新增业务 error_code。Host/Origin 边界
（10.1）对所有新端点生效。

| 方法 | 路径 | 应用层入口 | 说明 |
|---|---|---|---|
| GET | `/api/plan` | `PlanReadService.read_current_plan` | 当前计划 + 全部日程（含取消/锁定）；无计划 `{"plan": null}` |
| GET | `/api/plans/{plan_version_id}` | `PlanReadService.read_plan_version` | 含历史版本（不重激活）；不存在 404 |
| GET | `/api/plan/guidance` | `PlanReadService.read_current_plan_guidance` / `read_arrangement_guidance` | 整份计划安全复核；可选 `?arrangement_revision_id=` 按当次条件复核绑定版本 |
| GET | `/api/records` | `RecordReadService.list_records` | 稳定身份 + 当前修订事实 |
| GET | `/api/records/{session_id}` | `RecordReadService.read_record` | 不存在 404 |
| GET | `/api/records/{session_id}/judgement` | `StatsService.judge_session` | 三桶判定；无对照/已作废 `null` |
| GET | `/api/stats/completion` | `StatsService.weekly_completion` | `?plan_version_id=&week_no=`；无到期名额 `null`（「暂无」） |
| GET | `/api/stats/pr` | `StatsService.pr_max_load` / `pr_max_reps_at_load` | `?exercise_id=&load_notation=[&load_kg_key=]`；无候选 `null` |
| GET | `/api/reviews` | `ReviewStore.list_reviews` | 追加语义，旧复盘仍可读 |
| GET | `/api/reviews/{review_id}` | `ReviewStore.read_review` | 不存在 404 |
| GET | `/api/sessions/{id}/drafts` | `DraftRepo.list_for_conversation` + 各 kind 查询入口 | 返回**全部 kind** 草稿 |
| GET | `/api/drafts/{draft_id}` | 按 kind 分派 | 载荷按 kind 映射，不按错形状解码 |
| POST | `/api/drafts/{id}/revise` | `DraftService.revise_profile_draft` / `PlanDraftService.revise_plan_draft` / `RecordDraftService.revise_record_draft` | body `{revision, payload}`；计划/记录载荷用存储契约形状 |
| POST | `/api/drafts/{id}/confirm` | `ConfirmService.confirm_{profile,plan,arrangement,record}_draft` | body `{revision}`；计划确认业务日期由服务端按固定业务时区注入 |
| POST | `/api/drafts/{id}/void` | `ConfirmService.void_record_draft` | 仅记录草稿；追加 `voided` 修订 |
| POST | `/api/drafts/{id}/discard` | `DraftService.discard_draft` / `PlanDraftService.discard_plan_draft` / `RecordDraftService.discard_record_draft` | 只改草稿状态 |

未提供的入口（旁路扫描断言）：公开建草稿 `POST /api/drafts`、重算 `.../recalc`、直写正式事实
（`PUT/POST /api/plan|records|reviews`）、Agent/Run/聊天/模型/SSE（`/api/runs`、`/api/chat`、
`/api/models`、`/api/events`）。计划/日程投影只在 `app/plan_reads.py` 一份，`api/` 不持 SQL、不重建投影。

### 安全阻断映射（已拍 A，S3-07 残留③）

`GET /api/plan/guidance` 的 `guidance.safety` 字段：

```jsonc
{
  "context_version": 1, "reviewed_at": "…",
  "usable": false,                    // = 未阻断；false 时不给任何基于计划的指导
  "red_flag_blocked": false,          // 红旗独立阻断
  "conflicts": [ {"exercise_id","exercise_name","restriction":{"scope","target"},"matched_modes"} ],
  "action_unavailable": true,         // 计划引用的目录身份已读不到（契约外损坏态）
  "block_code": "plan_action_unavailable",  // 具体用户可见阻断，不降级为「需澄清」、不当作「无冲突」
  "unknown_exercise_ids": ["barbell-bench-press"],
  "reasons": [...], "clarifications": [...]   // clarifications ≠ 安全放行
}
```

计划与历史仍可查看；`usable=false` 时不输出可执行处方。带 `?arrangement_revision_id=` 时按该
已接受安排**绑定的计划版本**与该当次目标动作一并复核（04 4.3：未来安排使用时仍须按最新限制/红旗复核）。

## 2. DTO 形状要点

- **计划**：行字段（`id`/`version`/`starts_on`/`review_on`/`mode`/`is_current`/`confirmed_at`）+ `plan`
  （D9 `payload_to_json` 的 JSON，`schema_version=1`）+ `schedules[]`。日程：`{id, plan_version_id,
  plan_workout_key, scheduled_on, weekday(1=周一), cancelled, cancelled_at, locked_at,
  lock:{stored,by_business_date,effective}, status: cancelled|locked|scheduled}`。
- **记录**：`{id, created_at, revision:{id,revision_no,status,occurred_on}|null, record:<存储契约形状>}`。
- **统计**：完成率 `{plan_version_id, week_no, week_start, week_end, planned, completed, rate|null}`；
  三桶 `{met, unmet, pending}`；PR `{exercise_id, load_notation, load_kg_key|null, max_load_kg_key|null, best_reps|null}`。
- **复盘**：`{id, body_markdown, stale, generated_at, source_revision_ids, basis:{schema_version, per_week, prs}}`。
- **草稿**：公共 `{id, kind, status, revision, base_business_version, committed_revision, committed_business_version}`；
  `payload` 按 kind（profile/plan/record/arrangement）；`diff` 为结构化字段对（`{field, before, after, changed}`）。

## 3. 前端待改清单（前端本轮未改，需页面 owner 收口）

| # | 现状（contract.ts / 页面） | 后端交付 | 待改 |
|---|---|---|---|
| F1 | `ProfileResponse.plan/schedules/plan_safety` 期望 `/api/profile` 携带 | 未改 `/api/profile`（保持 S2-07 精确形状与旧用例），计划读在 `/api/plan`、安全复核在 `/api/plan/guidance` | 页面改调新只读端点，或后续另行拍板合并投影 |
| F2 | `DraftKind = training_record \| plan_adjust \| profile_update` | 后端 kind：`profile_update`/`plan`/`training_record`/`arrangement` | 前端加 `plan` 与 `arrangement`，或改名映射 |
| F3 | `ReviseRequest = {payload}`（无 revision） | `POST .../revise` 要求 `{revision, payload}` | 前端携带所见 `revision`（否则 400） |
| F4 | `ConfirmResult = {draft_id, status, newly_committed, context_version, summary}` | `{draft_id, status:"committed", committed_revision, committed_business_version, (+ plan_version(_id)/arrangement_revision*/training_session_id/session_revision_id/revision_no)}` | 前端改用后端提交凭据字段（幂等重放返回同一份） |
| F5 | `PlanVersion`/`PlanScheduleEntry`（展示形状） | `plan` 为 D9 payload；`schedules[]` 字段名不同 | 前端加映射（或由页面 mock 改口径） |
| F6 | `TrainingRecord`（扁平展示形状） | `record` 为存储契约形状（`exercises[].facts/sets`） | 前端加映射 |
| F7 | `GET /api/stats`（聚合 `StatsSummary`） | 提供 `/api/stats/completion` 与 `/api/stats/pr`（现算、无聚合） | 前端聚合，或后续拍板聚合端点 |
| F8 | `GET /api/review`（单文档 `{text, stale, generated_at}`） | `/api/reviews`（列表）与 `/api/reviews/{id}`，正文字段 `body_markdown` | 前端改路径与字段名 |
| F9 | 计划草案载荷的 `candidates`（可替换动作候选） | 未提供 HTTP 候选端点（生成侧内部） | 留待后续任务；不扩目录 |
| F10 | `DraftStatus` 含 `stale` | 后端草稿状态仅 `pending/committed/discarded`，过期是 409 `draft_stale` | 前端把 stale 当过渡标记，不落成草稿状态 |

## 4. Stage 4 接入说明（Agent/对话/Run/SSE/重算）

- **草稿创建仍只在内部应用层**（`PlanDraftService.create_plan_draft`、`RecordDraftService.create_record_draft`、
  `ArrangementDraftService.create_arrangement_draft`、`DraftService.create_profile_draft`）：Stage 4 的对话/工具层调用它们产 Pending 草稿，
  再经本轮的 `revise/confirm/discard/void` 端点落正式事实；**不要**新增 HTTP 建草稿入口。
- **一键重算**（01 1.6）不实现：`parent_draft_id` 未建；Stage 4 按草稿身份/基线/内容生成新草稿时再接。
- **Run/SSE/聊天/模型**：`runtime/` 与 `/api/runs`、`/api/chat`、`/api/events` 仍未接线，本轮未碰。
- **复盘正文生成**：Stage 3 只存取；Stage 4 在**显式请求**（D6）时生成 Markdown，经 `app/review_store.py:ReviewStore.save_review`
  保存（同一事务校验来源修订是当前修订）；HTTP 面仅为查询（本轮），保存面保持内部 seam。
- **计划安全复核**：任何「基于计划的指导」必须走 `PlanReadService` 复核并尊重 `usable=false`
  与 `block_code=plan_action_unavailable`；不得在生成侧绕过。
- **业务日期**：计划确认与到期锁定按固定业务时区（`api/deps.current_business_date`，07 7.3），
  Stage 4 不得改用客户端时钟。
- **上下文版本**：每次确认恰好 `+1`；前端据 409 `draft_stale` 重新准备草稿；记录草稿过期同口径。

## 5. 本轮边界

未接 Agent/模型/聊天生成/Run/SSE/Harness/一键重算；未改前端；未改 Stage 1–2 工件与冻结契约；
未新增依赖；未新增公开建草稿/直写正式事实/假重算路由。Windows 全量结项证据见
`evidence/S3-evidence-windows.md`（S3-14 行；本轮在 Linux 取证，Windows 命令未实际执行）。
