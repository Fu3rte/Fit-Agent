# Stage 5 验收证据（F5-01–F5-07：复盘闭环 + 接回/渐进）

> 契约正本指针：`plans/stage5.md`（范围/任务/§7 剧本）；实现细节以 `src/lib/contract.ts` / `src/mock/server.ts` 为准。
> **证据分层**：协议级可复现（vite 随机端口 + `/api/*`）/ UI=owner 浏览器 §7 全剧本走查（2026-09-13，口述确认，无截图）/ **mock≠真实链路** / 本阶段只改 `frontend/`，未改 `backend/`、`pre-prj/`。
> **基线**：HEAD `24d9751`（Stage4+5 一并入库）；Windows；Node v22；证据日期 2026-09-13。

## 1. 结果正本

| 门 | 命令/结论 | 实际 |
|---|---|---|
| 类型 | `node node_modules/typescript/bin/tsc -b --pretty false`（`npx tsc` 会命中占位包，勿用） | **EXIT=0** |
| 构建 | `npm run build` | **EXIT=0**，`✓ built in 5.24s`（仅既有 chunk 警告） |
| F5 探针合计 | f5-01…06 | **PASS=144 / FAIL=0**（27+31+22+19+29+16） |
| 回归 | f4-01 + f4-05 + f3-02 + f3-01 | **PASS=129 / FAIL=0**（30+31+32+36） |
| Owner | 浏览器 §7 全剧本 1–10 步 | **走查通过 + 整阶段结项（2026-09-13）** |

## 2. 覆盖对照（探针 → 验收单元）

| 探针 | 验证了什么 | PASS | 归档 |
|---|---|---|---|
| f5-01 | 契约 `ReviewBasis`/`ReviewEntry`；append-only；冻结快照+source_revision_ids；save fail-next 整份不落；stale 翻最新条；empty 无 basis 不编造；无新业务端点 | 27 | `stage5-evidence-assets/f5-01-probe-out.txt` |
| f5-02 | 显式生成/重生成 append；正文数字=basis；模糊/「看复盘」不落库；empty 拒绝；对话 fail-next 不落可重做；cv/plan/草稿不变 | 31 | `f5-02-probe-out.txt` |
| f5-03 | `/review` 最新条+BasisSummary；stale 徽章；预填「生成训练复盘」命中生成；只读无 RIR/无历史/无新路由 | 22 | `f5-03-probe-out.txt` |
| f5-04 | 复盘建议不自动建草稿；`isSuggestLong`→plan / `isSuggestAdjust`→arrangement；确认/幂等/fail 回滚；旧版 future 未锁定取消；复盘 text 不改写 | 19 | `f5-04-probe-out.txt` |
| f5-05 | 显式接回 + ≥7 天澄清；三档 mode=return；红旗/病后无许可只转介；`period=return` PR 排除；日程窗 7 天 | 29 | `f5-05-probe-out.txt` |
| f5-06 | 显式渐进候选（达上限→最小增量 80→82.5）；无记录不猜重；长期 plan 草稿；非显式不给 | 16 | `f5-06-probe-out.txt` |
| 回归 | Stage2–4 关键链 | 129 | `f5-07-regression-out.txt` |
| §7 剧本 | 步 1–10 协议级+浏览器均 PASS；步 11 build | — | 本文件 §3 |

实现落点（指针，不复述契约）：`contract.ts`（Review*、`TrainingRecord.period`）；`mock/server.ts`（intent 链：`REVIEW_GENERATE` → return/clarify → `isProgression` → `isSuggestLong` → arrangement/`isSuggestAdjust` → plan；`saveReviewEntry`/`returnAssessmentReply`/`deriveProgressionCandidates`）；`mock/plan.ts`（verified load 须 `basis_record_revision_id`）；`ReviewPage.tsx`（BasisSummary）。

## 3. 特殊事件与拍板链

| 事件 | 决定 → 落地 → 复核 |
|---|---|
| 「看/展示/显示复盘」曾进 `REVIEW_GENERATE` | owner 拍**收紧**：仅「生成/重新生成/更新+复盘」append；看类只读 → 改正则+GENERIC_REPLY 示例+补 f5-02「看复盘」不 append 断言 → f5-02 31 PASS |
| 病后未获专业允许 | owner 拍**只转介/建议休息**（非 PRD §5.12「最低活动建议或转介」字面）→ `returnAssessmentReply` 无许可不落处方 → f5-05 PASS |
| 中断 7 天 | owner 拍**代码常量** `INTERRUPT_DAYS=7`，不暴露设置 → 闸门只澄清，确认才评估 → f5-05 PASS |
| 最低版日程曾投影 >7 名额 | 对齐正本「3–7 天」→ 接回 draft `review_on=starts_on+7`（仍单套 `projectSchedules`）→ f5-05 复跑 29 PASS |
| f3-01 启动断言误伤 | 现象：F5-01 在 `recomputeStats` 与 `return state` 间插 basis 回填，原正则 FAIL → 放宽相邻行、**仍锁 recomputeStats 调用** → f3-01 36 PASS |
| 渐进长期草稿基线 | 种子含档案外器械（腿屈伸），原样改 load 会 `catalogViolation` → 以 `buildPplDraft` 重生成再写 verified load（与 F5-04 同链路）→ f5-06 PASS |
| B 档范围 | 原拍另拆 → owner 改拍并入 Stage5 为 F5-05/06 → 计划确认后实施；F5-06 曾后置再开工 |

## 4. Owner 浏览器走查（§7）

前置：default 种子 mock 日期 `2026-09-11`；`npm run dev`。

| 步 | 操作要点 | 结论 |
|---|---|---|
| 1 | `/review` 统计只读、无输入 | PASS |
| 2 | 对话「生成复盘」→ 最新条+冻结数字 | PASS |
| 3 | 建议不自动生效 | PASS |
| 4 | 调整意图→草稿→确认 cv+1；幂等/失败 | PASS |
| 5 | 更正/作废→stale 徽章、正文不变 | PASS |
| 6 | 重新生成再追加 | PASS |
| 7 | empty 暂无/拒绝不编造 | PASS |
| 8 | 失败注入不落→重做 | PASS |
| 9 | 刷新查询恢复 | PASS |
| 10 | 接回三档+渐进建议确认链 | PASS |
| 11 | build+归档 | PASS（协议级） |

依据：`stage5.md` §7；口述确认（无截图）。

## 5. 复现

```bash
cd frontend
node node_modules/typescript/bin/tsc -b --pretty false
node scripts/f5-01-probe.mjs   # expect 27
node scripts/f5-02-probe.mjs   # expect 31
node scripts/f5-03-probe.mjs   # expect 22
node scripts/f5-04-probe.mjs   # expect 19
node scripts/f5-05-probe.mjs   # expect 29
node scripts/f5-06-probe.mjs   # expect 16
# 回归（可选门）
node scripts/f4-01-probe.mjs   # 30
node scripts/f4-05-probe.mjs   # 31
node scripts/f3-02-probe.mjs   # 32
node scripts/f3-01-probe.mjs   # 36
npm run build                  # EXIT=0
```

- 入库：本文件、`f5-0{1..6}-probe-out.txt`、`f5-07-regression-out.txt`、探针脚本。（原 f5-01–06 worker report 已并入本文件并删除）
- 不入库：`*.log`（`.gitignore`；`f5-07-build.log` 本地可留）。

## 6. 缺口（红线：不得当成已验证）

1. **mock≠真实**：真实 `POST /api/reviews` / `ReviewStore` 竞态、真实模型正文质量、真实 DB/HTTP/SSE/Provider、Windows 验收、09 章测评、F1/F5–F9 端点拆分——均未验证。
2. **当次加重处置未拍**：安排 keep/deload 语义限制；仅长期 plan 渐进草稿路径。
3. **渐进草稿基线 = `buildPplDraft` 重生成**：确认后动作集可能与种子不完全一致。
4. **完成率仍计回归期**：仅 PR 排除 `period=return`；若 06 章要求完成率也排除需另拍。
5. **档案级红旗接回二次断言弱**：消息级红旗已覆盖；档案红旗靠既有 plan 校验。
6. **load 候选 =「最近一条达区间上限的 valid 记录」**，非字面「最近一条记录」。
7. **`帮我加重` 只出文本**；长期意图才出草稿（改 `PROGRESSION_LONG` 一行可扩）。
8. **子草稿终态后再 recalc**（01 1.6 完整语义）Stage4 遗留，本阶段未扩。
9. **接回日程窗**：已收窄 `review_on=starts_on+7`；若未来放宽复核窗，须再截断投影，勿默认 `projectSchedules` 全窗。
10. **§7 步 9**：刷新恢复协议级读回 `/api/review`+`/api/stats`；草稿侧沿用 f3-06 既有口径，本阶段**未另跑 f3-06**。
11. **有意不作为**：未写 `f5-07-evidence-probe.mjs`（空口已被 f5-01–06 覆盖，不硬造）。

## 7. 结论

**Stage 5 已结项（协议级 + owner 走查，2026-09-13）**：F5-01–07 完成；F5=144/0，回归=129/0，build EXIT=0。mock 通过不得声称为真实链路通过。
