# Stage 6 F6-01 / F6-02 Workflow（编排正本）

> 状态：**F6-00/F6-02 前端侧完成；F6-01 backend 已回滚、归后端 owner**（2026-09-14）。此前「F6-00–02 已完成（主控终审通过，2026-09-13）」中 F6-01 部分作废，不得再当完成证据。
> 范围：仅 F6-00（前置契约冻结）+ F6-01（后端联调底座）+ F6-02（前端真实适配）。**不含** F6-03+（真实模型、业务闭环复验、Windows 验收、结项）。
> 执行纪律：每子任务 1 worker + 1 reviewer；实现层细节 worker 自定；业务语义冲突或需新端点即停等拍；证据只记实际跑过的命令与退出码。

## 已拍边界（直接沿用，不重议）

| 项 | 结论 | 正本 |
|---|---|---|
| 作废即终态 | A：当前修订 `voided` 后身份终态，确认路径 fail-closed，复用 `invalid_request` | design-decisions；05 §5.3；stage4 S4-09 |
| 安排读回 | A2：前端自行投影，不增 `GET /api/arrangements` | stage6 §8 |
| 断线恢复 | E1：`GET /api/sessions/{id}`，不建 `/api/runs/active` | stage6 §8；08 8.7 |
| mock 去留 | F3：F6-02 联调验证后删 `src/mock/` 与插件挂载 | stage6 §8 |
| Provider 路由 | 只回 `has_api_key`；PUT/DELETE 管理 Key；完整 Key 不进响应/日志/SSE | 10.3；stage6 F6-01 |
| 静态托管 | FastAPI 托管前端 `dist` + SPA 回退；运行时无 Node | 10.1；stage6 F6-01 |
| 不做 | 不建浏览器 E2E；不增公开建草稿；不发明新业务语义/错误码；不改 `pre-prj/` 正本 | stage6 §4 |

## 子任务图

```text
F6-00 契约冻结（文档）
        │
        ├──────────────┬──────────────────┐
        ▼              ▼                  ▼
   F6-01a 作废终态  F6-01b Provider 路由  （F6-01b 完成后）
   （confirm.py）   （routes_settings）   F6-01c 静态托管
        │              │                  （app.py 托管）
        └──────────────┴──────────────────┘
                       │
                       ▼
              F6-01 全量后端门（并入 01c reviewer）
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   F6-02a 核心传输   F6-02b 看板查询  （02a 完成后）
   contract/api      stats/review/   F6-02c 草稿与安排
   sessions/runs/SSE plan/guidance   kinds/confirm/A2
        │              │              │
        └──────────────┴──────────────┘
                       ▼
              F6-02d mock 删除 + 探针 + 构建门
                       ▼
              主控最终审查（对照 stage6 F6-01/02 验收）
```

## 子任务定义

### F6-00 契约冻结与 F1–F10 收口

- **Worker**：写 `frontend/plans/stage6-transport-freeze.md`：§3.1 传输对照表按真实后端拼写冻结（路径/方法/请求体/响应投影/错误码）；handover F1–F10 逐项「本阶段改 / 已拍沿用 / 再拍」结论；标明 S4-09 硬前置（作废终态）。
- **Reviewer**：对照 stage6.md、stage3-handover、`routes_chat.py`/`routes_readonly.py`/`routes_drafts.py` 实际拼写；无未决项、无发明端点。
- **产物**：freeze 文档；无代码。

### F6-01a 作废终态拦截 + 定向回归

- **Worker**：`backend/app/confirm.py` 记录确认路径：身份当前修订为 `voided` 时拒绝后续更正/复活/补全（含 arrangement 不适用）；错误 `invalid_request`；补定向回归测试；跑相关 pytest。
- **Reviewer**：对照 05 §5.3 与 S4-09 验收；确认不删历史修订、不跳过草稿确认、旧用例不被放松。
- **边界**：只改确认拦截与测试；不改业务表语义。

### F6-01b Provider 设置 HTTP 路由

- **Worker**：实现 `routes_settings.py` 并在 `app.py` 接线：
  - `GET /api/provider` → 只投影 `has_api_key`（+ 可选 `provider` 标识；**不含** Key/掩码）
  - `PUT /api/provider/api-key` body `{api_key}` → `{has_api_key: true}`
  - `DELETE /api/provider/api-key` → `{has_api_key: false}`
  - 复用 `SettingRepo`；Key 边界同 10.3；补 HTTP 层测试。
- **Reviewer**：对照 10.3 与 stage6 F6-01；断言响应无 Key、无掩码、无 base_url 可编辑字段（协议/Base URL 属配置展示，不在本路由存储面）。
- **边界**：不换 Provider/模型；不实现 F6-03 兼容端点联调。

### F6-01c 静态托管 + SPA 回退 + 后端全量门

- **Worker**：FastAPI 托管 `frontend/dist`（存在时）；未知非 `/api/*` 路径回退 `index.html`；`/api/*` 与 `/healthz` 不抢静态；记录 dist 缺失时行为；跑 backend 全量 pytest + ruff/pyright（项目既有门）。
- **Reviewer**：对照 10.1；确认 API 路由不被静态捕获；全量门退出码与 skip 列表如实；无「新增 skip 掩盖缺口」。
- **依赖**：01b 完成（同改 `app.py` 装配，串行）。

### F6-02a 前端核心传输适配

- **Worker**：
  - `contract.ts`/`api.ts`：对齐真实面——`POST /api/sessions/{id}/requests`、`GET /api/sessions/{id}`（消息+Runs）、`GET /api/runs/{run_id}/events` SSE、cancel；去掉生产路径 `POST /api/runs`、`GET /api/events`、`GET /api/runs/active`、`GET /api/sessions/{id}/messages`
  - Chat/恢复逻辑改会话查询（E1）
  - draft kind 对齐 `profile_update|plan|training_record|arrangement`（F2）；revise 带 revision（F3）；ConfirmResult 用提交凭据（F4/F10）
- **Reviewer**：对照 stage4 §6 拼写与 freeze 文档；`tsc -b`；无 mock 私有端点残留于生产路径。

### F6-02b 看板查询适配

- **Worker**：
  - 统计：`/api/stats/completion` + `/api/stats/pr` 前端聚合（F7）
  - 复盘：`GET /api/reviews` 取最新，字段 `body_markdown`（F8）
  - 档案页：`/api/plan` + `/api/plan/guidance`（F1）
  - 计划/记录 DTO 映射（F5/F6）
- **Reviewer**：对照 handover §1–3 与 freeze；页面编译通过；空数据/null 语义不编造。

### F6-02c 安排投影与草稿流收口

- **Worker**：
  - **A2**：去掉生产路径 `GET /api/arrangements`；从计划/记录/草稿推导已接受安排
  - recalc 保持 `{client_request_id}`
  - 修正受形状影响的组件与 DraftCard 流
- **Reviewer**：对照 stage6 A2；投影不足须列缺项，不得静默改业务语义。

### F6-02d mock 删除、探针与构建门

- **Worker**：
  - F3：删除 `src/mock/` 与 Vite mock 插件挂载
  - 新增/修正 `f6-02` 协议探针（仅回环、无真实模型）：查询类端点可读、确认/丢弃/修订形状
  - 旧 f3–f5 探针：迁真实或明确退役（不放松断言）
  - `tsc -b` + `npm run build` 零错误
- **Reviewer**：对照 F6-02 验收标准全文；确认无 `/api/stats` 聚合、`/api/review`、`POST /api/runs`、`GET /api/events` 生产残留；探针输出归档。
- **依赖**：02a–02c 完成；F6-01 完成（探针对真实后端）。

### 主控最终审查（编排者）

- 对照 `stage6.md` F6-01 与 F6-02 **逐条**验收标准。
- 核对证据：命令、退出码、未覆盖项；不把静态检查冒充执行证据。
- 列出仍未做的 F6-03+ 与任何残留缺口；**不**写「已交付」。

## 验证证据归档

- 后端：命令输出摘要写入 `pre-prj/stage/evidence/` 或本 workflow 附录（worker 报告为准，主控汇总）。
- 前端：探针输出 → `frontend/plans/stage6-evidence-assets/`；本 workflow 附录摘要。
- 全程：无 API Key 明文；不发起真实模型计费请求。

## 风险与停机条件

| 条件 | 动作 |
|---|---|
| 需要新增业务端点/错误码 | 停，列选项等拍 |
| 与已拍语义冲突 | 停，列冲突等拍 |
| F6-01 未完成即要求 F6-02d 对真实后端探针 | 可先做 mock 删除与构建；探针可等 01 |
| 模型超时/环境故障 | 记一次即交付，不空转重试 |
| Provider 路由若必须回 base_url/model 才能过设置页 | 按「只回 has_api_key」实现，前端 F6-02 改展示；若 UI 必须展示协议/Base URL，从后端**只读配置**投影且**不可编辑、不含 Key**——属实现细节，不改存储面 |
