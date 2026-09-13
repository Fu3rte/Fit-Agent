# F5-06 worker report：基于可信历史的渐进／负荷建议

- **日期**：2026-09-13
- **任务**：F5-06（只做本项；不做 F5-07）
- **状态**：协议级完成（mock 探针全绿）
- **范围**：显式渐进／加重意图 → 基于已确认有效记录 + progression 规则的候选；可选长期 plan 草稿；确认走既有事务

## 已拍口径落地

| 口径 | 实现 |
| --- | --- |
| 仅显式意图 | `PROGRESSION_HINT`（加重/加重量/渐进/渐进建议/帮我加重/按最近表现…）；非显式不给 |
| 仅已确认有效记录 | `deriveProgressionCandidates` 只读 `status=valid` 且非 `period=return` 记录 |
| 无可信记录 → 校准 | `kind=calibration` 文案；**不猜重量** |
| 最小增量 | `MIN_LOAD_STEP`：杠铃/绳索/器械 2.5、哑铃 1（mock 写死） |
| 次数上限 | 外加负重 ≤12、自重 ≤15（`REPS_CAP_*`） |
| 缺体感不阻塞/不当轻松可加重 | 文案边界写明；派生不读 RIR 做加重条件 |
| 文本 + 可选草稿；不自动改正式计划 | 纯意图（如「帮我加重」）只出文本；`PROGRESSION_LONG`（「按最近表现加重调整计划」等）才出 plan 草稿 |
| 确认走既有事务 | `pendingPlanDraft` + `replacementPayload`（Stage 2 链路）；fail-next 回滚、幂等 |
| 不发明 D3 外阈值 | 无新校准阈值；无自动加重/后台检测 |
| 分支顺序 | 接回/澄清优先；`isProgression` 在 `isSuggestLong` 之前且不吞 F5-04/05 探针短语 |

## 改动文件

1. **`src/mock/server.ts`**
   - `MIN_LOAD_STEP` / `REPS_CAP_EXTERNAL` / `REPS_CAP_BODYWEIGHT`
   - `PROGRESSION_HINT` / `PROGRESSION_LONG`
   - `deriveProgressionCandidates` / `progressionCandidateLine` / `progressionScriptReply`
   - `runScript`：`isProgression` 意图 + 分支接线
   - 长期草稿：以 `buildPplDraft` 为基线写入 load 候选为 `verified`（种子计划含档案外器械如腿屈伸，直接复用种子 workouts 会过不了 `catalogViolation`）
2. **`src/mock/plan.ts`**
   - `itemStructuralError`：允许 `verified` 负荷（须 `value>0` + `load_notation` + `basis_record_revision_id`）；`needs_calibration` 路径不变（种子/首版生成仍恒为 needs_calibration）
3. **`scripts/f5-06-probe.mjs`**：新建协议探针
4. **`plans/stage5.md`**：F5-06 状态勾选与验证证据行（worker 已改；文档一致性由 reviewer 复核）

## 验证（真跑）

| 命令 | 结果 |
| --- | --- |
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误 |
| `node scripts/f5-06-probe.mjs` | **16 passed, 0 failed** |
| `node scripts/f5-05-probe.mjs` | **29 passed, 0 failed** |
| `node scripts/f5-04-probe.mjs` | **19 passed, 0 failed** |
| `node scripts/f5-02-probe.mjs` | **31 passed, 0 failed** |

### f5-06 覆盖摘要

- 源码：常量/派生函数/接线；planPayloadError 允许 verified；无新业务端点
- 显式「帮我加重」→ 文本含种子卧推候选 80→82.5；无 draft；cv/plan 不变
- 「按最近表现加重调整计划」→ plan 草稿（新版本+负荷 diff）；确认前隔离；fail-next 500 回滚；成功 cv+1、旧版 future 未锁定取消；幂等
- empty 种子显式加重 → 不给具体重量数字（走建档）
- 非显式「聊聊复盘」→ 无负荷草稿、cv 不变
- 生成复盘正文后渐进建议 → text/generated_at 逐字不变

## 缺口 / 边界（如实）

1. **长期草稿基线 = `buildPplDraft` 重生成**，不是种子计划原样改 load：种子含档案器械外动作（腿屈伸），原样 `replacementPayload` 会被 `catalogViolation` 拒。确认后新版本动作集可能与种子不完全一致（与 F5-04「按建议调整计划」同链路行为）。
2. **当次安排加重草稿未做**：安排 `keep` 校验要求负荷与计划全等，`deload` 只允许降载；当次加重需另拍处置语义。本任务只交付长期 plan 草稿路径。
3. **load 候选判定用「最近一条全部工作组达区间上限的 valid 记录」**（如种子 08-31 卧推 80×8），不是「最近一条记录」（09-07 deload 未达上限）。符合 progression「达上限后加重」规则，但与「最近一条记录读负荷」字面略有偏差。
4. **`帮我加重` 只出文本**（不直接出草稿）：与「按最近表现加重」分流；文案引导用户再说长期意图。若 owner 要求「帮我加重」也出草稿，改 `PROGRESSION_LONG` 一行即可。
5. **不做**：自动加重、后台检测、医学阈值、动作替代完整流程。

## 未 commit / 未动 backend / pre-prj

符合任务约束。
