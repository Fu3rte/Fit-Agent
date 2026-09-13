# Stage 6 传输对照表与 F1–F10 收口结论（F6-00 契约冻结）

> 状态：**联调正本（冻结）**；对应 stage6.md F6-00。本文件冻结「前端最终消费的传输面」：路径、方法、请求体关键字段、响应投影要点、错误码。只写文档，不改代码。
> 权威来源与优先级：后端实际路由（`backend/api/routes_chat.py` / `routes_readonly.py` / `routes_drafts.py`）+ stage4.md §6 冻结拼写 + stage3-handover §1–3。与前端 `contract.ts` / mock 私有端点冲突时，**以后端实际 + stage4 §6 为准**，前端改适配。
> 已拍依据：stage6.md §8 A–F（2026-09-13 owner 拍板）；handover F1–F10 方向。
> 错误形状统一：`{"http_status","error_code","message","detail"?}`。业务 `error_code` 封闭集：`invalid_request` / `draft_stale` / `draft_modified`（drafts/readonly 面）+ `conversation_busy`（运行时 HTTP 409）。Run 终态原因（非 HTTP 码）：`interrupted_by_restart` / `model_request_timeout` / `run_timeout` / `context_budget_exceeded` / `model_request_failed`（`backend/runtime/error_codes.py` 封闭集）。

## 1. 传输对照表（联调正本）

### 1.1 会话 / Run / SSE（`routes_chat.py`；stage4 §6 拼写逐字冻结）

| 能力 | 前端最终路径 | 方法 | 请求体关键字段 | 响应投影要点 | 错误码 |
| --- | --- | --- | --- | --- | --- |
| 会话创建 | `/api/sessions` | POST | `{}`（空对象，不接受客户端指定身份） | `session_dto`（会话 + Run 列表 + 消息） | 400 `invalid_request` |
| 会话查询（断线/刷新恢复，已拍 E1） | `/api/sessions/{session_id}` | GET | — | 会话 + 全部 Run + 已保存消息（未完成回答标 `complete=false`）；**取代** mock 的 `/api/runs/active` 与 `/api/sessions/{id}/messages` | 404 `invalid_request` |
| 用户请求提交 | `/api/sessions/{session_id}/requests` | POST | `{client_request_id, text}`（均非空字符串） | `{created: bool, run}`；相同 `client_request_id` 幂等返回已有 Run 且不重启；不同请求遇全局活跃 Run 409 | 400 `invalid_request`；404 会话不存在；409 `conversation_busy` |
| Run 查询 | `/api/runs/{run_id}` | GET | — | `{run}`；五态权威 `status`（pending/running/completed/failed/cancelled）+ `error_code`（Run 终态原因）+ `retry_of_run_id` + `conversation_id` + 时间戳 | 404 `invalid_request` |
| Run 取消 | `/api/runs/{run_id}/cancel` | POST | `{}`（空对象） | `{run}`（条件更新为 `cancelled` 后中断底层调用）；SSE 断开/刷新**不**取消 | 404；409 `invalid_request`（终态重复取消） |
| Run 重试 | `/api/runs/{run_id}/retry` | POST | `{client_request_id}` | `{created, run}`；用旧 Run 同一请求事实创建**新** Run（`retry_of_run_id`），不复活旧记录 | 400；404；409 `conversation_busy` |
| SSE 事件流 | `/api/runs/{run_id}/events` | GET | —（EventSource） | `text/event-stream`；只发产品事件白名单（状态/回答块/依据与说明/草稿引用/压缩状态/heartbeat）；复盘 Run **仅状态事件**；15s heartbeat（传输层，不入库）；终态即结束；断线不取消、不重跑、不重放 | 404 `invalid_request` |
| 重算（重新生成草稿） | `/api/drafts/{draft_id}/recalc` | POST | `{client_request_id}` | `{created, run}`；幂等先于 busy；新草稿带 `parent_draft_id`，旧草稿不变 | 400；404；409 `conversation_busy` |
| 显式复盘生成 | `/api/reviews` | POST | `{client_request_id}` | `{created, run}`；复盘正文经 SSE 仅状态 + `GET /api/reviews` 查询获得；幂等先于 busy | 400；409 `conversation_busy` |

### 1.2 只读看板（`routes_readonly.py`；handover §1）

| 能力 | 前端最终路径 | 方法 | 请求体/查询参数 | 响应投影要点 | 错误码 |
| --- | --- | --- | --- | --- | --- |
| 正式档案 | `/api/profile` | GET | — | `profile: null`（未建档）或 S2-07 精确形状；**不含** plan/schedules/safety（F1） | — |
| 当前计划 | `/api/plan` | GET | — | `plan: null` 或计划视图：行字段 + `plan`（D9 payload）+ `schedules[]`（`{id,plan_version_id,plan_workout_key,scheduled_on,weekday,cancelled,cancelled_at,locked_at,lock,status}`） | — |
| 计划历史版本 | `/api/plans/{plan_version_id}` | GET | — | 同上；历史不重激活 | 404 `invalid_request` |
| 计划安全复核 | `/api/plan/guidance` | GET | 可选 `?arrangement_revision_id=` | `guidance: null` 或 `guidance.safety`（`usable`/`red_flag_blocked`/`conflicts`/`action_unavailable`/`block_code`/`reasons`/`clarifications`）；`usable=false` 时不给可执行处方 | 404（带 arrangement_revision_id 且不存在） |
| 记录列表 | `/api/records` | GET | — | `{records: [...]}`；每条 `{id, created_at, revision, record(存储契约)}` | — |
| 记录单条 | `/api/records/{session_id}` | GET | — | `{record}` | 404 `invalid_request` |
| 记录三桶判定 | `/api/records/{session_id}/judgement` | GET | — | `judgement: null`（无对照/已作废）或 `{met,unmet,pending}` | 404 `invalid_request` |
| 完成率 | `/api/stats/completion` | GET | 必填 `?plan_version_id=&week_no=` | `completion: null`（「暂无」）或 `{plan_version_id,week_no,week_start,week_end,planned,completed,rate|null}` | 400 `invalid_request`（缺参/类型不符） |
| PR | `/api/stats/pr` | GET | 必填 `?exercise_id=&load_notation=`；可选 `?load_kg_key=` | `pr: {exercise_id,load_notation,load_kg_key|null,max_load_kg_key|null,best_reps|null}`；无候选为 `null` 语义字段 | 400 `invalid_request` |
| 复盘列表 | `/api/reviews` | GET | — | `{reviews: [...]}`；追加语义；最新条即前端「当前复盘」（F8；取代 mock `GET /api/review`）；字段 `body_markdown`/`stale`/`generated_at`/`source_revision_ids`/`basis` | — |
| 复盘单条 | `/api/reviews/{review_id}` | GET | — | `{review}`；`stale` 现算 | 404 `invalid_request` |

### 1.3 草稿（`routes_drafts.py`；handover §1–2）

| 能力 | 前端最终路径 | 方法 | 请求体关键字段 | 响应投影要点 | 错误码 |
| --- | --- | --- | --- | --- | --- |
| 会话草稿列表 | `/api/sessions/{session_id}/drafts` | GET | — | 全部 kind 草稿数组；公共字段 `{id,kind,status,revision,base_business_version,parent_draft_id,payload,diff,parent_diff,committed_revision,committed_business_version}`；`payload` 按 kind | — |
| 草稿读取 | `/api/drafts/{draft_id}` | GET | — | 同上单条；载荷按 kind 映射，不按错形状解码 | 404 `invalid_request` |
| 草稿纠错 | `/api/drafts/{draft_id}/revise` | POST | `{revision, payload}`（revision 必带，F3） | `{draft}`；只改草稿不提交；载荷形状按 kind（plan 带日期字段） | 400 `invalid_request`；404；409 `draft_modified`（所见 revision 不符）；409 `invalid_request`（非 Pending 等） |
| 草稿确认 | `/api/drafts/{draft_id}/confirm` | POST | `{revision}` | 提交凭据（F4/F10）：公共 `{draft_id, status:"committed", committed_revision, committed_business_version}` + kind 附加（计划 `plan_version_id` + `plan_version` / 安排 `arrangement_revision_id`/`arrangement_revision_no`/`scheduled_session_id`/`accepted_at` / 记录 `training_session_id`/`session_revision_id`/`revision_no`/`revision_status`）；幂等重放返回同一份 | 400；404；409 `draft_modified`；409 `draft_stale`（业务基线过期 → 过渡态，触发 recalc，F10）；409 `invalid_request`；422 `invalid_request`（向作废身份追加——`InvalidRecordFact`） |
| 记录作废 | `/api/drafts/{draft_id}/void` | POST | `{revision}` | 提交凭据；追加 `voided` 修订；仅记录草稿；**作废即终态**（已实现，2026-09-13；证据见 `../../pre-prj/stage/evidence/S4-evidence.md` §1／§2 S4-09 行） | 400；404；409 `draft_modified`；409 `invalid_request`（非记录 kind——`DraftKindMismatch`）；422 `invalid_request`（向作废身份追加——`InvalidRecordFact`，已实现） |
| 草稿丢弃 | `/api/drafts/{draft_id}/discard` | POST | `{}`（空对象） | `{draft_id, status}`；只改草稿状态；重复丢弃同结果；安排草稿无丢弃入口（后端按 kind 明确拒绝） | 400；404；409 `invalid_request` |

### 1.4 Provider（`routes_settings.py`；**已实现**，2026-09-13）

| 能力 | 前端最终路径 | 方法 | 请求体关键字段 | 响应投影要点 | 错误码 |
| --- | --- | --- | --- | --- | --- |
| Provider 查询 | `/api/provider` | GET | — | `{provider, has_api_key, protocol, base_url, model:{name, deployment}}`；**无** `data_dir`；完整 Key 与掩码不进入响应/日志/SSE/异常 | — |
| Provider 录入/替换 | `/api/provider/api-key` | PUT | `{"api_key": "..."}`（body 键恰为 `api_key`） | `{provider, has_api_key: true}` 安全投影 | 400 `invalid_request`（形状/空 Key；不回显 Key） |
| Provider 删除 | `/api/provider/api-key` | DELETE | — | `{provider, has_api_key: false}`；幂等（未配置同样返回 false） | — |

> **路径拼写说明**：路径草案沿用前端 mock `/api/provider`；stage4/10.3 只定 `has_api_key` 行为边界，不冻结路径拼写；拼写归 F6-01 工作项 2（Provider 路由）。**2026-09-13 已实现**：`GET /api/provider`＋`PUT/DELETE /api/provider/api-key` 按上表形状返回（后端证据：`tests/test_provider_settings_api.py` **6 passed**（Subtask C 当前字节复跑；初版 4 例）＋回环 curl 冒烟，见 `../../pre-prj/stage/evidence/S4-evidence.md` §1／§2／§3）。无 Key 时 Run 以 `model_request_failed` fail-closed 结束（该行为不依赖 F6-01 路由，属运行时既有边界）。

## 2. F1–F10 逐项结论

| # | 项 | 结论 | 说明 |
| --- | --- | --- | --- |
| F1 | 计划/安全字段在 `/api/profile` | **本阶段改** | 档案页改调 `/api/plan` + `/api/plan/guidance`；`/api/profile` 保持 S2-07 精确形状不扩 |
| F2 | `DraftKind` 缺 `plan`/`arrangement` | **本阶段改** | 前端 kind 对齐后端四类：`profile_update`/`plan`/`training_record`/`arrangement` |
| F3 | revise 缺 `revision` | **本阶段改** | `POST /api/drafts/{id}/revise` 必带 `{revision, payload}`；联调后删 `src/mock/`（已拍 F3） |
| F4 | `ConfirmResult` 用提交凭据 | **本阶段改** | 前端改用 `{draft_id,status,committed_revision,committed_business_version,+kind 附加}`；幂等重放同份 |
| F5 | `PlanVersion`/`PlanScheduleEntry` 展示形状 | **本阶段改** | 前端映射：`plan`（D9 payload）+ `schedules[]`（存储字段名）→ 展示形状 |
| F6 | `TrainingRecord` 扁平展示形状 | **本阶段改** | 前端映射：`record` 存储契约（`exercises[].facts/sets`）→ 展示形状 |
| F7 | 统计聚合 `/api/stats` | **本阶段改** | 前端聚合 `/api/stats/completion` + `/api/stats/pr`；不新增后端聚合端点 |
| F8 | 复盘路径与字段 | **本阶段改** | 改 `/api/reviews` 列表取最新 + `/{id}`；正文字段 `body_markdown`；显式生成走 `POST /api/reviews` |
| F9 | 计划草案 `candidates` 候选 | **再拍/本阶段不做** | 后端无 HTTP 候选端点（生成侧内部）；不扩目录、不发明端点；联调剧本不依赖 candidates 展示 |
| F10 | `DraftStatus` 含 `stale` | **本阶段改** | 后端状态仅 `pending/committed/discarded`；`stale` 是 409 `draft_stale` **过渡态**（触发 recalc），不落为草稿状态 |

> 全部为 handover 已拍方向或 stage6 §8 已拍范围内；无新增「再拍」项（F9 除外，维持 handover「留待后续」）。

## 3. S4-09 硬前置

- **作废终态拦截**：**已实现**（2026-09-13，归后端 owner）。契约：当前修订 `voided` 的身份在确认与作废共用路径上抛 `InvalidRecordFact`（HTTP **422** `invalid_request`）；定向回归 `tests/test_stage3_record_confirm.py` **11 passed**（终态拒绝＋incomplete→valid 正对照），缺陷注射探针证明产出路径被覆盖；证据见 `../../pre-prj/stage/evidence/S4-evidence.md` §1／§2 S4-09 行／§3。
- F6-06 更正／作废剧本依赖该拦截；拦截已实现，但真实后端下的剧本重走仍属 F6-06（未执行前不得称已联调）。
- **S4-09 后端完成判定（2026-09-13，owner 拍 A）**：S4-09 后端任务按自动化／协议级／真实模型最小联调证据判完成（作废终态、Provider 路由、静态托管均已实现并有测试与回环证据；已完成判定的正本见 `../../pre-prj/stage/evidence/S4-evidence.md` §3 新小节）。真实浏览器五页走查、设置页 UI 闭环与 Windows 未执行——豁免为 S4-09 阻塞，仍属本阶段 F6-03–F6-10 的未执行证据缺口，不得当 PASS。

## 4. 明确不为联调新增的端点（已拍，前端不得依赖）

以下 mock/设想路径**没有**对应后端 HTTP 端点，联调正本不包含；前端须去掉生产依赖：

| 不新增端点 | 替代口径（已拍） |
| --- | --- |
| `GET /api/arrangements` | **A2**：前端从计划/记录/草稿已有投影推导已接受安排；投影不足列缺项停下 |
| `POST /api/runs` | `POST /api/sessions/{id}/requests` |
| `GET /api/events` | `GET /api/runs/{run_id}/events`（按 Run 订阅） |
| `GET /api/runs/active` | **E1**：`GET /api/sessions/{id}`（消息 + 全部 Run） |
| `GET /api/sessions/{id}/messages` | 消息内嵌于会话查询 |
| `GET /api/stats`（聚合） | 前端聚合双端点（F7） |
| `GET /api/review`（单文档） | `GET /api/reviews` 列表取最新（F8） |
| 公开建草稿 `POST /api/drafts`、直写正式事实、`/api/chat`、`/api/models` | 不存在；对话是唯一变更入口 |

## 5. F6-01 后端实现现状（2026-09-13 更新）

| 缺口 | 现状 | 归属 |
| --- | --- | --- |
| 静态托管（FastAPI 托管 `frontend` 构建产物 + SPA 回退；运行时无 Node） | **已接线**（Subtask C 已 `npm run build` 重建 dist，并以 f6-02＋窄回环验 `/`／`/profile`／`/records`／`/review`／`/settings` 回退 index.html；真实浏览器走查未做） | F6-01 / 后端 owner |
| Provider HTTP 路由 | **已实现**（`GET /api/provider`＋`PUT/DELETE /api/provider/api-key`，`tests/test_provider_settings_api.py` **6 passed**；Subtask C 窄回环探针验过 GET/PUT/DELETE 与不回显） | F6-01 / 后端 owner（契约形状见 §1.4） |
| 作废终态拦截 | **已实现**（`tests/test_stage3_record_confirm.py` 11 passed） | F6-01 / 后端 owner（契约见 §3） |

> 后端三项已实现并经协议级联调验证（Subtask C：`npm run build`＋f6-02 探针 15 PASS；Subtask D 真实模型最小联调 4 Runs／$0.01963052）；**S4-09 后端任务已按 owner 2026-09-13 拍 A 判完成，F6-01 后端完成**。仍未执行（Stage 6 缺口，不是 F6-01 后端阻塞）：真实浏览器五页走查与浏览器 SSE／断线恢复、真实 Provider 完整业务流式、Windows 验证；F6-03–F6-11 未完成前，关联联调项不得标 PASS。前端 `f6-02-probe` 对 Provider／SPA 的断言已按重跑后的后端事实转 PASS（旧 SKIP 已消失）。

## 6. 冲突记录

- 与后端代码抽查结果（2026-09-13 重做后）：表中路径/方法/字段仍为契约正本（`routes_chat.py` / `routes_readonly.py` / `routes_drafts.py` + stage4 §6 冻结拼写）；**Provider 路由与作废终态拦截已于 2026-09-13 重做并实现**（测试与 curl 冒烟证据见 S4-evidence），此前「已实现」声明曾随当日早先的全量回滚作废，现已恢复为已实现状态。
- 与前端 mock/contract.ts 的差异即 F1–F10 本体，已按 handover 方向收口为「本阶段改」，不在此文件展开代码。
- Provider 端点契约形状：GET `/api/provider` + PUT/DELETE `/api/provider/api-key`（见 §1.4）；**已在当前 backend 实现**，归 F6-01/后端 owner。无 Key 时 fail-closed 行为边界仍按 10.3。

---

**冻结声明**：本对照表为 Stage 6 联调正本。变更须后端更新契约并通知前端 owner，双方确认后同步；不在联调中静默改路径、错误码或业务语义。
