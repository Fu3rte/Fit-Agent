# F5-04 Worker Report：复盘后后续安排／新计划草稿

> 任务：F5-04（plans/stage5.md §3.3 / §5 F5-04 / §7 第 3、4、8 步）  
> 状态：**协议级完成（2026-09-13）**——tsc 零错误；`f5-04` 19 PASS；回归 `f5-02` 31 / `f5-03` 22 / `f4-05` 31 / `f3-02` 全绿。  
> 边界：仅 mock；不碰 F5-07；不 commit；不改 backend/pre-prj；无新端点、无第二套事务。

## 1. 改动

| 文件 | 变更 |
| --- | --- |
| `src/mock/server.ts` | `runScript` 意图链增加 F5-04 分支：`isSuggestLong` / `isSuggestAdjust`；复用既有 `planScriptReply` / `arrangementScriptReply`，无新草稿路径 |
| `scripts/f5-04-probe.mjs` | 新增验收探针（19 项） |

### 1.1 意图路由（最简正则，不依赖会话状态）

在 `isReviewGenerate` 之后、既有 `isArrangement` / `isPlan` 处增加：

- **`isSuggestLong`**（profile 已有）：命中 `换计划|改长期|计划调整|调整计划|调计划|按(?:复盘)?建议.{0,8}(?:计划|长期|调整)` → `planScriptReply`（Stage 2 替换草稿：只追加新版本 + 旧版未来未锁定日程取消清单；确认走既有事务）。
- **`isSuggestAdjust`**（profile 已有）：命中 `按(?:复盘)?建议` 且未落入长期分支 → 与 `isArrangement` 合并进 `arrangementScriptReply`（Stage 3：MILD→安排草稿；无明确确认意图→澄清不落草稿）。

不变量（复用既有实现，本次未重写）：

- `REVIEW_GENERATE` 仍优先；`reviewGenerateReply` 不产出 draft；复盘正文建议保持纯文本「不会自动创建草稿」。
- 确认前正式计划/日程/安排不变；`/api/dev/confirm/fail-next` 整份回滚；重复确认 `newly_committed=false`。
- 复盘条目 `text`/`generated_at` 不因建议或计划确认改写（业务变更仅翻 `stale`）。

## 2. 验证（真跑）

工作目录：`D:/Repository/Fit_Agent/frontend`

| 命令 | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误（无输出） |
| `node scripts/f5-04-probe.mjs` | **19 passed, 0 failed** |
| `node scripts/f5-02-probe.mjs` | **31 passed, 0 failed** |
| `node scripts/f5-03-probe.mjs` | **22 passed, 0 failed** |
| `node scripts/f4-05-probe.mjs` | **31 passed, 0 failed** |
| `node scripts/f3-02-probe.mjs`（安排链路回归，因合并 `isArrangement \|\| isSuggestAdjust`） | 全部通过（含 deload/keep/越界/幂等/失败注入） |

### 2.1 f5-04 覆盖要点（摘录）

- 生成复盘 → draft 数 0；cv 不变；条数仅 +1。
- 「按复盘建议调整计划」→ plan 草稿 v3、cancellations=12；确认前 plan/schedules/cv 隔离；fail-next → 500 回滚（cv=3、plan=v2、草稿 pending）；重做确认 cv=4、v3 启用、v2 未来未锁定 12 条 cancelled、已锁定 6 条不动；重复确认 `newly_committed=false` 且 cv 不再 +1；复盘 text/generated_at 逐字不变（stale=true）。
- 「换计划」→ plan 草稿。
- 「今天按建议轻一点」→ arrangement 草稿（不改计划版本/cv）。
- 「你觉得呢」→ 不落草稿；裸「按建议」→ 澄清文案、不落草稿。

## 3. 验收对照（stage5 F5-04）

| 标准 | 结论 |
| --- | --- |
| 1 明确调整意图 → 复用安排/计划草稿 | ✅ f5-04 §3/4/5 |
| 2 复盘正文建议不自动建草稿 | ✅ f5-04 §1/2；生成复盘 draft=0 |
| 3 无确认意图不落草稿；模糊只解释 | ✅ f5-04 §6/7 |
| 4 确认前正式数据不变；既有事务；失败回滚；幂等 | ✅ f5-04 §3 |
| 5 新计划同事务取消旧版未来未锁定日程 | ✅ 复用 `planScriptReply`/`commitPlanDraft`；f5-04 §3 cancels=12 |
| 6 复盘条目不因建议改写 | ✅ f5-04 §3 text/generated_at 不变；stale 可变 |

## 4. 剩余缺口 / 未做

- Owner 浏览器走查（§7 第 3、4、8 步）未做——协议级交付，交 F5-07 闭环演示归档。
- 未改 `plans/stage5.md` 勾选状态（文档同步按项目规则另派 worker/reviewer）。
- 未 commit。
