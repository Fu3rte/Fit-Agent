# Stage 4 验收证据（F4-01–F4-07）

> 对应 `plans/stage4.md` 第 5 节 F4-01–07 与第 6 节证据格式。**Stage 4 已协议级结项（2026-09-13）**：owner 确认计划 + 浏览器 §7 全剧本走查通过。
> **F4-07 证据层（2026-09-13）**：协议级第 7 节 11 步覆盖核对 + 探针全量重跑归档 + `npm run build` 零错误。
> **证据分层：**
> 1. **协议级**（mock HTTP / 模块级）：§2–§3 实际执行结果，可复现。
> 2. **UI / 浏览器走查**：owner 浏览器 §7 全剧本走查通过（2026-09-13）。
> 3. mock 通过 ≠ 真实链路通过：未接真实后端/模型/数据库/Windows 验收。
> 4. 范围：只改 `frontend/`；未改 `backend/`、`pre-prj/` 决策正本。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Windows；Node v22.22.3；TypeScript 5.8.3；vite 7.3.6 |
| 代码版本 | 复跑时 HEAD `ee72a18`；Stage 4 改动**在工作树未提交**（f4-01–06 + 本任务新增 f4-07 探针/证据） |
| 证据日期 | 2026-09-13（F4-07 协议级闭环归档） |
| mock 运行 | 探针以 vite 程序化启动（`port:0` 随机端口），不触碰用户 5173 |
| 网络 | 进程内 fetch，无外网、无模型调用 |
| 归档产物 | `plans/stage4-evidence-assets/`：`f4-0{1..6}-probe-out.txt`、`f4-07-evidence-probe-out.txt`、`f4-07-regression-out.txt`（f3 全量 + f2-04）、`f4-07-build.log`（`*.log` 不入库）、各探针明细 `*-out.txt`、`HANDOVER.md` |

主要实现文件（F4-01–06）：`src/lib/contract.ts`、`src/lib/api.ts`、`src/mock/server.ts`、`src/features/records/RecordsPage.tsx`、`src/features/chat/DraftCard.tsx`、`src/features/chat/ChatPage.tsx`、`scripts/f4-0{1..6}-probe.mjs`。F4-07 新增：`scripts/f4-07-evidence-probe.mjs`（不改业务代码）。

## 1. 构建

| 命令 | 预期 | 实际 |
|---|---|---|
| `node node_modules/typescript/bin/tsc -b` | 零错误 | **EXIT=0**（2026-09-13 F4-07 复跑） |
| `npm run build`（`tsc -b && vite build`） | 零错误 | **EXIT=0**，`✓ built in 5.59s`；仅既有 chunk>500kB 与字体解析警告；归档 `f4-07-build.log` |

## 2. 协议探针（F4-07 重跑归档）

方法：沿用 f2/f3 模式（vite 起服务 + `/api/*` + 模块断言）；无测试框架、无新依赖。下列为 **2026-09-13 F4-07 全量重跑**（exit 均为 0，FAIL 均为 0）。

| 探针 | 覆盖 | 结果 | 归档 |
|---|---|---|---|
| `f4-01-probe.mjs` | 契约 `voided`/修订链/种子收口；作废事务；fail-next；终态 | **PASS=30 / FAIL=0** | `f4-01-probe-out.txt` |
| `f4-02-probe.mjs` | 定位/完整修订/未述保持/Diff/越界拒绝/`draft_modified`/歧义/无法定位/voided 终态/三桶 5→6 | **PASS=31 / FAIL=0** | `f4-02-probe-out.txt` |
| `f4-03-probe.mjs` | 作废草稿/确认/幂等/丢弃/fail-next 回滚/终态拒绝（含补全与二次作废） | **PASS=27 / FAIL=0** | `f4-03-probe-out.txt` |
| `f4-04-probe.mjs` | 补全 incomplete→valid；PR 自动纳入；无对照不进三桶；仍不完整确认仍 incomplete | **PASS=22 / FAIL=0** | `f4-04-probe-out.txt` |
| `f4-05-probe.mjs` | Records 修订追溯/voided 徽章/三桶现算渲染；Review stale 徽章+正文不变；只读无 RIR | **PASS=31 / FAIL=0** | `f4-05-probe-out.txt` |
| `f4-06-probe.mjs` | stale→recalc→再确认；Pending 子稿幂等；training_void recalc 分支；fail-next 子稿回滚 | **PASS=34 / FAIL=0** | `f4-06-probe-out.txt` |
| `f4-07-evidence-probe.mjs` | **§7.9 补口**：empty 空数据 + 更正/作废/预填文案无法定位不落草稿；§7.1 入口预填源码断言；CHECKIN 门禁 | **PASS=11 / FAIL=0** | `f4-07-evidence-probe-out.txt` |
| f3 全量 + f2-04 回归 | Stage 2/3 链路未回归 | f2-04 **22/0**；f3-01 **36/0**；f3-02 **32/0**；f3-03 **26/0**；f3-04 **50/0**；f3-05 **18/0**；f3-06 **41/0** | `f4-07-regression-out.txt` |

**F4 合计 PASS=186 / FAIL=0**（30+31+27+22+31+34+11；计数排除 summary 行，与 stage3 口径一致）。

## 3. 第 7 节 11 步覆盖对照

| §7 步 | 主覆盖探针 | 结果 | 备注 |
|---|---|---|---|
| 1 入口与定位（/records 预填 → 对话定位） | f4-02（协议定位）+ f4-07（源码预填模板/CHECKIN）+ f4-02 §9 歧义 | 协议+源码 PASS | **浏览器点按钮未走查**（owner 未做） |
| 2 更正草稿完整修订/Diff/无 RIR | f4-02 | PASS | 载荷 3 组、未述保持、Diff 5→6、无 RIR |
| 3 内联纠错与越界/draft_modified | f4-02 | PASS | 改身份/关联拒；所见 revision 不匹配 409 `draft_modified` |
| 4 确认与刷新（三桶/PR/cv） | f4-02 + f4-05 | PASS | cv 恰 +1；三桶 1/1/1→2/0/1；W2 仍 1/3；Records 追溯；Review 消费现算 |
| 5 冲突与重算（stale/recalc/幂等/丢弃） | f4-06 + f3/f2 既有 stale 链路 | PASS | 409 `draft_stale`；子稿 `parent_draft_id`；Pending 幂等；丢弃拒；fail-next 回滚 |
| 6 作废 | f4-03 + f4-05 | PASS | 退出统计、历史保留、幂等；PR 不变 |
| 7 补全 | f4-04 | PASS | 09-05→valid、进 PR、不进三桶、身份不变 |
| 8 复盘不重写 | f4-05 | PASS | stale=true；正文与 `generated_at` 逐字不变；徽章文案源码断言 |
| 9 空数据与无法定位 | f3-06（empty records/stats）+ **f4-07**（empty 更正/作废/预填不落草稿） | PASS（F4-07 补口后） | 此前 f4-01–06 仅 default 种子；本轮已补 empty 分支 |
| 10 失败与恢复 + build | f4-03/06 fail-next + f3-06 刷新协议层 + `npm run build` | PASS | **浏览器刷新未做**（协议层 session drafts 读回） |
| 11 作废终态 | f4-02/f4-03 | PASS | 更正/补全/二次作废均无草稿、不推 cv；确认侧 409 `invalid_request` |

## 4. F4-01–07 验收对照

| 任务 | 验收要点 | 结论 |
|---|---|---|
| F4-01 | 契约唯一来源；修订链投影；种子收口；作废不进统计；重算体正本 | 协议级 PASS（30/0）+ owner 走查通过 |
| F4-02 | 定位/完整修订/越界拒/draft_modified/终态生成拒 | 协议级 PASS（31/0）+ owner 走查通过 |
| F4-03 | 作废事务/幂等/回滚/终态确认拒 | 协议级 PASS（27/0）+ owner 走查通过 |
| F4-04 | 补全转 valid + PR 自动纳入 | 协议级 PASS（22/0）+ owner 走查通过 |
| F4-05 | 修订追溯/统计刷新/复盘不重写/只读 | 协议级 PASS（31/0）+ owner 走查通过 |
| F4-06 | stale/recalc/幂等/void 分支 | 协议级 PASS（34/0）+ owner 走查通过 |
| F4-07 | 第 7 节协议覆盖核对 + 证据归档 + build | 协议级 PASS（11/0 + 全量回归 + build 0）+ owner 走查通过；**整阶段协议级结项** |

## 5. mock 边界（不得标为已验证）

以下**明确未验证**，不得写成已通过：

- 真实后端更正／作废／重算接口、真实数据库修订指针与事务
- 真实 Agent 定位与事实整理质量（mock 为脚本化 `correctionScriptReply` / `voidScriptReply`）
- 复盘正文生成／重新生成交互（Stage 5；本阶段只展示 stale 标记）
- Windows 验收（owner 本机验收流）
- 交接 F1／F5–F9 端点拆分与形状重构
- 子草稿进入终态后再 recalc（01 1.6 完整语义，本阶段不实现）
- 日期／反馈等非动作组字段的更正演示（沿用同边界，未单列断言）

## 6. 走查与结项记录

| 项 | 状态 | 说明 |
|---|---|---|
| 浏览器第 7 节全剧本 | **owner 走查通过（2026-09-13）** | 协议级 + 浏览器点验 |
| F4-05 Records/Review UI | **owner 走查通过** | 修订追溯/三桶/复盘徽章 |
| F4-06 DraftCard stale/一键重算 | **owner 走查通过** | |
| §7.1 预填跳转 | **owner 走查通过** | 源码断言亦覆盖 |
| §7.10 浏览器刷新恢复 | **owner 走查通过** | 协议层 f3-06 亦覆盖 |
| 复盘 false→true 翻转 | **不做** | mock 无显式复盘写路径；等价不变量已断言 |
| 整阶段 owner 结项 | **已完成（2026-09-13）** | 确认 stage4.md 为阶段契约正本 |

## 7. 结论

F4-01–07 协议级完成并经 reviewer；F4 合计 PASS=186 / FAIL=0；`npm run build` 零错误。  
**Stage 4 协议级结项（2026-09-13）**：owner 确认计划 + 浏览器 §7 全剧本走查通过。

真实链路与 Windows 验收不在本证据范围（Stage 6）。mock 通过不得声称为真实链路通过。
