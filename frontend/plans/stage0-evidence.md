# Stage 0 验收证据（F0-06：闭环演示与证据）

> 本文件按 `plans/stage0.md` 第 6 节格式归档 F0-01–F0-06 的证据，并整理第 8 节接入点对照表与未覆盖项。
> **本文件不声称真实链路通过。** 浏览器人工走查已由 owner 于 2026-09-09 完整执行（第 7 节剧本 1–10 步，含正常与异常分支）并确认通过（见 §6 顶部记录、§8）；mock 通过不代表真实链路通过。
> mock 通过 ≠ 真实链路通过：本阶段不接真实后端（无真实 HTTP/SSE/模型调用、无真实 Key 流程），真实接入按第 8 节对照表在对应后端阶段就绪且获授权后逐条替换。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Linux 6.6.114.1-microsoft-standard-WSL2 x86_64（WSL2），Node v24.19.0，npm 11.17.0，vite 7.3.6 |
| 代码版本（HEAD） | `16165572e8339e79b8115a72def01936b1570b26`（短号 `1616557`，`spike: 补测真实流式 usage 浮出 + 修复流式完整消费从不结算`） |
| 工作树状态 | **dirty**：证据对应工作树，不对应任何干净提交。frontend 子树 13 个已跟踪文件变更：11 改（`src/app/App.tsx`、`src/features/chat/{ChatPage,DraftCard}.tsx`、`src/features/chat/useChatEvents.ts`、`src/features/{profile,records,review}/*Page.tsx`、`src/index.css`、`src/lib/api.ts`、`src/lib/contract.ts`、`src/mock/server.ts`）+ 2 删（`AGENTS-FRONTEND.md`、`PLAN-FRONTEND.md`）；另有 2 处未跟踪（`plans/`、`src/components/ui/sidebar.tsx`）。本文件为 `plans/` 下新增未跟踪文件 |
| 证据日期 | 2026-09-09（本机时间 +08:00） |
| mock 运行方式 | `npm run dev -- --port 5199 --strictPort --host 127.0.0.1`（vite dev server，mock 中间件 `/api/*`，无真实后端） |
| 端口约定 | 5199 为本次探针专用；**5173 为用户正在运行的 dev server，全程未触碰**。注意：`vite.config.ts` 未配置 `server.port`，**`VITE_PORT` 环境变量不被 vite 读取**，端口只能经 `--port` 传入 |
| 网络 | 所有探针请求 `curl --noproxy '*'`，直连 `127.0.0.1:5199`（环境存在 `http_proxy/https_proxy`） |

## 1. 构建证据

| 任务 | 命令 | 预期 | 实际 | 证据 |
|---|---|---|---|---|
| F0-06 / 第 10 节 | `npm run build`（= `tsc -b && vite build`，在 `frontend/` 执行一次） | 零错误 | **EXIT=0**；`✓ 1978 modules transformed.` → `✓ built in 3.72s`；产物 `dist/assets/index-D_7DvCHw.js 551.39 kB (gzip 170.66 kB)`、`index-6_QnlbXD.css 266.85 kB`；仅有既有的 chunk >500 kB 提示（警告，非错误） | 本次运行日志 `/tmp/f0-06-build.log`（含 `EXIT=0`） |

## 2. 静态检查证据（F0-01 / F0-02 残留与契约边界）

全部命令在 `frontend/` 执行，只读 grep，无代码改动。

| 检查 | 命令 | 预期 | 实际 |
|---|---|---|---|
| S1 无 Last-Event-ID 补读代码路径 | `grep -rn -i 'last[-_]event[-_]id' src/` | 仅注释（声明不使用），无代码 | 4 处命中，**全部为注释**：`src/features/chat/useChatEvents.ts:7`、`src/lib/contract.ts:397`、`src/lib/api.ts:130`、`src/mock/server.ts:398`（均写明「不使用 Last-Event-ID 补读」）；无查询参数、无请求头、无 mock 事件 ID |
| S2 无事件重放路径 | `grep -rn -i 'replay\|重放\|补读\|补发' src/` | 仅注释/说明，无缓冲与重放实现 | 13 处命中，全部为注释、dev 控制端点说明文案（如 `server.ts:1015`「不补发挂起期间丢弃的事件（08 8.7 无重放）」）或「不重放通知」的前端注释；`SseHub` 无事件缓冲结构，挂起窗口内业务事件整段丢弃（`server.ts:408`） |
| S3 SseEvent 无通用 `error` 业务事件 | `sed -n '399,409p' src/lib/contract.ts`；`grep -c 'event: *"error"' src/lib/contract.ts` | 事件全集 9 项、无 `error` | 联合类型 9 个成员：`run.started` / `message.delta` / `draft.proposed` / `context.compacting` / `context.compacted` / `run.completed` / `run.cancelled` / `run.failed`（带 `error_code: ErrorCode`）/ `heartbeat`；`event: "error"` 命中 **0** |
| S4 形状只在契约 | `grep -rn 'fetch(' src/ \| grep -v 'src/lib/api.ts'`；`grep -rn '^\(export \)\?\(interface\|type\) ' src/ --include=*.ts --include=*.tsx \| grep -v 'src/lib/contract.ts'` | 网络访问与 API 形状单点 | `fetch(` 在 `src/lib/api.ts` 之外 **0** 处；`EventSource` 仅经 `api.ts:createEventSource()` 创建。非契约声明仅三类：UI 局部类型（`useChatEvents.ts` Listener/ConnectionLostReason/UseChatEventsOptions、`DraftCard.tsx` DraftCardProps、`ChatPage.tsx` ActiveRun/ClosedRun/Item、`lib/theme.ts` Theme、`components/ui/sidebar.tsx` SidebarContextProps）、mock 内部状态（`server.ts` RunState/MockState/PlanState）、dev-only 诊断形状（`server.ts` DevSuspendControl/DevRunSnapshot，注释声明不进契约） |
| S5 dev 控制端点不进契约 | `grep -c '/api/dev' src/lib/contract.ts` | 0 | **0**（`/api/dev/*` 仅存在于 `src/mock/server.ts`，`server.ts:770` 起注释声明非契约、前端不消费） |
| S6 契约对齐 08/01 | 人工对照 `src/lib/contract.ts` 注释与正本章节号 | Run 五状态 + 8.8 显示映射、`ErrorCode` 含 `draft_stale`/`draft_modified`/`conversation_busy`/`interrupted_by_restart`、`Draft.revision`/`DraftStatus.discarded`、`ConfirmRequest.revision`、`ActiveRunResponse`、`ChatMessage.evidence` 均在契约中 | 逐项存在（见 `contract.ts:22-38`、`:204-292`、`:303-343`、`:185-202`） |

## 3. 运行时探针证据（mock HTTP + SSE）

方法：**3 个一次性有界探针脚本**，`trap` 清理、总预算 90s/75s/60s、`curl -sS --noproxy '*' -m 8`、`--strictPort`，每个脚本结束时 `pkill -f "vite --port 5199"`；用户 5173 全程未触碰（收尾 `ss -ltn` 仅见 5173）。

| 探针 | 覆盖 | 结果 |
|---|---|---|
| #1 `/tmp/f0-06-probe.sh` | 重置种子、置 Key、发起 Run（pending→running）、busy 409、runs/active 快照、取消、丢弃→确认拒绝、纠错 revision+1、错误修订→`draft_modified`、正确修订→提交、重复确认→幂等回执、stale→recalc→再确认、会话草稿查询、dev 诊断、SSE 事件 | PASS=38 / FAIL=5 / TOTAL=43，elapsed 73s |
| #2 `/tmp/f0-06-probe2.sh` | 取消时机修正（等首个 delta 后再取消）、plan_adjust 的 stale→recalc 非空 diff→再确认、dev restart 注入 `interrupted_by_restart`、dev 挂起/恢复 | PASS=16 / FAIL=1 / TOTAL=17，elapsed 16s |
| #3 `/tmp/f0-06-probe3.sh` | 修正探针自身取 `run_id` 的缺陷后，重测「流式中取消 → 已流出文本保留」 | PASS=10 / FAIL=0 / TOTAL=10，elapsed 5s |

关键断言（期望 / 实际）：

| 场景 | 断言 | 期望 | 实际 | 来源 |
|---|---|---|---|---|
| 重置种子 | `POST /api/dev/reset` | `ok=true`，`drafts=0` | 一致；`context_version` 种子基线 = **3**（`src/mock/server.ts:363`，非 0） | #1 P1a–P1c，#2 日志 |
| 配置链路 | `PUT /api/provider/api-key` | `has_api_key=true`，不返回明文 | 一致 | #1 P1d |
| 发起 Run | `POST /api/runs` | 200 + `run_id`；`runs/active` 状态 `pending\|running`，含 `saved_text` | 一致（快照含 `saved_text`/`drafts`） | #1 P2a–P2d |
| busy 409 | 活跃期再发 | HTTP 409 + `error_code=conversation_busy` | 一致 | #1 P3a–P3b |
| 流式中取消 | 收到首个 `message.delta` 后 `POST /api/runs/:id/cancel` | 响应与 `runs/active` 均 `cancelled`；`saved_text` 保留且 ≥ 取消前；无草稿；无 `error_code` | 一致（保留文本示例：`已将打卡内容整理为训练记录草稿：**杠铃卧推 4 组 x 8 次 @ 80kg**…`） | #3 C1–C9 |
| 执行名额 | 取消后 `GET /api/dev/status` | `execution_slot_run_id` 释放（`null`） | 一致（探针 #1 因把 `null` 写成期望 `<absent>` 而报 FAIL，属期望写法问题） | #1 P4f |
| 丢弃→确认拒绝 | `discard` 后 `confirm` | 丢弃返回 `discarded`；确认 409 `invalid_request`（01 1.3） | 一致 | #1 P5a–P5d |
| 纠错 | `POST /api/drafts/:id/revise` | `revision` 1→2，diff 随内容更新 | 一致 | #1 P6a–P6b |
| 修订冲突 | 用旧 revision 确认 | 409 `draft_modified`（01 1.4 修订检查） | 一致 | #1 P6c–P6d |
| 提交 | 用所见 revision 确认 | `status=committed`，`newly_committed=true` | 一致 | #1 P6e–P6f |
| 幂等回执 | 重复确认同草稿 | `newly_committed=false`，返回**原** `context_version` 与 `summary` | 一致（`context_version=4`，`summary=训练记录已写入正式数据`） | #1 P6g–P6i |
| stale | 他草稿提交推进 `context_version` 后再确认 | 409 `draft_stale`（01 1.6） | 一致 | #1 P7b–P7c |
| 重算 | `POST /api/drafts/:id/recalc` | 新草稿 `revision=1`、`base_business_version=当前 context_version`、旧草稿 `stale`、返回 `draft_vs_draft_diff`；新草稿再确认成功 | 一致（plan_adjust 分支 diff 非空，见下） | #1 P7d–P7h，#2 R9–R12 |
| 草稿查询 | `GET /api/sessions/:id/drafts` | 返回 `Draft[]`，状态含 `discarded`/`committed`/`stale` | 一致 | #1 P8a–P8b |
| 失败/重启中断 | `POST /api/dev/restart` | 活跃 Run → `failed` + `interrupted_by_restart`；`runs/active` 暴露 `error_code` 且保留 `saved_text` | 一致 | #2 R13–R14 |
| 45s 转查询前置 | `POST /api/dev/events/suspend` / `resume` | 完全静默（无业务事件、无 heartbeat）；恢复后**不补发**已丢弃事件 | 一致（`business_events=dropped`，`client_effect` 文案与 08 8.7 一致） | #2 R15–R16 |
| SSE 事件全集 | `curl -N --noproxy '*' /api/events`（单客户端） | 只出现契约 9 项中的业务事件 + 命名 heartbeat；无 `error` 事件、无事件 ID | 观测计数：#1 `run.started×5, message.delta×8, draft.proposed×4, context.compacting×5, context.compacted×5, run.completed×4, run.cancelled×1, heartbeat×1`；#2 另观测 `run.failed×1` | #1/#2 SSE 日志 |

### 3.1 探针报错行逐条裁定（无产品缺陷）

| 行 | 实际 | 裁定 |
|---|---|---|
| #1 P1b `dev reset context_version` 期望 0 | 3 | 探针期望写错：种子基线为 3（`server.ts:363`） |
| #1 P4a/P4d `saved_text` 取消前后均 0 字符 | 0 | 探针时序问题：取消发生在首个 `message.delta` 之前（mock 首个 delta 约在 POST 后 1.5s：pending 300ms + 压缩 600ms + 600ms）。#3 等首个 delta 后取消，`saved_text` 保留断言通过 |
| #1 P4f 执行名额期望 `<absent>` | `null` | 探针期望写法问题：`null` 即已释放 |
| #1 P7g `draft_vs_draft_diff` 为空 | `[]` | **草稿类型差异，非缺陷**：`training_record` 重算按设计保留原 payload（用户意图），字段无变化故 diff 为空；`plan_adjust` 分支从当前计划派生新提案，diff 非空（#2 R11 实测含 `payload.title`、`payload.diff` 两行） |
| #2 R3 取消后 `runs/active` 仍 `running` | `running` | **探针自身缺陷**：`RID` 误从 HTTP 状态码流解析（得到 `<absent>`），取消请求打到不存在的 Run。#3 修正后 C4–C6 全部通过 |

## 4. 逐任务证据表（F0-01 … F0-06）

| 任务 | 平台 | 代码版本 | 步骤 / 命令 | 预期 | 实际 | 证据来源 |
|---|---|---|---|---|---|---|
| F0-01 契约 v1 | 静态（无浏览器） | HEAD `1616557` + dirty 工作树 | `npm run build`；S3/S5/S6 静态核对 | 事件全集 9 项无 `error`；RunStatus 五值；ErrorCode 含 `draft_modified`/`interrupted_by_restart`；`Draft.revision`、`discarded`、`ConfirmRequest.revision`、`runs/active`、`evidence`；`tsc -b` 零错误 | 全部满足；`tsc -b` 经 `npm run build` 通过 | 本文 §1、§2（S3/S5/S6） |
| F0-02 mock 改造 | mock 进程（HTTP/SSE，无浏览器） | 同上 | 探针 #1/#2/#3 全部场景 | 状态机 pending→running→终态；取消仅活跃态；busy 409；幂等/stale/修订/丢弃与 01 一致；无重放路径；dev 控制端点可注入异常 | 一致（§3 表） | 本文 §3；`/tmp/f0-06-probe{,2,3}.sh` 输出 |
| F0-03 对话运行状态（前端） | 代码级（**浏览器呈现未验证**） | 同上 | 静态核对契约 8.8 映射与 mock 状态供给 | 五状态均有映射；取消/失败保留已保存文本 | mock 侧已供齐（`cancelled` + `saved_text`、`failed` + `interrupted_by_restart`）；**UI 文案/「输出未完成」标记的浏览器呈现 PENDING OWNER RUN** | 本文 §3、§6 |
| F0-04 SSE 订阅治理与查询恢复（前端） | 代码级 + mock 协议级（**浏览器行为未验证**） | 同上 | 静态核对 `useChatEvents.ts`/`ChatPage.tsx`；mock 侧 `runs/active`、挂起端点 | 45s 无事件转查询、退避 1/2/4/8/15s、刷新不重放不重复拼接 | mock 侧端点与「不补发」语义已验证（R15/R16）；**45s 自动关闭、轮询退避、刷新恢复的浏览器行为 PENDING OWNER RUN** | 本文 §2（S1/S2）、§3 |
| F0-05 草稿公共流程（前端） | 代码级 + mock 协议级（**浏览器交互未验证**） | 同上 | 探针 #1 P5–P8、#2 R6–R12 | 纠错/确认/丢弃走业务接口；幂等原结果、丢弃拒绝、stale 拦截、修订冲突刷新 | 业务接口分支全部一致；**草稿卡 UI 状态渲染与 toast 行为 PENDING OWNER RUN** | 本文 §3 |
| F0-06 闭环演示与证据 | mock HTTP/SSE + 构建 | 同上 | `npm run build`；3 个有界探针；本文归档 | 剧本全步骤通过、构建零错误、证据完整 | 构建零错误；mock 协议级剧本分支通过；**浏览器剧本全流程未执行 → 未达结项门槛** | 本文全文 |

## 5. 接入点对照表

`plans/stage0.md` 第 8 节对照表**保持原样、本阶段未变**，本阶段新增的 `/api/dev/*` 为 dev-only 控制端点（不进契约、不参与真实接入映射）。要点复述（正本见 stage0 §8）：

| mock 契约端点 | 未来真实来源 | 可接入时机 |
|---|---|---|
| `GET/PUT/DELETE provider`、`api-key` | 10.3 安全投影 + `routes_settings` | 后端 HTTP 层就绪且获授权后 |
| `GET profile / records / stats / review` | `domain/` 业务域 + `routes_readonly` | 后端 Stage 1–3 后 |
| `sessions`、`messages`、`sessions/:id/drafts` | conversations/messages 表 + `routes_chat` | Stage 4 后 |
| `POST /api/runs`、`cancel`、`GET /api/runs/active`、SSE `/api/events` | `runtime/ run_service/run_task/events` | Stage 4 后 |
| `drafts` 纠错 / confirm / recalc / discard | `app/` drafts、confirm + `routes_drafts` | Stage 2 后 |

接入规则不变：后端对应接口就绪且获授权后逐条闭环替换，每次替换只改 `src/lib/api.ts` 与 mock 注入点，形状以契约 v1 为准。**本阶段未做任何真实接入。**

## 6. 第 7 节剧本步骤与走查记录（原 PENDING OWNER RUN）

> **走查结果（2026-09-09，owner 浏览器人工执行）：1–10 步全部通过**，含正常与异常分支（取消保留文本、重启中断注入、45s 挂起转查询、stale/重算、draft_modified、丢弃拒绝）。以下步骤清单保留作为执行记录；截图/录屏由 owner 留存。

以下 10 步原计划由 owner 在 mock 上按下述最小步骤走查；前置：`cd frontend && npm run dev`（默认 5173），浏览器打开该地址，先执行 `curl --noproxy '*' -X POST http://127.0.0.1:5173/api/dev/reset -H 'Content-Type: application/json' -d '{}'` 重置种子。

1. **启动与配置链路**：五页导航可达 → 侧栏会话可见 → 明暗主题切换正常 → 未配置 Key 时对话页显示配置指引空态 → 设置页录入任意占位 Key（仅置 `has_api_key`）→ 返回对话页可输入。
2. **发起与流式**：发送「今天卧推 80kg 4组x8 打卡」→ 乐观气泡 → 处理中 → 文本流式 → 完成后完整消息落列表 + 草稿卡出现。
3. **取消**：再次发送同一类消息 → 等首个字出现后点「停止」→ 状态显示「已取消」→ 已流出文本保留并标「输出未完成」→ 全局可再次发送（若立刻 409 busy，等「处理中」消失再试）。
4. **失败与中断**：发送消息后在流式中执行 `curl --noproxy '*' -X POST http://127.0.0.1:5173/api/dev/restart -d '{}' -H 'Content-Type: application/json'` → 页面显示「未完成」+ 服务重启中断文案，保留已保存内容与草稿 → 手动重试生成新 Run。
5. **断线恢复**：流式中刷新页面 → 出现「连接中断，正在恢复」→ 查询活跃 Run 转轮询 → 恢复已保存部分（未完成标记）→ 完成后消息完整、草稿卡状态按 `GET /api/sessions/:id/drafts` 恢复；核对无重复拼接、无历史通知重放、无重跑。
6. **45s 转查询**：`curl --noproxy '*' -X POST http://127.0.0.1:5173/api/dev/events/suspend -H 'Content-Type: application/json' -d '{"seconds":50,"heartbeat":false}'` → 等待 ≥45s 无事件 → EventSource 自动关闭转 `GET /api/runs/active` 轮询至终态；随后 `curl --noproxy '*' -X POST .../api/dev/events/resume -d '{}'`。
7. **确认与幂等**：草稿卡点确认 → toast + 看板数据刷新 → 再次确认同草稿 → 返回原结果（不重复写入）。
8. **stale 与重算**：建议用**计划类草稿**（发送「最近很累，帮我调整计划」）→ 确认草稿 A → 用另一草稿确认推进 `context_version`（或经控制端点）→ 再确认 A → 409 `draft_stale` → 一键重算 → 新草稿 + 新旧 Diff（计划类非空）→ 再次确认生效。（注：训练记录类草稿重算按设计保留原 payload，Diff 为空属预期，见 §3.1）
9. **修订与丢弃**：草稿卡内联纠错字段 → 纠错接口 `revision+1`、Diff 更新；用旧修订确认制造 `draft_modified` → 提示并刷新草稿；丢弃 pending 草稿 → 已丢弃态 → 再确认被拒。
10. **收尾**：`npm run build` 零错误；截图/日志按第 6 节格式补入本文件或单独归档。

## 7. 风险与观察（交评审）

1. **浏览器呈现与交互验证**：~~结项门槛未达~~ → 已于 2026-09-09 由 owner 走查通过（§6）。
2. **训练记录类草稿重算的 Diff 为空**（§3.1）：`training_record` 重算保留原 payload（用户意图），`draft_vs_draft_diff=[]`；若走查第 8 步用训练记录草稿，将看不到新旧 Diff 行。建议走查用计划类草稿，或由主会话裁定是否需要在 UI 上对空 Diff 给出说明。
3. **首个 delta 延迟约 1.5s**（pending 300ms + 压缩 600ms + 600ms；会话消息 ≥5 条时触发压缩）：人工走查第 3 步需等出现文字后再点「停止」，否则已保存文本为空（语义正确：未流出即无内容可保留）。
4. **取消后执行名额最多延迟约 450ms 释放**（08 8.3：剧本实际退出后释放）：取消后立即再发送可能收到 409 busy，属预期。
5. **证据对应 dirty 工作树**（HEAD `1616557`，frontend 子树 11 改 / 2 删 / 2 未跟踪，共 13 个已跟踪变更）：如需可复现，应先落定提交；本文件本身为新增未跟踪文件。
6. 探针脚本已归档至 `plans/stage0-evidence-assets/`（`f0-06-probe{,2,3}.sh` 及输出/SSE 日志/build 日志）；关键输出已内联到本文件。仓库未新增依赖、未改源码。

## 8. 复验后修复记录（评审通过后、走查前）

| 修复 | 依据 | 实施与验收 |
|---|---|---|
| 恢复轮询退避对齐正本字面 1/2/4/8/15s（原实现首败 2s 起档） | stage0 F0-04 验收标准字面 + reviewer P2 | 主会话直接修复（ChatPage.tsx 一行 + 注释）；`tsc -b`/build 通过；走查第 6 步实测确认 |
| 草稿卡有未提交纠错时禁用「确认采纳」并提示先提交纠错 | owner 拍板方案 A（未提交编辑时确认会采纳旧内容，UX 陷阱） | worker 实施（DraftCard.tsx）、主会话直接验收；走查第 7/9 步实测确认 |

两项均为 mock 层呈现/交互修复，不改变契约形状与业务语义。
