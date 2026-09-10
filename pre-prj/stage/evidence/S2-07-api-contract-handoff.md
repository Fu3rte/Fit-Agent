# Stage 2 S2-07：业务 API 与前端契约交接

> 本文是 S2-07 的交付物（`pre-prj/stage/stage2.md` §5 S2-07：DTO 映射、请求响应样例、前端待改
> 清单）。样例来自本次实跑（临时文件库 + 内部应用层创建草稿 + 进程内 ASGI 调用），不是手写
> 示意。本文不改 `PLAN.md`、设计正本、Stage 1 与 `stage2.md`，也不记录阶段验收（归 S2-08）。

## 0. 已接线端点（沿用现有前端路径）

| 方法 | 路径 | 行为 | 路由 → 应用层入口 |
|---|---|---|---|
| GET | `/api/profile` | 当前正式档案 + `context_version`；未建档 `profile: null`；无写入副作用 | `api/routes_readonly.py` → `ProfileService.read_formal_profile` |
| GET | `/api/sessions/{session_id}/drafts` | 该会话已持久化草稿的当前状态（各带 Diff）；不依赖历史通知 | `api/routes_drafts.py` → `DraftService.list_drafts` |
| GET | `/api/drafts/{draft_id}` | 单草稿内容、revision、状态、Diff 与已提交结果（本阶段补充接口） | → `DraftService.get_draft` |
| POST | `/api/drafts/{draft_id}/revise` | 内容 + 所见 revision；只改 Pending 草稿并递增 revision，不自动提交 | → `DraftService.revise_profile_draft` |
| POST | `/api/drafts/{draft_id}/confirm` | 所见 revision；返回持久化提交结果（重复确认原样返回） | → `ConfirmService.confirm_profile_draft` |
| POST | `/api/drafts/{draft_id}/discard` | 丢弃待确认草稿；不撤销已提交事实；重复丢弃幂等 | → `DraftService.discard_draft` |

装配：`api/app.py` 的 `include_router(readonly_router)` / `include_router(drafts_router)` 与
`api.dto.install_error_handlers(app)`（统一错误形状）。路由只做传输校验、调用应用层、映射结果
与错误；领域规则、revision／基线判定、事务编排与 SQL 全在 `app/`、`domain/` 与各 repo。

**未接线（本阶段不提供）**：创建草稿（只在内部应用层）、`/recalc`、正式档案写入、`/api/runs`、
`/api/sessions`（POST）、聊天／模型／SSE 路由。HTTP 面上不存在这些路径（测试逐条断言 404/405）。

## 1. DTO 形状

### 1.1 档案事实：三态对象（唯一表达，不用空数组／默认值代替未知）

```json
{"state": "unknown" | "denied" | "known", "value": null | 值}
```

- `unknown` = 尚未收集；`denied` = 用户明确无（两者都不带值，`value` 恒为 `null`）。
- `known` 必带值：文本 / 整数 / 数值 / 文本数组 / 限制数组。
- 「显式空集合」`known([])`（如 `body_state: []`、`red_flags: []`）与 `denied` 是两种不同表达，
  与 `unknown` 也互不混同（Stage 2 领域契约：三态不得折叠）。
- 九个字段名与 `domain/profile/schema.py:FACT_FIELDS` 一致：`training_goal`、`training_experience`、
  `weekly_frequency`、`session_duration_minutes`、`available_equipment`、`action_restrictions`、
  `body_state`、`red_flags`、`body_weight_kg`。
- 请求体里这九个字段**必须全部出现**（纠错是整份替换）；缺字段／未知字段／非法 `state`／
  `known` 无值／非 known 带值都是 400。

### 1.2 限制：按稳定身份，不用展示名

`action_restrictions` 的 `known` 值是数组 `[{"scope": "specific_action" | "movement_pattern", "target": "..."}]`；
`target` 是稳定身份（具体动作 = `exercises.id`，如 `barbell-back-squat`；模式 = 13 项已拍词表原词）。
**不得把展示名当身份，也不得由身份反推展示名**；展示名由前端按目录 `CatalogExercise.standard_name_zh`
或模式词表解析。词表外模式、目录外动作由应用层拒绝（422）。

### 1.3 响应体

| 端点 | 形状 |
|---|---|
| `GET /api/profile` | `{"context_version": int, "profile": null \| {九字段三态}}`；`null` = 未建档（与「已建档但全字段未知」不是同一语义） |
| `GET /api/sessions/{id}/drafts` | `DraftDTO[]`（无草稿或会话无草稿 → `[]`，不创建、不报假失败） |
| `GET /api/drafts/{id}` | `DraftDTO` |
| `POST .../revise` | `{"draft": DraftDTO}` |
| `POST .../confirm` | `{"draft_id", "status": "committed", "committed_revision", "committed_business_version"}` |
| `POST .../discard` | `{"draft_id", "status": "discarded"}` |

`DraftDTO`：

```json
{
  "id": "d1",
  "kind": "profile_update",
  "status": "pending" | "committed" | "discarded",
  "revision": 1,
  "base_business_version": 0,
  "payload": {"profile": {九字段三态}},
  "diff": [{"field": "training_goal", "before": 三态对象, "after": 三态对象, "changed": true}],
  "committed_revision": null,
  "committed_business_version": null
}
```

- `payload.profile` 是**数据库保存的最终草稿内容**（服务端权威；确认不对它做客户端替换）。
- `diff` 由后端按「生成基线快照 → 拟议快照」计算，顺序固定为九字段；不接受客户端 before／after。
- `committed_revision` / `committed_business_version` 只在已提交草稿上有值，就是持久化提交凭据；
  后续业务版本变化不改写它（重复确认返回同一份）。

## 2. 真实样例（本次实跑，已省略重复的九字段列表）

`GET /api/profile`（未建档）：

```json
{"context_version": 0, "profile": null}
```

`GET /api/drafts/d1`（Pending：内容、状态、revision、Diff；`unknown` 基线逐字段表达）：

```json
{
  "id": "d1", "kind": "profile_update", "status": "pending", "revision": 1,
  "base_business_version": 0,
  "payload": {"profile": {
    "training_goal": {"state": "known", "value": "增肌"},
    "action_restrictions": {"state": "denied", "value": null},
    "body_state": {"state": "known", "value": []},
    "red_flags": {"state": "denied", "value": null}}},
  "diff": [
    {"field": "training_goal",
     "before": {"state": "unknown", "value": null},
     "after": {"state": "known", "value": "增肌"},
     "changed": true}
  ],
  "committed_revision": null, "committed_business_version": null
}
```

`POST /api/drafts/d1/revise`（请求 → 响应；只改草稿、不提交）：

```json
请求: {"revision": 1,
       "payload": {"profile": {"training_goal": {"state": "known", "value": "力量"}, "...": "九字段全量"}}}
响应: 200 {"draft": {"id": "d1", "status": "pending", "revision": 2,
                     "committed_revision": null,
                     "payload": {"profile": {"training_goal": {"state": "known", "value": "力量"}, "...": "..."}},
                     "diff": [{"field": "training_goal", "before": {"state": "unknown", "value": null},
                               "after": {"state": "known", "value": "力量"}, "changed": true}]}}
```

`POST /api/drafts/d1/confirm`（提交结果；重复确认同一结果）：

```json
请求: {"revision": 2}
响应: 200 {"draft_id": "d1", "status": "committed",
           "committed_revision": 2, "committed_business_version": 1}
```

`GET /api/profile`（提交后：正式档案 + 版本；`body_state: []` 与 `denied` 各自保留）：

```json
{"context_version": 1, "profile": {"training_goal": {"state": "known", "value": "力量"},
  "body_state": {"state": "known", "value": []},
  "red_flags": {"state": "denied", "value": null}, "...": "九字段"}}
```

409（业务基线过期，附可核实字段变化；另一份同版本草稿先提交后）：

```json
{"http_status": 409, "error_code": "draft_stale",
 "message": "草稿业务基线 1 与当前 context_version 2 不符：d3；可核实字段变化：body_weight_kg",
 "detail": "可核实字段变化：body_weight_kg"}
```

409（所见 revision 不符）与 404／409／400 其余样例：

```json
{"http_status": 409, "error_code": "draft_modified",
 "message": "所见 revision 9 与草稿当前 revision 1 不符：d4"}

{"http_status": 404, "error_code": "invalid_request", "message": "草稿不存在：d-missing"}
{"http_status": 409, "error_code": "invalid_request", "message": "草稿已 discarded，不可确认：d4"}
{"http_status": 409, "error_code": "invalid_request", "message": "最终草稿与正式档案相同，无业务变更，不提交：d2"}
{"http_status": 400, "error_code": "invalid_request", "message": "revision 必须是 >= 1 的整数：'x'"}
{"http_status": 422, "error_code": "invalid_request", "message": "首次建档缺少明确回答的事实：['body_state', ...]"}
```

非回环 Host（既有 10.1 边界，形状由既有中间件决定，不是业务 `ApiError`）：

```json
{"detail": "forbidden"}   // HTTP 403
```

## 3. 错误映射（统一 `ApiError` 形状）

响应体恒为 `{"http_status", "error_code", "message", "detail"?}`；`error_code` 只用前端契约已登记的
值（`invalid_request`／`draft_stale`／`draft_modified`），不新增业务语义；`message` 只取业务异常
文本，不含路径、SQL 或堆栈。

| 情况 | HTTP | error_code | 判定位置 |
|---|---|---|---|
| 非法 JSON／非对象／未知字段／缺字段／类型不符（含 `revision` 为 0、布尔、字符串、小数；含 `NaN`／`Infinity`／`-Infinity` 这类非标准 JSON 常量） | 400 | `invalid_request` | 路由（传输形状，触碰应用层之前） |
| 数值字段的非有限值（合法 JSON 但溢出，如 `1e999` → `inf`） | 422 | `invalid_request` | 应用层领域校验（`validate_profile_structure` 要求有限浮点值） |
| 数值字段的不可表示整数（如 400 位整数：合法 JSON，但超出 float 表示范围） | 422 | `invalid_request` | 应用层领域校验（number 事实读写两侧都以 float 存取） |
| 草稿身份不存在 | 404 | `invalid_request` | 应用层 `UnknownDraft` |
| 终态／非法状态（已提交不可纠错、不可丢弃；已丢弃不可确认／纠错） | 409 | `invalid_request` | 应用层 |
| 最终草稿与正式档案完全相同（无业务变更） | 409 | `invalid_request` | 应用层 `NoBusinessChange` |
| 所见 revision 不符 | 409 | `draft_modified` | 应用层 `DraftRevisionConflict` |
| 业务基线过期（`detail` 列出可核实字段变化；无可核实差异时为「业务版本已变化，当前快照无字段差异」） | 409 | `draft_stale` | 应用层 `DraftStale` |
| 结构／限制引用／首次建档完整性（形状合法但内容不合领域规则） | 422 | `invalid_request` | 应用层领域校验 |
| 未登记的异常（如库内草稿行损坏） | 500 | —（保持框架默认，不伪装成客户端错误） | 不映射 |

判定顺序：**传输形状先于身份查找**（`POST /api/drafts/d-missing/revise` 带非法请求体 → 400，
不是 404）；确认路径内部按 S2-05／S2-06 顺序（幂等已提交 → 拒绝已丢弃 → 业务基线 → revision →
领域复查）。

非有限数值（JSON 无法可靠表达：SQLite `json_valid` 拒绝 `Infinity`／`NaN`，响应编码也拒绝非
有限浮点）必须在触碰草稿前拒绝：`NaN`／`Infinity`／`-Infinity` 不是合法 JSON，按 400 拒；
指数溢出（`1e999`）是合法 JSON 数字，按 422 拒。超大整数同理：合法 JSON 整数，但 number 事实
在读写两侧都以 float 表示，超出 float 表示范围的整数会落盘成功却读不回来（解码抛
`OverflowError`），因此按 422 拒；仍在 float 表示范围内的整数照常接受（业务值域阈值未拍，
不新增）。这些情况都不产生任何部分更新。

## 4. 与前端契约（`frontend/src/lib/contract.ts`、`src/lib/api.ts`）的差异

前端契约未改动（本任务不切换前端／mock 轨道）。下表是**前端待改清单**，改动只落在少数渲染分支
与 API 封装（stage0 F0-01 契约先行的既定收敛点）。

| 项 | 现状 | 需要改成 |
|---|---|---|
| 档案事实形状 | `Profile` 字段全部非可选（`goal: string`、`weekly_frequency: number`、`equipment: string[]`），无法表达「未收集」 | 事实改为三态：`{state, value}`；`unknown` 不渲染成 `0`／`[]`；`denied` 与显式空集合分别有文案（如「明确无」/「无」），不显示成完整档案 |
| `GET /api/profile` | `ProfileResponse {profile: Profile \| null, restrictions, context_version, plan?, schedules?, plan_safety?}` | 采用 `{context_version, profile: null \| 九字段三态}`；`profile === null` = 未建档（建档引导入口）；`restrictions` 改为读 `profile.action_restrictions`（不再单独下发数组，避免第二份可失步形状） |
| `ProfileDraftPayload` | `{profile: Partial<Profile>, restrictions?: Restriction[]}` | `{profile: 九字段全量三态}`；限制并入 `action_restrictions` 事实；请求体缺字段／多字段一律 400 |
| `Restriction` | `{name, scope, note?}` | 传输层用 `{scope, target}`（稳定身份）；`name` 由前端按 `target` 查目录／模式词表，`note` 本阶段无承载（S2-01 §6 已记） |
| `FieldDiff` | `{field, old_value?, new_value}`（文本值） | `{field, before, after, changed}`，`before`／`after` 是三态对象（否则 unknown→denied 会被压成空串） |
| `Draft` | `{id, kind, status, revision, base_business_version, parent_draft_id?, payload, diff}` | 增加 `committed_revision`／`committed_business_version`（已提交结果）；本阶段不发 `parent_draft_id`（Stage 4 重算才有关联）；`status` 不出现 `stale`（过期只以 409 `draft_stale` 表达，不是持久化状态） |
| `ReviseRequest` | `{payload}` | `{revision, payload}`（新增所见 revision；`reviseDraft(draftId, revision, payload)`） |
| `ConfirmRequest` | `{revision}` | 不变（已一致） |
| `ConfirmResult` | `{draft_id, status, newly_committed, context_version, summary}` | 后端不提供 `newly_committed`／`summary`（S2-05 幂等契约：重复确认必须返回同一份结果，无法区分「本次新提交」；版本用 `committed_business_version`）；前端文案自行组合 |
| `DiscardResult` | `{draft_id, status}` | 不变（已一致） |
| `/api/drafts/:id/recalc`、`recalcDraft` | 契约中存在 | 本阶段不提供（404）；重算归 Stage 4——不要接假成功 |
| 纠错／确认按钮（S2-04 已拍 3） | — | 无本地未保存修改时禁用「提交纠错」、允许「确认草稿」；有未保存修改时允许「提交纠错」、禁用「确认草稿」；纠错成功后以服务端新 revision／内容更新基准；失败不得标成已保存；两者还须服从 Pending、输入有效、请求未在进行；不用本地「有无修改」放行终态或已知过期草稿 |
| 建档安全询问文案（§8 已拍 方案 A） | 术语「红旗」 | 面向用户改具体症状询问（如「最近训练时有没有胸部不适、晕厥、异常气短等情况？」）；文案落地属前端，真实对话询问归 Stage 4 |
| 会话草稿查询 | `getSessionDrafts(sessionId)` | 不变；恢复时用它读当前状态（不依赖历史通知） |

## 5. 本任务边界与验证

- 未切换前端／mock 轨道，未改 `frontend/**`；未新增路由、依赖、业务模块、通用引擎或第二份版本／锁。
- 自动化：`backend/tests/test_stage2_business_api.py`（25 用例，进程内 ASGI 驱动同一装配与中间件栈；
  草稿由内部应用层在临时库创建）。`backend/tests/test_stage1_profile_write.py` 的接线守卫按
  「显式扩展、不删测试、不放宽」口径增加 api/ 白名单（`dto.py`、`routes_readonly.py`），runtime/ 仍全禁。
- 实跑（Linux WSL，非 Windows）：

```bash
cd backend && .venv/bin/python -m pytest tests/test_stage2_business_api.py -q          # 25 passed
cd backend && .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
# 394 passed, exit 0
```

- Windows 全量自动化仍是阶段门槛（stage2.md §6），归 S2-08；本文不声称阶段通过。
