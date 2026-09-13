# F5-05 worker report：中断接回与最低训练版

- **日期**：2026-09-13
- **任务**：F5-05（只做本项；不做 F5-06/F5-07）
- **状态**：协议级完成（mock 探针全绿）
- **范围**：对话剧本触发接回评估 → mode=return 计划草稿 → 既有 plan 事务确认；回归期标签与 PR 排除

## 已拍口径落地

| 口径 | 实现 |
| --- | --- |
| 触发① 显式「重新开始/中断回归/接回」 | `RETURN_EXPLICIT` → `returnAssessmentReply` |
| 触发② 今日训练且距最近已确认 ≥7 天 | `INTERRUPT_DAYS=7`；`needInterruptClarify` 只澄清；「是中断/确认接回」才评估 |
| 无已确认记录不判中断 | `interruptGapDays` → null；今日训练走既有指导 |
| 病后未获专业允许 | 只转介/建议休息，不落训练处方 |
| 红旗 | 消息级+档案级 `classifyBodyConditions.confirmed`；不生成三档处方 |
| 三档 | 正常=原结构；降级=−1组+RIR≥2；最低=启动活动文案+一个主项≈15min；mode=return |
| 铁律 | 不补课/不惩罚/不照搬重量（上限参考+needs_calibration 文案）；新版本草稿；复用 plan 事务 |
| 回归期 | `period?: "return"` 写在记录修订；PR 排除；完成率/工作组仍计 |

## 改动文件

1. **`src/lib/contract.ts`**：`TrainingRecord.period?: "normal" | "return"`（最小字段；mock 消费；ReviewPage 不读该字段不崩）
2. **`src/mock/server.ts`**：
   - `INTERRUPT_DAYS` / 意图正则 / 间隔闸门 / `returnAssessmentReply` / `buildReturnProposal`
   - `replacementPayload` 支持 `mode: "return"` 与自定义 `calendar_cycle`（最低版）
   - 训练记录确认时：`plan.mode === "return"` → `period: "return"`
   - `recomputeStats` PR 排除 `period === "return"`
3. **`scripts/f5-05-probe.mjs`**：新建协议探针

## 验证（真跑）

| 命令 | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误 |
| `node scripts/f5-05-probe.mjs` | **29 passed, 0 failed** |
| `node scripts/f5-04-probe.mjs` | **19 passed, 0 failed** |
| `node scripts/f5-02-probe.mjs` | **31 passed, 0 failed** |

### f5-05 覆盖摘要

- 显式接回 → plan 草稿 mode=return；确认前隔离；fail-next 回滚；成功 cv+1、旧版 future 未锁定取消；幂等
- 作废 09-07/09-05 → 间隔 9 天；今日训练只提示；未确认不改计划；确认后评估出草稿
- 全作废无 valid → 不触发中断澄清
- 消息红旗「刺痛」→ 无草稿+专业评估+接回无权解除
- 病后无许可 → 只转介；「已获专业允许」→ 可出草稿
- 回归期打卡 100kg → `period=return`；PR 仍 80×8

## 缺口 / 边界（如实）

1. **档案级红旗接回二次断言弱**：对话一次「身体情况：胸部明显疼痛」未稳定产出可确认的 profile 草稿；消息级红旗已覆盖「接回不解除红旗」。档案红旗由既有 `buildPplDraft`/`planPayloadError` 同源拦截。
2. **最低版日程投影**：3 日循环（练/休/休）在 `[starts_on, review_on)` 内会投影多于 7 个名额；文案写「未来 3–7 天简单日程」，未截断投影上限（复用 `projectSchedules`，避免第二套投影）。
3. **完成率仍计回归期记录**：按「统计仍展示工作组」；仅 PR 排除。若后续 06 章要求完成率也排除，需另拍。
4. **降载比例未固化数值**：负荷以「最近确认记录为上限」+ needs_calibration 文案表达，未在 plan payload 写死 VerifiedLoad（与 D3 无记录不猜重量一致；有记录时指导路径仍可 `referenceWorkKg`）。
5. **不做**：F5-06 自动加重、后台检测推送、恢复常规自动版本（须复盘后另启新常规版本——仅文案提示）。

## 未 commit / 未动 backend / pre-prj

符合任务约束。
