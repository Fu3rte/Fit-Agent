# Stage 1 验收证据（F1-05：闭环演示与证据）

> 本文件按 `frontend/plans/stage1.md` 第 6 节格式归档 F1-05 的证据，沿用 `plans/stage0-evidence.md` 的表格口径。
> **证据分层声明：**
> 1. **协议级（mock HTTP）**：本文件第 1–2 节为实际执行的命令与结果，可复现。
> 2. **浏览器 UI / 人工走查**：**未执行**。第 4 节列出的渲染、内联控件、刷新恢复、toast、45s 转查询等均为 **PENDING OWNER RUN**，本文件不声称通过。
> 3. mock 通过 ≠ 真实链路通过：本阶段不接真实后端（无真实 HTTP/SSE/模型调用），未验证项见第 4 节末尾。
> 4. 本次**未**勾选 `plans/stage1.md` 第 9 节任何完成条件、**未**改任何计划/决策正本；结项门槛（人工走查）仍待 owner 执行。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Linux 6.6.114.1-microsoft-standard-WSL2 x86_64（WSL2），Node v24.19.0，npm 11.17.0，vite 7.3.6 |
| 代码版本（HEAD） | `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e`（短号 `dd168bb`，`docs: Stage 0 Windows 人工验收证据…`） |
| 工作树状态 | **dirty**：证据对应工作树，不对应任何干净提交。已跟踪变更 8 个：`frontend/plans/stage1.md`、`frontend/src/features/chat/{ChatPage,DraftCard}.tsx`、`frontend/src/features/profile/ProfilePage.tsx`、`frontend/src/lib/contract.ts`、`frontend/src/mock/server.ts`、`pre-prj/stage/evidence/S0-08-windows-2026-09-09.md`、`pre-prj/stage/stage0.md`；未跟踪 3 个：`frontend/src/lib/profile.ts`、`pre-prj/stage/evidence/S1-01-baseline-linux-2026-09-09.md`、`pre-prj/stage/stage1.md`。本文件与 `plans/stage1-evidence-assets/` 为本次新增未跟踪内容；F1-05 未修改任何 `src/` 代码 |
| 证据日期 | 2026-09-09（本机时间 +08:00） |
| mock 运行方式 | `./node_modules/.bin/vite --port 5199 --strictPort --host 127.0.0.1`（vite dev server，mock 中间件 `/api/*`，无真实后端） |
| 端口约定 | 5199 为本次探针专用；**5173 为用户正在运行的 dev server（PID 298452），全程未触碰**；探针收尾只 `kill` 本次启动的精确 PID，未使用 `pkill` |
| 网络 | 所有探针请求 `curl -sS --noproxy '*' -m 8`，直连 `127.0.0.1:5199`（环境存在 `http_proxy/https_proxy`） |
| 探针预算 | 硬上限 180s（`DEADLINE`），`trap cleanup EXIT INT TERM`；本次实际 elapsed **28s**（主探针）+ **73s**（缺口探针） |

> **2026-09-09 全量复验变更**：复验发现并修复 1 处缺陷——`frontend/src/mock/server.ts` 建档状态在收到「清单外症状」时无条件清空 `physical_state`，导致同会话**先报告的红旗被后到的清单外症状抹掉**（违反 F1-02 / §7 第 4 步「症状进入待生成档案内容」）。修复为「已记录红旗不得被清空」的单行守卫，详见 §2.2。另按 pi-lens 阻断项把 4 处 dev 控制端点的未捕获 `JSON.parse` 收敛为 `readJsonBody` 辅助函数（非法 JSON → 空对象，行为对合法请求无变化）；除此之外未改任何 `src/` 代码。

## 1. 构建证据

| 任务 | 命令 | 预期 | 实际 | 证据 |
|---|---|---|---|---|
| F1-05 / 第 11 节 | `npm run build`（= `tsc -b && vite build`，在 `frontend/` 执行**一次**；修复后复跑） | 零错误 | **EXIT=0**；`✓ 1979 modules transformed.` → `✓ built in 6.36s`；产物 `dist/assets/index-C7MWZtAw.js 561.22 kB (gzip 173.20 kB)`、`index-DgrU7BXM.css 268.28 kB (gzip 106.42 kB)`；仅有既有的 chunk >500 kB 警告（非错误） | 本地日志 `plans/stage1-evidence-assets/f1-05-build.log`（含 `EXIT=0`；`*.log` 按 .gitignore 与 Stage 0 惯例**不入库**）；本次运行 `/tmp/verify-build3.log` |

## 2. 运行时探针证据（协议级，mock HTTP）

方法：**单个有界探针脚本** `plans/stage1-evidence-assets/f1-05-probe.sh`（= 本次 `/tmp/f1-05-probe.sh`），无 SSE 订阅、无测试框架、无新依赖；通过 `POST /api/dev/reset {"seed":"empty"}` 从全新空种子开始；每轮 Run 以轮询 `GET /api/runs/active`（终态）＋ `GET /api/dev/status.execution_slot_run_id`（名额释放）确认完成后再发下一条，避免 `conversation_busy`。

| 探针 | 覆盖 | 结果 |
|---|---|---|
| `f1-05-probe.sh` | 空种子重置、Key/会话、多轮缺失追问、红旗分支、结构化档案草稿、确认前不变、纠错 revision+1+派生 Diff、确认写入、幂等、丢弃→拒绝、查询恢复、stale→重算→确认 | **PASS=110 / FAIL=0 / TOTAL=110，elapsed 28s**；日志 `plans/stage1-evidence-assets/f1-05-probe-out.txt` |
| `f1-05-gap-probe.sh`（本次新增，复验缺口） | 红旗→清单外症状跨轮（缺陷回归）、红旗→直接否认、同轮红旗+清单外、清单外→红旗、打卡请求、八项事实逐项追问顺序、否定式红旗、两种限制粒度、器械未知 | **PASS=30 / FAIL=0 / TOTAL=30，elapsed 73s**；日志 `plans/stage1-evidence-assets/f1-05-gap-probe-out.txt`；修复前同脚本 **PASS=29 / FAIL=1**（`f1-05-gap-probe-prefix-fail.txt`） |

逐项要求对照（全部 actual == expected；check ID 见归档日志）：

| 要求覆盖 | check ID | 关键预期 / 实际 |
|---|---|---|
| 空重置 | A1–A10 | `POST /api/dev/reset {"seed":"empty"}` → `ok=true, seed=empty, has_profile=false, drafts=0, context_version=0`；`GET /api/profile` → `profile=null, restrictions=[], context_version=0`；`/api/records` 空、`/api/sessions` 空（裸数组长度 0） |
| Key 设置与会话 | B1–B3 | `PUT /api/provider/api-key` → `has_api_key=true`（不返回明文）；`POST /api/sessions` 返回 id；会话数 1 |
| 多轮缺失追问 | T1a–T1g | 仅给「我想增肌」→ Run `completed`、草稿数 0、`profile` 仍 `null`；回复追问缺失事实（含「训练经验」），不含「生成 1 份档案草稿」，也不出现编造默认值「初级」 |
| 明确红旗：无训练建议 | T2a–T2h | 回复含「线下」「评估」「红旗症状」（记入待生成档案内容）；不含 `组数/RIR/RPE/计划调整/建议你` 任一训练处方标记；草稿数 0、`dev/status.drafts=0`、`has_profile=false` |
| 一份结构化档案草稿 | T3a–T3r | 补齐六类事实+体重+具体动作限制 → 恰 1 份 `profile_update`，`pending`/`revision=1`/`base_business_version=0`；payload 目标/经验/频率 3/时长 60/体重 75/器械含杠铃、红旗 `胸部异常不适` 入 payload、限制 1 条 `specific_action`；服务端派生 diff 非空且含「档案 · 体重」「档案 · 当前有效限制」 |
| 确认前档案不变 | F1–F3 | `GET /api/profile` 仍 `profile=null`、`restrictions=[]`、`context_version=0` |
| 纠错 revision+1 + 派生 Diff | R1–R8 | `revise` → `revision=2`、仍 `pending`；Diff 重派生含「档案 · 每周频率」「4 次」「深蹲」；payload 限制 2 条；纠错不动正式数据（`has_profile=false`、`context_version=0`） |
| 确认一次：档案+限制+版本 | C1–C11 | `newly_committed=true`、`status=committed`、`context_version=1`、`summary=档案与动作限制已写入正式数据`；`/api/profile` 写入频率 4/体重 75/器械 3/红旗保留/限制 2 条/`context_version=1` |
| 重复确认幂等 | I1–I4 | `newly_committed=false`，返回**原** `context_version=1` 与原 `summary`；`dev/status.context_version` 仍 1（不重复写入） |
| 丢弃→确认被拒 | TD1、DC0–DC3 | 新 pending 草稿 → `discard` 返回 `discarded`；再确认 HTTP **409** + `error_code=invalid_request`；`context_version` 仍 3 |
| 查询恢复 | Q1–Q6 | `GET /api/sessions/:id/drafts` 返回 4 份，含 `committed`/`stale`/`discarded`；`GET /api/runs/active` 暴露最近 Run 的草稿当前状态；`GET .../messages` 可恢复 |
| stale→重算→确认 | TB1–TB3、SC1–SC5、ST1–ST4、TS1–TS3、RC1–RC6、CB1–CB5 | 草稿 B（base=1）建好后由第二会话确认推进 `context_version=2` → 确认 B 得 **409 `draft_stale`** 且版本仍 2、已提交草稿数不变；B 仍 pending 时再发事实只提示「过期」不生成竞争草稿；`recalc` → 新草稿 `revision=1`/`base_business_version=2`、旧草稿 `stale`、`draft_vs_draft_diff` 非空（含「档案 · 每周频率」）、新草稿 diff 对当前正式档案重派生非空；确认新草稿 → `newly_committed=true`、`context_version=3`、档案频率 5/体重 75/限制 2 条 |

### 2.1 探针自身缺陷修正记录（无产品代码改动）

首次试跑（未归档）暴露的是**探针脚本**两处缺陷，均已在记录用版本中修正，未改任何 `src/`/mock 源码：

| 现象 | 实际原因 | 修正 |
|---|---|---|
| 全部 `revise` 断言失败（响应无 `draft`） | 纠错请求体形状错：接口契约为 `{payload}`，探针把 payload 放在顶层 → 400「缺少 payload」 | 改为 `{"payload":…}` |
| `sessions` 计数断言 `<absent>` | `GET /api/sessions` 返回**裸数组**（非 `{sessions:…}`），探针按对象取键 | 改为取数组 `length` |

修正后同一次脚本重跑即 **110/110 PASS**；未出现需要停手的实现失败。

### 2.2 全量复验发现的缺陷与修复（有产品代码改动）

| 项 | 内容 |
|---|---|
| 现象 | 同会话「先报告明确红旗、后报告清单外症状」时，已记录的红旗事实被清空；补齐事实后档案草稿 `physical_state.red_flags = []`（红旗丢失） |
| 复现（空种子，单会话） | ①「我最近胸部异常不适」→ 正确给线下评估、无草稿；②「肩部偶尔发酸」→ 澄清、不判定安全；③补齐其余事实 → 草稿红旗为空 |
| 根因 | `frontend/src/mock/server.ts` `onboardingTurn` 的清单外症状分支无条件 `ob.physical_state = undefined`（意图「未知不等于无」），连带清空已记录红旗，并使 `physical_none` 的「不覆盖已明确红旗」守卫失效 |
| 影响 | 违反 `plans/stage1.md` §5 F1-02 / §7 第 4 步「红旗症状记入档案草稿」；与 02 2.3 冲突 |
| 修复 | 仅当 `(ob.physical_state?.red_flags.length ?? 0) === 0` 时才置 `undefined`；已记录红旗保留，语义不变（未判定安全的症状仍不写成「无」） |
| 证据 | 修复前：`f1-05-gap-probe-prefix-fail.txt`（G1e FAIL，29/30）；修复后：`f1-05-gap-probe-out.txt`（G1e PASS，30/30）。正对照 G2a（红旗→直接否认）、G8a（同轮红旗+清单外）、G9a（清单外→红旗）修复前后均 PASS，证明缺陷仅在此跨轮路径 |
| 回归检查 | `f1-05-gap-probe.sh` 的 G1e 即为该缺陷的回归断言；`f1-05-probe.sh` 110/110 与 `npm run build` EXIT=0 在修复后复跑通过 |
| 附带修复（pi-lens 阻断项） | 4 处 dev 控制端点的未捕获 `JSON.parse` 收敛为 `readJsonBody<T>` 辅助（try/catch，非法/空 JSON → `{}`）；各端点仍自行校验字段，合法请求行为不变（探针 A1–A10、TD/DC、TC 等 dev 端点断言修复后仍全过） |

> 本缺陷为 §5 旧第 3 条「代码阅读观察」所述路径的实测确认；原观察已由本表取代。

## 3. 协议级证据与 owner 浏览器 UI 证据（明确分离）

### 3.1 本文件已证（协议级）

- 第 1 节构建；第 2 节 12 项要求的 HTTP 断言（空种子、Key/会话、缺失追问、红旗无建议、结构化草稿、确认前不变、纠错 revision+1、确认写入、幂等、丢弃拒绝、查询恢复、stale/重算/确认）。
- 这些是 **mock 进程**的行为，**不代表**浏览器渲染、交互与 45s 客户端逻辑。

### 3.2 PENDING OWNER RUN（未执行，不得标为通过）

| # | 待 owner 在 mock 上人工验证 | 依据 |
|---|---|---|
| U1 | `/profile` 未建档引导卡渲染 +「前往对话开始建档」跳转；对话页未建档引导文案渲染 | stage1 §5 F1-04、§7 第 1 步 |
| U2 | 设置页录入 Key 的 UI 流程 → 回对话页可输入（协议侧 B1 已过，UI 未验） | §7 第 2 步 |
| U3 | 档案草稿卡六类字段/体重/限制/红旗渲染、Diff 呈现、必含元素齐全 | §5 F1-03、§7 第 5 步 |
| U4 | 草稿卡内联纠错控件实际交互（频率选择、体重数字、器械/限制列表增删）→ `revision+1`、Diff 实时更新 | §5 F1-03、§7 第 6 步 |
| U5 | 确认采纳 toast + 看板失效刷新；`/profile` 档案卡 + 限制卡红色「暂禁」徽章 +「尚无计划」占位 | §7 第 7 步 |
| U6 | 草稿待确认时刷新页面 → 草稿卡按查询恢复；确认后刷新 → `committed` 态与档案页一致 | §7 第 9 步 |
| U7 | 45s 无事件 → EventSource 自动关闭转 `GET /api/runs/active` 轮询（浏览器行为） | stage0 F0-04、§7 第 9 步 |
| U8 | 断线/刷新恢复、无重复拼接、无通知重放（Stage 0 遗留项的 Stage 1 复验） | stage0 §7 第 5 步 |

### 3.3 本阶段明确不验证（不得标为已通过）

- 真实档案域后端、真实模型调用；红旗对计划/指导/接回的实际阻断效果；限制与计划的冲突复核（stage1 §6、§3 阶段交界）。

## 4. 逐任务证据表（F1-01 … F1-05）

| 任务 | 平台 | 代码版本 | 步骤 / 命令 | 预期 | 实际 | 证据来源 |
|---|---|---|---|---|---|---|
| F1-01 契约微调与空种子 | 静态 + mock 进程 | HEAD `dd168bb` + dirty 工作树 | `npm run build`；探针 A1–A10 | `tsc -b` 零错误；空种子五页数据源可查询、未建档态为 `profile=null`、`context_version=0` | 一致（构建 EXIT=0；空种子断言全过） | 本文 §1、§2 |
| F1-02 建档对话剧本（mock） | mock 进程（HTTP，无浏览器） | 同上 + 红旗保留修复 | 探针 T1–T3、TB、TS；缺口探针 G1–G9 | 缺失逐项追问不编造；红旗给线下专业评估、无训练建议、症状入草稿；齐备只出 1 份结构化草稿；同基线不并存竞争草稿；跨轮红旗不被清单外症状清空 | 一致（T1/T2/T3/TS 全过；G1e 回归修复后 PASS，缺口探针 30/30） | 本文 §2、§2.2 |
| F1-03 档案草稿卡与确认 | mock 协议级（**UI 未验**） | 同上 | 探针 F、R、C、I、ST、RC、CB、DC | 确认前正式档案不变；纠错 revision+1 且 Diff 重派生；确认写入档案+限制、`context_version+1`；幂等原结果；stale 拦截、重算新草稿、丢弃拒绝 | 协议分支一致；**草稿卡渲染/内联控件/toast 为 U3–U5 PENDING OWNER RUN** | 本文 §2、§3.2 |
| F1-04 未建档空态与档案页更新 | 代码级（**浏览器未验**） | 同上 | 探针 A6–A8、C5–C11 | 未建档态可查询；建档后 `/api/profile` 返回档案+限制 | 数据源一致；**/profile 与对话页渲染、跳转、红色徽章为 U1/U5 PENDING OWNER RUN** | 本文 §2、§3.2 |
| F1-05 闭环演示与证据 | 构建 + mock HTTP 探针 | 同上 | `npm run build`；`f1-05-probe.sh` | 第 7 节剧本协议级分支可复现、构建零错误、证据完整 | 构建 EXIT=0；协议级 110/110 PASS；**浏览器剧本未执行 → 结项门槛未达** | 本文全文 |

## 5. 风险与观察（交评审）

1. **结项门槛未达**：`plans/stage1.md` §9「第 7 节演示剧本从空种子完整走通并留证据」需 owner 浏览器人工执行（§3.2 U1–U8）；本文件仅覆盖协议级。
2. **证据对应 dirty 工作树**（HEAD `dd168bb`，8 改 + 3 未跟踪，另加本文件与 `plans/stage1-evidence-assets/`）：如需复现应先落定提交。
3. **已确认并已修复**：原「清单外症状清空红旗」观察经缺口探针实测为真（G1e FAIL）；已按「已记录红旗不得被清空」单行守卫修复并回归通过（§2.2）。修复后 `f1-05-gap-probe.sh` 30/30、`f1-05-probe.sh` 110/110、`npm run build` EXIT=0。
4. **探针为协议级、无 SSE**：本轮不订阅 `/api/events`（F1-05 任务清单未要求）；SSE 事件语义的 Stage 0 证据仍然有效，本阶段未重复验证。
5. **`draft_vs_draft_diff` 依赖会话最新事实**：重算新旧 Diff 非空的前提是「重算前该会话事实又更新过」（本探针用「每周练5次」制造），否则档案重算可能产生相同载荷、Diff 为空——与 stage0 §3.1 记录同源，属设计口径而非缺陷。
6. 仓库未新增依赖、未建测试框架、未改 `../backend/`、`../spike/`、任何决策正本与 `worker-timeout-review.md`。

## 6. 复现方式

```bash
cd frontend
# 1) 构建（记录用只跑一次）
npm run build            # 期望 EXIT=0
# 2) 有界探针（专用 5199；只杀自己启动的 PID；180s 硬上限；不触碰 5173）
timeout 220 bash plans/stage1-evidence-assets/f1-05-probe.sh
# 期望末行：== RESULT: PASS=110 FAIL=0 TOTAL=110 | elapsed=~28s ==
# 3) 缺口/回归探针（红旗跨轮保留、追问顺序、打卡、粒度等）
timeout 220 bash plans/stage1-evidence-assets/f1-05-gap-probe.sh
# 期望末行：== GAP RESULT: PASS=30 FAIL=0 TOTAL=30 | elapsed=~73s ==
```

产物（入库）：`plans/stage1-evidence-assets/{f1-05-probe.sh,f1-05-probe-out.txt,f1-05-gap-probe.sh,f1-05-gap-probe-out.txt,f1-05-gap-probe-prefix-fail.txt}`；`f1-05-build.log` 为本地取证文件（`*.log` 被 .gitignore 忽略，不入库，与 Stage 0 一致）。
