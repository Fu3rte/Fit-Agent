# Stage 1 验收证据（F1-05：闭环演示与证据）

> 按 `frontend/plans/stage1.md` §6 归档，口径沿用 `plans/stage0-evidence.md`。
> **证据分层**：① 协议级（mock HTTP）命令与结果可复现；② 浏览器 UI 为 owner 2026-09-10 **口述确认（无截图/录屏）**；③ mock 通过 ≠ 真实链路——本阶段不接真实后端，未验证项见「缺口」；④ 2026-09-10 据走查勾选 `plans/stage1.md` §9，未改决策正本。
> **基线**：HEAD `6b4aee3`（前端 Stage 1 已入库；协议级证据对应此提交）；WSL2 / Node v24.19.0 / npm 11.17.0 / vite 7.3.6；探针专用 5199（用户 5173 未触碰），`curl --noproxy '*'` 直连；证据日期：协议级 2026-09-09、走查 2026-09-10。

## 1. 结果正本

| 项 | 方式 | 结果 |
|---|---|---|
| 构建 | `npm run build`（`tsc -b && vite build`，`frontend/`，修复后复跑） | **EXIT=0**；1979 modules，6.36s；产物 JS 561 kB / CSS 268 kB（gzip 173/106）；仅有既有 chunk>500kB 警告 |
| 主探针 | `plans/stage1-evidence-assets/f1-05-probe.sh`（空种子起、轮询终态防 busy） | **PASS=110 / FAIL=0，elapsed 28s**；日志 `f1-05-probe-out.txt` |
| 缺口/回归探针 | `f1-05-gap-probe.sh`（红旗跨轮、追问顺序、打卡、粒度等） | **PASS=30 / FAIL=0，elapsed 73s**；日志 `f1-05-gap-probe-out.txt`；修复前同脚本 29/30（`f1-05-gap-probe-prefix-fail.txt`） |
| owner 浏览器走查 | 2026-09-10，`stage1.md` §7 第 1–10 步与 U1–U8 | 全部通过（口述确认，逐项见下表） |

### 1.1 owner 走查明细（2026-09-10，口述确认）

| # | 走查项 | 依据 | 结论 |
|---|---|---|---|
| U1 | `/profile` 未建档引导卡 +「前往对话开始建档」跳转；对话页未建档文案 | §5 F1-04、§7 第 1 步 | 通过 |
| U2 | 设置页录入 Key → 回对话页可输入 | §7 第 2 步 | 通过 |
| U3 | 草稿卡六类字段/体重/限制/红旗渲染、Diff 呈现 | §5 F1-03、§7 第 5 步 | 通过 |
| U4 | 内联纠错控件（频率/体重/器械/限制增删/粒度）→ `revision+1`、Diff 实时更新 | §5 F1-03、§7 第 6 步 | 通过 |
| U5 | 确认 toast + 看板刷新；档案卡 +「暂禁」限制卡 +「尚无计划」占位 | §7 第 7 步 | 通过 |
| U6 | 待确认刷新→草稿卡恢复；确认后刷新→`committed` 与档案页一致 | §7 第 9 步 | 通过 |
| U7 | 45s 无事件 → EventSource 关闭转 `/api/runs/active` 轮询 | stage0 F0-04、§7 第 9 步 | 通过 |
| U8 | 断线/刷新恢复无重复拼接；幂等确认、丢弃拒绝、stale→重算→确认 | stage0 §7 第 5 步、§7 第 8/10 步 | 通过 |

## 2. 探针覆盖对照（全部 actual==expected；check ID 见归档日志）

| 要求 | check | 关键断言摘要 |
|---|---|---|
| 空重置 | A1–A10 | `reset seed=empty` → `profile=null`、`context_version=0`、草稿/会话/记录全空 |
| Key/会话 | B1–B3 | `PUT api-key` → `has_api_key=true`（无明文）；建会话成功 |
| 多轮缺失追问 | T1a–T1g | 仅「我想增肌」→ 追问缺失事实、草稿 0、不编造默认值 |
| 红旗无建议 | T2a–T2h | 回复含线下评估、无任何训练处方标记；红旗记入待生成内容 |
| 结构化草稿 | T3a–T3r | 补齐事实 → 恰 1 份 pending 草稿 `revision=1`；payload 含红旗/限制；派生 diff 非空 |
| 确认前不变 | F1–F3 | 正式档案仍 `null`、版本 0 |
| 纠错 | R1–R8 | `revision=2`、Diff 重派生；不动正式数据 |
| 确认写入 | C1–C11 | `context_version=1`；档案+限制+红旗落库 |
| 幂等 | I1–I4 | 重复确认返回原版本与原 summary，不重复写 |
| 丢弃→拒绝 | TD/DC | discard 后确认 **409 `invalid_request`** |
| 查询恢复 | Q1–Q6 | drafts 含 committed/stale/discarded；messages 可恢复 |
| stale→重算→确认 | TB/SC/ST/TS/RC/CB | 他会话推进版本 → 确认 **409 `draft_stale`**；recalc 新草稿 base=2、旧 stale；确认后 `context_version=3` |

## 3. 产品缺陷与修复（本轮唯一，其余为探针脚本自身笔误，不改 `src/`）

- **缺陷**：同会话「先红旗、后清单外症状」时 `mock/server.ts` `onboardingTurn` 无条件清空 `physical_state`，已记录红旗丢失（违反 F1-02/§7 第 4 步）。缺口探针 G1e 实测 FAIL（29/30）。
- **修复**：仅当 `red_flags.length === 0` 才置 `undefined`（「已记录红旗不得被清空」单行守卫）；附带把 4 处 dev 端点未捕获 `JSON.parse` 收敛为 `readJsonBody`（非法 JSON→`{}`，合法请求行为不变）。
- **回归**：G1e 变 PASS（30/30）；正对照 G2a/G8a/G9a 修复前后均 PASS，缺陷仅在跨轮路径；修复后主探针 110/110、构建 EXIT=0 复跑通过。

## 4. 复现

```bash
cd frontend
npm run build                                          # 期望 EXIT=0
timeout 220 bash plans/stage1-evidence-assets/f1-05-probe.sh      # PASS=110 FAIL=0
timeout 220 bash plans/stage1-evidence-assets/f1-05-gap-probe.sh  # PASS=30 FAIL=0
```

入库产物：`plans/stage1-evidence-assets/` 下两脚本＋三 txt；`*.log` 按 .gitignore 与 Stage 0 惯例不入库。

## 5. 缺口（不得当成已验证）

1. 协议级证据仅 mock 进程：不代表浏览器渲染、交互与 45s 客户端逻辑（UI 侧仅 owner 口述走查）。
2. 真实档案域后端、真实模型调用未接；红旗对计划/指导的实际阻断、限制与计划冲突复核归后续阶段（stage1 §6 交界）。
3. 未订阅 `/api/events`（F1-05 未要求）；SSE 语义沿用 Stage 0 证据，本阶段未重复验证。
4. `draft_vs_draft_diff` 依赖会话最新事实更新，否则重算可能同载荷、Diff 为空——设计口径（与 stage0 §3.1 同源），非缺陷。
5. 仓库未新增依赖、未建测试框架、未改 `../backend/`、`../spike/`、决策正本与 `worker-timeout-review.md`。
