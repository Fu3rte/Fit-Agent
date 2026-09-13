# F5-03 Worker Report

- **任务**：`/review` 展示、stale 徽章与重新生成入口（plans/stage5.md §5 F5-03 / §3.4 / §7 第 5、8 步）
- **日期**：2026-09-13
- **状态**：协议级完成（代码 + 探针 + 回归全绿）

## 1. 缺口分析（开工前）

对照验收标准与现有 `ReviewPage.tsx`：

| 项 | 开工前 | 结论 |
| --- | --- | --- |
| 只渲染最新一条（无历史列表 UI） | 已满足（`getReview` 返回最新条投影，无 map 历史） | 无需改 |
| 正文 + Markdown 白名单 | 已满足 | 无需改 |
| stale 徽章「依据已变更 · 可重新生成」 | 已满足 | 无需改 |
| 页内无 input/select/textarea；主文案无 RIR | 已满足 | 无需改 |
| 预填文案「请基于最新数据生成训练复盘。」 | 已命中 `REVIEW_GENERATE`（`生成训练复盘` → `生成.{0,6}复盘`） | 无需改文案 |
| 未新增路由 | 已满足 | 无需改 |
| **basis 快照摘要** | **缺失**：正文下无冻结事实摘要展示 | **本任务补齐** |

## 2. 实现内容

### 2.1 `src/features/review/ReviewPage.tsx`（唯一切片改动）

1. 新增只读组件 `BasisSummary`（不新增路由/端点）：
   - 展示 `review.data.basis` 的冻结快照：`per_week` 完成率摘要行、三桶组数、`prs` 摘要、`basis.data_updated_at`。
   - 文案：「依据快照（生成时冻结）」「依据数据时间：…」。
   - 空态：`review.data.basis` 缺省时显示「暂无依据快照（空数据种子或尚未生成正式复盘）；页面不编造完成率与 PR。」——与 mock 空态投影一致，不编造。
2. 正文 Markdown 白名单、stale 徽章、预填按钮、页脚现算 `data_updated_at` **全部保持原样**，未改坏。

### 2.2 `scripts/f5-03-probe.mjs`（新建探针）

源码断言 + 运行时真跑，共 22 项：

- 源码（ReviewPage）：stale 徽章文案、生成按钮 + prefill、预填文案命中 `REVIEW_GENERATE`（从 server 源码还原正则后断言）、无 input/select/textarea、无历史列表 UI、无 RIR、Markdown 白名单、`BasisSummary`/依据快照/basis 消费、空态不编造文案。
- 源码（App）：`/review` 恰一条、无详情子路由。
- 运行时：
  - `GET /api/review` 最新条含 basis（per_week/三桶/PR/data_updated_at），与 `/api/stats` 对齐；
  - empty 种子 basis 缺省（空态不编造）；
  - 显式生成（stale=false）→ 更正确认 → stale=true 且 text/generated_at 逐字不变；
  - 预填文案经 `POST /api/runs` 发送 → 命中 F5-02 生成路径（回复含「已保存复盘」、条数+1、stale=false、basis 冻结数字）。

## 3. 验证证据（真跑）

| 命令 | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误（无输出） |
| `node scripts/f5-03-probe.mjs` | **22 passed, 0 failed** |
| `node scripts/f5-02-probe.mjs`（回归） | **31 passed, 0 failed** |
| `node scripts/f4-05-probe.mjs`（回归） | **31 passed, 0 failed** |

f5-03 探针关键 PASS 明细（摘录）：

- 预填文案命中 REVIEW_GENERATE — `{"prefill":"请基于最新数据生成训练复盘。"}`
- basis 与 /api/stats 对齐（default 种子冻结）
- empty 种子：basis 缺省（空态不编造）— `{"hasBasis":false}`
- 更正后 stale=true 且 text/generated_at 逐字不变 — `{"stale":true,"textSame":true,"genSame":true,"n":2}`
- 预填发送后命中生成路径：回复含「已保存复盘」；生成成功：条数+1、generated_at 更新、stale=false

## 4. 验收标准对照

| # | 标准 | 结论 |
| --- | --- | --- |
| 1 | /review 只渲染最新一条（无历史列表 UI） | ✅ |
| 2 | 正文 + 快照摘要 + data_updated_at | ✅（BasisSummary 补齐快照摘要） |
| 3 | stale 徽章文案正确 | ✅（未改） |
| 4 | 「在对话中生成复盘」预填命中 REVIEW_GENERATE | ✅（文案未改，探针断言） |
| 5 | 页内无 input/select/textarea；主文案无 RIR | ✅ |
| 6 | Markdown 白名单沿用 | ✅ |
| 7 | 更正后 stale=true 且旧正文/generated_at 逐字不变 | ✅（f5-03 运行时断言 + f4-05 回归） |
| 8 | 不得新增第六页路由 | ✅ |

## 5. 改动文件

- `src/features/review/ReviewPage.tsx`（补 BasisSummary + 空态）
- `scripts/f5-03-probe.mjs`（新建）
- `plans/f5-03-worker-report.md`（本报告）

未动：`src/app/App.tsx`、契约、mock、backend、pre-prj。未 commit。

## 6. 剩余缺口 / 风险

- owner 浏览器走查（§7 第 5、8 步）未做（本 worker 仅协议级）。
- F5-04／F5-07 未开工，归后续任务。
- 无待拍项。
