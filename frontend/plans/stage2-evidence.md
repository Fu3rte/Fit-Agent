# Stage 2 验收证据（F2-06：闭环演示与证据）

> 本文件按 `frontend/plans/stage2.md` 第 6 节格式归档 F2-06 的证据，沿用 `plans/stage0-evidence.md`／`plans/stage1-evidence.md` 的表格口径。
> **证据分层声明（未达成项已显式标注）：**
> 1. **协议级（mock HTTP / 模块级）**：第 1–2 节为本次实际执行的命令与结果，可复现。
> 2. **浏览器 UI / 人工走查**：**本次未执行**。F2-06 subagent 无浏览器交互能力；仓内不得新增浏览器 E2E／组件测试框架（`frontend/AGENTS.md` 存档 B6），故不以脚本伪造 UI 走查。§7 中依赖渲染与交互的项见第 3 节「UI 缺口」列，**不得标为已通过**，仍需 owner 人工浏览器走查。
> 3. mock 通过 ≠ 真实链路通过：未接真实后端（无真实 HTTP／SSE／模型调用）、无真实数据库日程锁定，未验证项见第 5 节。
> 4. 本次未改任何 `src/`、mock、契约或决策正本代码；只新增本文件与探针输出归档。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Linux 6.6.114.1-microsoft-standard-WSL2 x86_64（WSL2），Node v24.19.0，npm 11.17.0，vite 7.3.6 |
| 代码版本（HEAD） | `1ab79701dea69ec2375a5b0d7183550e0afc50ff`（短号 `1ab7970`，`frontend: Stage 1 结项…`） |
| 前端 Stage 2 变更状态 | **未提交工作树**（本切片按要求不做任何提交／暂存）。`git status --porcelain -- frontend`：` M plans/business-roadmap.md`、` M src/features/chat/DraftCard.tsx`、` M src/features/profile/ProfilePage.tsx`、` M src/lib/contract.ts`、` M src/mock/server.ts`，未跟踪 `plans/stage2.md`、`scripts/`、`src/mock/catalog.ts`、`src/mock/plan.ts`。因此上表 HEAD 只能定位基线提交，**Stage 2 证据实际对应上列未提交工作树**（无 digest 可引用，评审时以工作树为准） |
| 未跟踪产物（本次新增） | `plans/stage2-evidence.md`（本文件）、`plans/stage2-evidence-assets/f2-0{1..5}-probe-out.txt`（入库文本）；`plans/stage2-evidence-assets/f2-06-build.log` 为本地取证文件（`.gitignore:26 *.log`，不入库，同 Stage 0/1 惯例） |
| 证据日期 | 2026-09-10（本机时间 +08:00） |
| mock 运行方式 | 无外部 dev server：探针以 vite 程序化启动本仓配置（mock 中间件挂 `/api/*`，`port: 0` 随机端口）或在 Node 内直接导入 mock 模块；**未触碰用户可能在跑的 5173** |
| 网络 | 探针只用进程内 `fetch` 直连自身随机端口，无外网、无模型调用 |
| 探针预算 | 每个探针独立 `timeout`（120s／400s）；实测 f2-04 35.9s、f2-05 75.3s、f2-01–03 各 <10s |

## 1. 构建证据

| 任务 | 命令 | 预期 | 实际 | 证据 |
|---|---|---|---|---|
| F2-06 / §7 第 12 步 | `npm run build`（=`tsc -b && vite build`，在 `frontend/` 执行**一次**） | 零错误 | **EXIT=0**；`tsc -b` 无输出（零错误）→ `✓ 1979 modules transformed.` → `✓ built in 3.42s`；产物 `dist/assets/index-IwyY5ZCW.js 570.22 kB (gzip 175.95 kB)`、`index-WoUCc4aE.css 269.09 kB (gzip 106.52 kB)`；仅有既有 chunk >500 kB 警告（非错误） | 本地日志 `plans/stage2-evidence-assets/f2-06-build.log`（含 vite 版本与产物行；`*.log` 不入库） |

## 2. 运行时探针证据（协议级，mock HTTP／模块级）

方法：**运行 F2-01–F2-05 既有验收探针脚本，只回填输出，未改探针代码**；无测试框架、无新依赖、无 SSE 订阅。每脚本自建 vite 实例或直接导入 mock 模块，结束自清进程。

| 探针（脚本） | 覆盖（§7 步） | 结果 | 归档输出 |
|---|---|---|---|
| `scripts/f2-01-catalog-probe.mjs` | 24 项目录身份／模式／器械与后端 003 迁移逐字段一致、无第 25 项；未受检／inactive 被可推荐谓词拒绝；计划／日程为结构化契约字段（§7 第 3 步的目录前提） | **PASS=14 / FAIL=0**（末行「全部通过」），EXIT=0 | `plans/stage2-evidence-assets/f2-01-probe-out.txt` |
| `scripts/f2-02-plan-probe.mjs` | 生成分支与候选、日期边界、器械／具体动作／模式限制过滤、fail-closed、同日重复拒绝／跨日复用放行、无起始重量、缺档案与红旗不给处方、非法纠错拒绝（§7 第 2–5、11 步） | **PASS=30 / FAIL=0**，EXIT=0 | `.../f2-02-probe-out.txt` |
| `scripts/f2-03-draft-revise-probe.mjs` | 草稿卡结构化口径与轻量纠错 rebuild／拒绝面；`DraftCard.tsx` 静态断言含校准文案、具体日程、旧日程取消预览、无 `EditablePlanDiff`（§7 第 3–5 步） | **PASS=21 / FAIL=0**，EXIT=0 | `.../f2-03-probe-out.txt` |
| `scripts/f2-04-plan-commit-probe.mjs` | 确认前隔离、首次启用 v1 + 12 日程、幂等、长期器械组合草稿与旧日程取消、已锁定不动、故障注入整份回滚、`draft_modified`／`draft_stale`→重算、丢弃拒绝、流式期间版本推进（§7 第 5–9 步） | **PASS=22 / FAIL=0**，EXIT=0 | `.../f2-04-probe-out.txt` |
| `scripts/f2-05-plan-safety-probe.mjs` | `/api/profile` 空态→启用→替换投影与刷新一致、幂等、具体动作与动作模式限制整份阻断、任何训练日说法均阻断、历史可见不伪造「部分可用」、红旗独立阻断（有／无计划）、受限动作改入草稿被拒（§7 第 1、6、7、10、11、12 步） | **PASS=28 / FAIL=0**，EXIT=0 | `.../f2-05-probe-out.txt` |

合计 **PASS=115 / FAIL=0**；无一条 `FAIL`（`grep -c '^PASS'` 计数与各脚本末行「全部通过」一致）。

### 2.1 §7 剧本逐项对照（协议级 actual == expected）

| §7 步 | 剧本要求 | 协议级证据（check 名摘录） | 结论 |
|---|---|---|---|
| 1 | 空计划检查：已建档、有「尚无计划」、无直接编辑入口 | f2-05「空态：已建档但无 plan / schedules / plan_safety」；`ProfilePage.tsx` 静态检查：全文无 `<input>/<select>/<textarea>/onChange/contentEditable`（无编辑入口），有计划／无计划两种安全复核文案 | 协议级通过；卡片渲染见 §3 UI 缺口 |
| 2 | 提出候选并生成结构化草稿 | f2-02「可从已建档／无红旗／无计划档案生成计划草稿」「候选与已拍 3.3 一致（09-14 起／一三五／10-12 复核）」「控制端点注册 noplan 种子」；f2-04「步骤 5 生成结构化计划草稿」 | 通过 |
| 3 | 处方与校准：24 项来路、器械／限制符合、组次／RIR／渐进／需要校准、无猜测重量 | f2-02「计划动作全部在已拍 24 项目录内且可推荐」「计划不含任何起始重量或猜测负荷」；f2-05「处方含身份／组次／RIR／校准（无起始重量）」；f2-03 静态断言「需要校准」「校准说明（无可信训练记录：不给起始重量）」 | 通过 |
| 4 | 日程边界：09-14 至 10-09 共 12 个应训练日，休息日与 10-12 及以后无名额 | f2-02「恰好 12 个应训练日」「只落在周一／周三／周五且全部早于复核日」「休息日与复核日当天不生成名额」「越界日程／休息日日程被拒绝」；f2-05「具体日程 12 条应为训练日且全部 scheduled」 | 通过 |
| 5 | 内联纠错 revision+1 且 Diff 更新；确认前 `/profile` 无正式计划 | f2-04「步骤 5 轻量纠错：revision+1、日程按生效范围重算、官方数据不动」「纠错后仍未写入正式数据」「步骤 5 确认前隔离」；f2-03 纠错重建与拒绝面；f2-02 非法纠错载荷拒绝 | 协议级通过；内联控件实际交互见 §3 |
| 6 | 首次启用：toast、`context_version+1`、v1 + 12 日程、刷新一致、重复确认幂等 | f2-04「步骤 6 首次确认原子启用 v1（context_version 仅 +1、12 个日程 scheduled）」「步骤 6 重复确认幂等」；f2-05「确认成功并递增 context_version」「启用后 /profile 给出计划版本／状态／日期」「刷新一致：再次查询计划／日程完全相同」「重复确认幂等」 | 协议级通过；toast 与缓存失效见 §3 |
| 7 | 替换：组合草稿展示档案补丁＋新 Diff＋旧版未来未锁定取消清单＋新日程；确认前不变 | f2-04「步骤 7 组合草稿：档案器械补丁 + v3 提议 + 12 个新日程 + 旧版未来未锁定日程取消清单（不含已锁定）」「步骤 7 确认前隔离」；f2-03 静态断言「旧版未来未锁定日程取消预览」；f2-05「旧版未来未锁定日程取消仍在投影中可见（历史不隐藏）」 | 协议级通过；同卡渲染见 §3 |
| 8 | 原子切换；故障注入后重做，任一步失败全部回滚；旧 v1 保留 | f2-04「步骤 8 故障注入：确认失败，档案、计划与历史、全部日程、草稿状态、context_version 整份回滚」「步骤 8 原子切换：档案补丁 + v3 启用 + 新日程 + 旧版未来未锁定取消，context_version 仅 +1」「步骤 8 已锁定日程不动」 | 通过 |
| 9 | 公共冲突分支：revision 冲突 `draft_modified`、推进版本→`draft_stale`→一键重算→新旧 Diff→再确认、丢弃后拒绝 | f2-04「步骤 9 revision 冲突：409 draft_modified 且正式数据不动」「基线落后：409 draft_stale」「一键重算：新草稿 revision 1、关联旧草稿、旧草稿置 stale」「重算后确认：v5 启用且 context_version 仅 +1」「丢弃后拒绝确认：409」「1.3 流式期间推进 context_version→409 draft_stale」 | 通过 |
| 10 | 限制冲突整份阻断：不输出其余未冲突处方、旧计划仍可查看、引导对话修订 | f2-05「新增限制后整份不可用且指出冲突动作」「动作模式限制命中多个动作时整份不可用并逐条列出冲突」「任一训练日说法都整份阻断且不输出任何处方」「模式冲突下指导同样整份阻断、不输出未冲突动作处方」「阻断时正式计划、日程与历史仍返回（不隐藏、不伪造「部分可用」）」「阻断文案引导从对话发起修订草稿」 | 通过 |
| 11 | 红旗仅建议线下专业评估、无计划草稿；非法纠错（限制／器械不符／同日重复）被服务端拒绝且正式数据不变 | f2-05「红旗写入档案且复核为独立阻断（无限制冲突）」「红旗指导：仅建议线下专业评估、无处方、无草稿」「红旗请求计划：不给计划草稿，正式计划保持不变」「红旗种子（无计划）：请求计划与指导均不给草稿、不给处方」；f2-02「档案含红旗症状不生成处方」「红旗档案的计划载荷也被校验拒绝」；f2-05「把受限动作改入草稿被服务端拒绝（限制动作／器械不符／同日重复同一口径）」「被拒纠错不改正式数据」 | 通过 |
| 12 | 恢复与收尾：待确认时刷新恢复草稿状态；确认后刷新恢复正式计划／日程；`npm run build` 零错误 | f2-05「刷新一致：再次查询计划／日程完全相同」「替换态刷新一致」；f2-04 重算链路中重新查询草稿状态（pending／stale／committed）恢复一致；构建 EXIT=0（本文 §1） | 协议级通过；浏览器刷新恢复见 §3 |

## 3. 未覆盖／未验证（显式缺口，不得计为通过）

### 3.1 UI 缺口（本切片未执行浏览器走查）

| 缺口 | 说明 | 需要谁补 |
|---|---|---|
| §7 第 1、2、3、4、6、7、11 步的渲染结果 | `/profile` 空计划卡与计划卡（版本／状态／日期／处方／校准／日程锁定-取消徽章）、对话页候选与结构化草稿卡、红旗回复与限制阻断文案的实际排版与可读性 | **owner 人工浏览器走查**（`npm run dev`） |
| §7 第 5 步内联纠错交互 | 日期／训练日／动作候选／组数／次数区间／RIR 控件实际改值 → `revise` → `revision+1` 与 Diff 实时更新的端到端交互（协议侧与静态断言已过，控件点击未验） | owner 人工浏览器走查 |
| §7 第 6 步 toast 与缓存失效 | 确认后 toast、`/profile` 与对话页 react-query 看板失效刷新（`context_version` 变化驱动） | owner 人工浏览器走查 |
| §7 第 12 步浏览器刷新恢复 | 待确认时按 F5 恢复草稿卡当前状态；确认后按 F5 恢复正式计划／日程（协议侧「重新查询一致」已过，浏览器刷新与客户端缓存重建未验） | owner 人工浏览器走查 |
| 无编辑入口 | 仅做了源码级静态检查（`ProfilePage.tsx` 无输入控件）；未在浏览器确认无隐藏入口或二次跳转 | owner 人工浏览器走查 |

> 说明：本机存在 `/snap/bin/chromium`，但驱动 §7 的对话—纠错—确认—刷新交互需要浏览器测试框架或等价驱动代码，属 `frontend/AGENTS.md` 明确禁止的新增范畴；单张静态截图也不构成剧本走查，故本切片**不伪造 UI 证据**，如实标记为缺口。

### 3.2 明确不验证（本阶段边界，不得标为已通过）

- 真实计划域后端事务、真实动作 `recommendable` 更新、真实 Agent 生成质量、真实数据库日程锁定（stage2 §6）。
- 复核日后的自动续期／跨周期切换（stage2 §4，保持未拍）。
- Stage 3+ 的当次安排、实际训练与打卡，Stage 5 的基于历史渐进与复盘。
- 本切片未跑 SSE `/api/events`；桌面／移动端多分辨率与无障碍检查不在本阶段范围。

### 3.3 mock-only 边界声明

本文全部 PASS 来自 **vite 内 mock 中间件与 mock 模块导入**：无真实后端进程、无真实网络、无模型调用、无真实数据库；mock 日期固定 `2026-09-10`，候选日期固定 09-14／一三五／10-12。**mock 通过不代表真实链路通过**；结项门槛（stage2 §7 完整走通）在 owner 完成 §3.1 浏览器走查前**未达成**。

## 4. 逐任务证据表（F2-01 … F2-06）

| 任务 | 平台 | 代码版本 | 步骤／命令 | 预期 | 实际 | 证据路径 |
|---|---|---|---|---|---|---|
| F2-01 契约与 mock 目录 | 模块级（Node 导入 mock） | 工作树（基线 `1ab7970`） | `node scripts/f2-01-catalog-probe.mjs` | 24 项与后端 003 一致、无第 25 项；非候选被拒；计划／日程结构化 | 一致（14/14 PASS） | `plans/stage2-evidence-assets/f2-01-probe-out.txt` |
| F2-02 计划生成与安全前置 | 模块级 | 同上 | `node scripts/f2-02-plan-probe.mjs` | 条件匹配、12 日程、无猜重量、缺档案／红旗不给处方、非法纠错拒绝 | 一致（30/30 PASS） | `.../f2-02-probe-out.txt` |
| F2-03 结构化草稿卡与轻量纠错 | 模块级 + 源码静态 | 同上 | `node scripts/f2-03-draft-revise-probe.mjs` | 纠错 rebuild、服务端派生 Diff、拒绝面；卡展示校准／日程／取消预览、无受限编辑器 | 一致（21/21 PASS）；控件交互见 §3.1 | `.../f2-03-probe-out.txt` |
| F2-04 启用、替换与原子切换 | mock HTTP（vite 程序化） | 同上 | `node scripts/f2-04-plan-commit-probe.mjs` | 原子启用／替换、幂等、回滚、stale／revision、锁定不动 | 一致（22/22 PASS，35.9s） | `.../f2-04-probe-out.txt` |
| F2-05 当前计划、日程与使用阻断 | mock HTTP（vite 程序化） | 同上 | `node scripts/f2-05-plan-safety-probe.mjs` | 投影一致与刷新一致、整份阻断、红旗独立阻断、历史可见 | 一致（28/28 PASS，75.3s） | `.../f2-05-probe-out.txt` |
| F2-06 闭环演示与证据 | 构建 + 上述探针 + （待补）浏览器走查 | 同上 | `npm run build`；5 个探针；§7 逐项对照 | 剧本全部通过、构建零错误、证据完整且标明 mock 边界 | 协议级 115/115 PASS、构建 EXIT=0；**浏览器走查未执行**（§3.1），结项门槛**部分待补** | 本文件全文 + 第 1–2 节输出归档 |

## 5. 风险与观察（交评审与 owner）

1. **结项门槛未闭合**：stage2 §9「第 7 节从无计划种子完整走通」中依赖浏览器渲染／交互的项（§3.1 五类）只能由 owner 人工走查确认；本切片不据此勾选任何完成条件，也未修改 `plans/stage2.md`。
2. **代码版本不可引用**：Stage 2 改动仍在未提交工作树（§0），无提交号可锚定；若后续编辑工作树，本文协议证据需重跑。建议评审通过后由 owner 决定提交时机。
3. **探针为脚本而非框架**：F2-01–F2-06 探针各自独立、无共享 harness，重跑需按 §6 顺序执行；`f2-03` 含源码正则断言（无编译期保证），源文件重命名会使断言静默失配（脚本会 FAIL 而非误报通过）。
4. **f2-04 故障注入为 mock 事务内注入**：证明 mock 的整份回滚语义，不代表真实后端事务原子性。
5. **mock 种子耦合**：f2-05 的「默认种子 4 locked / 14 scheduled」与 f2-04 第 7 步依赖 mock 种子结构；改种子需同步更新探针期望。

## 6. 复现方式

```bash
cd frontend
# 1) 模块级探针（无 dev server，各自独立）
node scripts/f2-01-catalog-probe.mjs   # 期望末行：全部通过（PASS=14）
node scripts/f2-02-plan-probe.mjs      # 期望末行：全部通过（PASS=30）
node scripts/f2-03-draft-revise-probe.mjs  # 期望末行：全部通过（PASS=21）
# 2) mock HTTP 探针（自建 vite 实例，随机端口，不触碰 5173）
timeout 400 node scripts/f2-04-plan-commit-probe.mjs   # 期望：PASS=22 FAIL=0（~36s）
timeout 400 node scripts/f2-05-plan-safety-probe.mjs   # 期望：PASS=28 FAIL=0（~75s）
# 3) 构建（只跑一次）
npm run build                          # 期望 EXIT=0，✓ built in ~3.4s
# 4) 浏览器走查（owner 人工，本切片未执行）
npm run dev                            # 按 plans/stage2.md 第 7 节第 1–12 步
```

归档产物（入库）：`plans/stage2-evidence.md`、`plans/stage2-evidence-assets/f2-0{1..5}-probe-out.txt`；`f2-06-build.log` 为本地取证文件（`.gitignore` 忽略 `*.log`，不入库）。
