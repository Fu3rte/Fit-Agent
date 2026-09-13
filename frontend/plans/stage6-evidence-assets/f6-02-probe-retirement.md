# F6-02d 旧探针退役清单

**日期**：2026-09-13  
**背景**：`frontend/src/mock/` 与 Vite `mockPlugin` 已随 F6-02d 删除。f2–f5 探针依赖 mock 服务器（`createViteServer` + `vite.config.ts` 挂载 mockPlugin，或直接读 `src/mock/*.ts`），删除后无法再对 mock REST 跑通。**不放松断言冒充通过**：处置一律为「退役」或「待迁真实」；优先保证 `f6-02-probe.mjs` 可跑。

## 处置总则

| 处置 | 含义 |
|------|------|
| 退役 | 仅验证 mock 行为；mock 已删，断言不再适用生产路径。脚本保留在 `scripts/` 作历史证据，运行会因 mock 缺失失败——**不得**改弱断言后标通过。 |
| 迁真实 | 有对应真实后端端点可覆盖同等契约；应重写为对真实后端的探针（参考 `f6-02-probe.mjs`）。本轮未逐个重写，列入后续。 |

## 清单（grep：`src/mock` / `createViteServer` / `mockPlugin` / 直接 import mock）

| 脚本 | 依赖 | 处置 | 说明 |
|------|------|------|------|
| `f2-01-catalog-probe.mjs` | import `../src/mock/catalog.ts`、`server.ts`、`plan.ts` | 退役 | mock 目录与脚本对照；目录契约应走真实后端只读 API |
| `f2-02-plan-probe.mjs` | import mock catalog/plan + server 源码 | 退役 | mock 计划生成剧本 |
| `f2-03-draft-revise-probe.mjs` | import mock catalog/plan + server | 退役 | mock 纠错链路 |
| `f2-04-plan-commit-probe.mjs` | createViteServer + mockPlugin | 退役 | mock 计划确认 |
| `f2-05-plan-safety-probe.mjs` | createViteServer + mockPlugin | 退役 | mock 安全阻断 |
| `f3-01` … `f3-06-closed-loop-probe.mjs` | createViteServer + mockPlugin；部分读 `src/mock/server.ts` | 退役 | F3 全套走 mock REST；真实会话/Run/Draft 应迁真实后端（已有 f6-02 覆盖协议骨架） |
| `f4-01` … `f4-07-evidence-probe.mjs` | createViteServer + mockPlugin；部分读 server.ts | 退役 | F4 mock 确认/作废/渐进剧本 |
| `f5-01` … `f5-06-probe.mjs` | createViteServer + mockPlugin；读 server.ts / plan.ts | 退役 | F5 mock 复盘/建议剧本 |
| **`f6-02-probe.mjs`**（本阶段新增） | 真实 uvicorn 后端 + 临时数据目录 | **现行** | 协议联通：healthz/session/只读看板/无 Key 失败；Provider 路由与静态托管归 F6-01/后端 owner（404/非 SPA → SKIP）；见 `f6-02-probe.txt` |

## 已知限制（安排徽章 / 三桶）

- **安排徽章假阴性**：真实后端无跨会话「已接受安排」枚举端点；ProfilePage 的 `arranged` 恒为空数组 → 徽章默认「尚无安排」。**不静默改语义**（不伪造 accepted）。详见 `src/features/profile/ProfilePage.tsx` 注释与 02d 报告。
- **三桶 / PR / completion**：空库下 `f6-02-probe` 只验证协议形状（必填查询参数 + null 候选），不验证业务数值——业务数值验收属后端 stage 测试与后续迁真实探针。

## 证据有效性（2026-09-14 backend 回滚）

- 旧 `f6-02-probe.txt`（2026-09-13，15/15 PASS 含 `GET /api/provider` 与 `GET /` 静态托管）基于**已回滚的后端临时实现**，**不得再当 F6-01 完成证据**。
- 重跑后探针对 Provider 404 / 非 SPA 只记 SKIP；剩余断言仅覆盖不依赖 F6-01 的契约面（healthz、sessions、只读查询、无 Key `model_request_failed`）。

## 后续（不在本轮）

- 选定关键 F3/F4 契约（会话查询恢复、草稿确认、void）迁真实探针；在迁移完成前 f2–f5 脚本保持退役状态，不得当 CI 门禁。
