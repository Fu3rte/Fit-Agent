# Stage 3 验收证据（F3-01–F3-06）

> 对应 `plans/stage3.md` 第 5 节 F3-01–06 与第 6 节证据格式。**Stage 3 已结项（2026-09-12 owner 确认）**。
> **证据分层：**
> 1. **协议级**（mock HTTP / 模块级）：§1–§3 实际执行结果，可复现。
> 2. **UI 渲染 / 浏览器走查**：主路径已走查（无截图/录屏，口头验收）；§7.10–14 以协议探针为准，owner 不再单独人工复测。
> 3. mock 通过 ≠ 真实链路通过：未接真实后端/模型/数据库。
> 4. 范围：只改 `frontend/`；未改 `backend/`、`pre-prj/` 决策正本。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Windows；Node v22.22.3；TypeScript 5.8.3；vite 7.3.6 |
| 代码版本 | 复跑时 HEAD `c52e4f4`；Stage 3 改动**在工作树未提交** |
| 证据日期 | 2026-09-12（含 owner 走查后修正复跑） |
| mock 运行 | 探针以 vite 程序化启动（`port:0` 随机端口），不触碰用户 5173 |
| 网络 | 进程内 fetch，无外网、无模型调用 |
| 归档产物 | `plans/stage3-evidence-assets/`：`f3-0{1..6}-probe-out.txt`（f3-06 为 `f3-06-closed-loop-probe-out.txt`）、`f2-0{1..5}-probe-out.txt`；`f3-build.log`（`*.log` 不入库） |

主要实现文件：`src/lib/contract.ts`、`src/lib/planView.ts`、`src/lib/api.ts`、`src/mock/server.ts`、`src/features/profile/ProfilePage.tsx`、`src/features/chat/DraftCard.tsx`、`src/features/chat/SetInputs.tsx`、`src/features/records/RecordsPage.tsx`、`scripts/f3-0{1..5}-probe.mjs`、`scripts/f3-06-closed-loop-probe.mjs`。

## 0b. Owner 赞查后修正（2026-09-12）

| 反馈 | 处置 |
|---|---|
| 指导文案出现「目标 RIR」 | 改为大白话「每一组结束还能再做 N–M 次的重量」；有记录后直接标参考重量 |
| 罗马尼亚硬拉被识别成传统硬拉 | `RECORD_EXERCISES` 优先匹配「罗马尼亚」 |
| 「让人辅助才做完」未标辅助 | 辅助词表扩展 |
| 记录草稿 RIR | **已拍隐藏且不落库**（编辑器不展示、解析不写入、`/records` 不展示） |
| 组数 0 / 删动作 | 不改：`work_sets>0`；不练用 `local_skip`；删动作不属本阶段 |

## 1. 构建

| 命令 | 预期 | 实际 |
|---|---|---|
| `npm run build`（`tsc -b && vite build`） | 零错误 | **EXIT=0**，`✓ built in 7.48s`（owner 赞查修正后复跑）；仅既有 chunk>500kB 警告 |

## 2. 协议探针

方法：沿用 f2 模式（vite 起服务 + `/api/*` + 模块断言）；无测试框架、无新依赖。

| 探针 | 覆盖 | 结果（owner 赞查修正后复跑） | 归档 |
|---|---|---|---|
| `f3-01-probe.mjs` | 契约 S4-04 字段、`GET /api/arrangements`、种子 3.5 事实表（W1 2/3、W2 1/3、三桶 1/1/1、PR）、统计现算、revise/confirm 凭据与幂等、CHECKIN 门禁移除 | **PASS=36 / FAIL=0** | `f3-01-probe-out.txt` |
| `f3-02-probe.mjs` | 安排四种处置边界、状态档位（正常/一般/明显）、越界拒绝、接受即落盘、幂等、失败回滚、local_skip/keep、无 RIR 缩写 | **PASS=32 / FAIL=0** | `f3-02-probe-out.txt` |
| `f3-03-probe.mjs` | Profile 徽章分类、今日尚无安排/09-07 已调整、指导优先安排、限制/红旗整份阻断、只读、指导主文案无 RIR 缩写 | **PASS=26 / FAIL=0** | `f3-03-probe-out.txt` |
| `f3-04-probe.mjs` | 打卡解析（相对日期/辅助/热身）、RIR 不解析不落库、API 负 RIR 拒、歧义先问、安排关联、incomplete 排除、无安排加练、统计刷新、幂等 | **PASS=50 / FAIL=0** | `f3-04-probe-out.txt` |
| `f3-05-probe.mjs` | 记录卡三份事实（原计划 4 · 当次安排 3 · 实际）、组级 judgement、无对照/incomplete 不判定、不展示用力余量、两页只读、ReviewPage 消费 getStats、分母零「暂无」 | **PASS=18 / FAIL=0** | `f3-05-probe-out.txt` |
| `f3-06-closed-loop-probe.mjs` | §7 第 1–14 步串联/缺口补断言、空数据、记录失败注入回滚、stale→recalc→再确认、丢弃后拒确认、刷新恢复协议层 | **PASS=41 / FAIL=0** | `f3-06-closed-loop-probe-out.txt` |
| `f2-01`–`f2-05` 回归 | Stage 2 计划/安全链路未回归（此前抽样 f2-04/05） | f2-04 **PASS=22 / FAIL=0**；f2-05 **PASS=29 / FAIL=0** | `f2-0{1..5}-probe-out.txt` |

**F3 合计 PASS=203 / FAIL=0**（36+32+26+50+18+41）。

## 3. F3-06 与 §7 覆盖对照

| §7 步 | 主覆盖探针 | f3-06 断言 | 备注 |
|---|---|---|---|
| 1 查看与指导 | f3-03（主）+ f3-06 串联 | 今日锁定无安排 / 09-07 已调整 / 接受前按计划 | f3-06 在 default 种子上重断言 |
| 2 提出调整 | f3-02（主）+ f3-06 | deload 草稿 + session drafts 可读回 pending | |
| 3 状态档位 | f3-02（主）+ f3-06 | 明显状态差 → 建议休息、无草稿 | 正常档由第 1 步覆盖 |
| 4 内联纠错与校验 | f3-02（全量越界）+ f3-06 | 加组拒 + 合法再减组 revision+1 | 完整越界矩阵在 f3-02 |
| 5 接受即落盘 | f3-02（主）+ f3-06 | 凭据、cv+1、已调整徽章语义、幂等、锁定日可减组 | |
| 6 指导优先安排 | f3-03（主）+ f3-06 | 接受后按已接受安排回复 | |
| 7 打卡反馈 | f3-04-B（主）+ f3-06 | 关联 + 热身原文 + 辅助 + 空 RIR | |
| 8 纠错确认与回滚 | f3-04-B + f3-02 失败注入 + f3-06 | **记录侧**失败注入全回滚 → 重做成功；W2 1/3→2/3；幂等；三桶 met+2 | 安排侧失败注入见 f3-02 |
| 9 三份事实 | f3-05（主）+ f3-06 | planned 4 · arranged 2 · actual 2；辅助组不进 PR | 种子 4/3 见 f3-05 |
| 10 无安排加练 | f3-04-D（主）+ f3-06 | 不计完成率、judgement null、可进 PR | |
| 11 同日多练与歧义 | f3-04-C（主）+ f3-06 | 歧义询问、补充复用身份、新增新身份、**同一安排多次反馈完成率仍 2/3** | 「完成最多计一次」为 f3-06 补口 |
| 12 待补全 | f3-04-D（主）+ f3-06 | incomplete 不进分子、不抬升 PR | 补全交互归 Stage 4（不做） |
| 13 安全与公共冲突 | f3-03（限制/红旗主路径）+ f3-02（失败注入）+ f3-06 | 限制整份阻断抽验未回归；**draft_stale → recalc → 再确认**；**丢弃后确认 409** | 红旗主路径在 f3-03 |
| 14 刷新恢复与收尾 | f3-06 | 协议层：committed/pending/discarded 经 session drafts 读回；正式数据仍在；build 零错误 | **浏览器刷新未做** |

空数据（§6 边界，非 §7 编号）：empty 种子 → records `[]`、stats `per_week []` / buckets 0/0/0 / prs `[]`、arrangements `[]`。

## 4. F3-01–05 验收对照（保留）

| 任务 | 验收要点 | 结论 |
|---|---|---|
| F3-01 | 契约唯一来源；种子 3.5 可查；统计现算；cv 用 mock 时钟 | 协议级 PASS |
| F3-02 | 越界服务端拒；明显状态差无草稿；local_skip/keep 路径；幂等；锁定日可减组 | 协议级 PASS |
| F3-03 | 今日「已锁定·尚无安排」；09-07「已接受·已调整」；目标更保守；阻断无处方；指导优先安排 | 协议级 PASS（徽章/指导经 API+源码断言；**浏览器未走查**） |
| F3-04 | 事实不编造；歧义先问；关联仅显式；incomplete 排除正确；同日多练身份 | 协议级 PASS |
| F3-05 | 三份事实区分（4/3/3）；组级徽章按当次安排只看次数；未报告口径；两页只读；Review 消费现算 | 协议级 PASS（REST+源码断言；**浏览器未走查**） |
| F3-06 | §7 协议级串联与缺口；build 零错误；证据归档；mock 边界写明 | 协议级 PASS；owner 主路径已走查并确认结项 |

Reviewer 修正（F3-03，此前已修）：`classifyArrangementStatus` 降 RIR/条目集合不一致原先误判 identical → adjusted；确认摘要全等 keep 误标「目标更保守」→ 须至少一项真的升 RIR。

F3-06 探针修正记录：§7.11「补充上一练」语义为复用**当日最近一次**既有 `training_session_id`（f3-04 场景 C 建立身份后单独验证）；闭环主路径在无安排加练之后，断言改为「复用既有身份之一、不新增」，非 bug。

## 5. 明确未验证（不得标为已通过）

- 真实后端安排/记录/统计接口、真实 Agent 打卡整理、真实数据库事务与修订指针
- Stage 4+：更正/作废/补全转 valid、历史修订、复盘生成
- 交接 F1/F5–F9 端点重构、equivalent_replace 完整候选交互
- mock 专用 `GET /api/arrangements` 与后端 S3-14 的映射（接真实后端时收口）

## 6. 结论

F3-01–06 **协议级验收通过**（F3 合计 PASS=203 / FAIL=0）；`npm run build` 零错误；F2 抽样（f2-04/05）回归通过。  

Owner 已走查主路径并驱动修正（目标用力文案、参考重量、罗马尼亚匹配、辅助词表、记录侧 RIR 隐藏）；2026-09-12 owner 确认 **Stage 3 结项**。  

mock 通过不得声称为真实链路通过；F1/F5–F9 与安排读回端点仍留联调阶段。
