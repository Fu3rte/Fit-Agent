# 前端 Stage 6 开发计划：真实联调与交付验收

> 状态：**§8 A–F 已于 2026-09-13 owner 拍板**；整体计划仍待 owner 一句「按此开工」后实施。拍板本身 ≠ 已开工或已验收。
> 依赖：Stage 0–5 前端 mock 闭环已结项（见 [stage5.md](stage5.md) / [stage5-evidence.md](stage5-evidence.md)）；后端 S4-01–08 离线实现（`pre-prj/stage/stage4.md`），**S4-09 前后端联调未开始**。
> 本文件细化 [business-roadmap.md](business-roadmap.md) 阶段 6「真实联调与交付验收」：Provider 配置 → 真实运行 → 逐条复验闭环 → Windows 人工验收。
> **红线**：mock 通过不得声称为真实链路通过；本计划落盘 ≠ 开工或验收。
> **契约提醒**：B 档 Provider 范围已于 2026-09-13 同步写入 `PLAN.md`、`pre-prj/design-decisions.md`（「Stage 6 真实联调 Provider 范围」）、`pre-prj/architecture/08-agent-runtime.md`（思考/容量/Stage 6 费用护栏）与 `10-deployment-credentials.md`。同步 ≠ 已实现换商或已计费。

## 1. 阶段目标

1. **切换真实链路**：前端从 mock 模式切换到真实 FastAPI 后端（静态托管 + 同源 `/api/*`），对话、草稿、确认、查询、SSE 全部走生产路径。
2. **契约收口**：按 `pre-prj/stage/stage3-handover.md` F1–F10 与 S4 冻结传输面，逐项映射或拍板；不静默保留 mock 私有端点并声称为后端已交付。
3. **Provider 配置闭环**：设置页录入／替换／删除 Key；查询只回 `has_api_key`；完整 Key 不出现在响应、日志、SSE、异常详情。
4. **逐条复验业务闭环**：建档 → 计划启用 → 打卡／安排 → 更正作废 → 复盘与后续调整／接回渐进，在真实后端 + 真实模型上重走 Stage 1–5 剧本（正常、空数据、失败、冲突、幂等、重启恢复）。
5. **交付验收形态**：Python 项目安装启动、运行时不需要 Node.js、仅回环监听；Windows 按清单人工验收；证据与缺口如实归档。

## 2. 来源依据

| 依据 | 本阶段采用的约束 |
| --- | --- |
| [business-roadmap.md](business-roadmap.md) 阶段 6 | Provider 配置 → 真实运行 → 逐条复验 → Windows 人工验收；密钥不回显、失败可理解、重启可恢复查看 |
| [PLAN.md](../../PLAN.md) 项目级验收 | 核心自动化测试 + Windows 人工验收；Linux 通过不能代替 Windows；Python 发布、回环监听、不做 exe |
| [10 部署与凭据](../../pre-prj/architecture/10-deployment-credentials.md) | FastAPI 静态托管前端产物；platformdirs 数据目录；同库 Key；`has_api_key` 投影；运行中备份 SQLite Backup API |
| [09 测试与验收](../../pre-prj/architecture/09-testing-acceptance.md) | 产品自动化与 Agent 测评互不替代；Windows 清单实测；测评 20 元预算与案例编写**默认不在本阶段**（见 §8） |
| [08 Agent 运行时](../../pre-prj/architecture/08-agent-runtime.md) | 单 Run 互斥、断线查询恢复、SSE 白名单、Harness 已拍参数、Stage 4 联调费用护栏语义（额度须另批） |
| [01 共用事务](../../pre-prj/architecture/01-shared-transaction.md) | 对话唯一变更入口；确认原子／幂等／`context_version`；stale→recalc |
| [stage3-handover](../../pre-prj/stage/stage3-handover.md) §3 F1–F10 | 前端待改正本映射清单 |
| [stage4.md S4-09](../../pre-prj/stage/stage4.md) / [S4-evidence](../../pre-prj/stage/evidence/S4-evidence.md) | 联调缺口：作废终态拦截、复盘 SSE 仅状态、`/recalc` 幂等键、复盘正文查询口径 |
| [frontend 存档](../../pre-prj/architecture-archive/frontend-presentation-decisions.md) | 五页 IA；设置页 Provider 卡；不建浏览器 E2E（B6）；TanStack Query / 原生 EventSource |
| [stage5-evidence.md](stage5-evidence.md) §6 缺口 | mock≠真实；完成率回归期；安排读回 mock 端点；F1/F5–F9 未收口 |

## 3. 当前差距（开工前事实基线）

### 3.1 前后端传输面差异（必须收口）

| 能力 | 前端 mock（现状） | 后端真实（S3/S4 冻结） | Stage 6 处理 |
| --- | --- | --- | --- |
| 提交对话请求 | `POST /api/runs` | `POST /api/sessions/{id}/requests` `{client_request_id,text}` | **前端改调真实端点** |
| SSE | `GET /api/events` | `GET /api/runs/{run_id}/events` | **前端按 Run 订阅**；复盘 Run 只有 `status` |
| 断线恢复 | `GET /api/runs/active` | `GET /api/sessions/{id}`（消息+全部 Run）；**无** `/api/runs/active` | **已拍 E1**：前端改查询会话，不新增 active 端点 |
| 会话消息 | `GET /api/sessions/{id}/messages` | 会话查询内嵌消息 | **前端合并到 session 读** |
| 统计 | `GET /api/stats` 聚合 | `/api/stats/completion` + `/api/stats/pr` | **前端聚合**（handover F7 已拍方向） |
| 复盘读 | `GET /api/review` 最新一条 | `GET /api/reviews` 列表 + `/{id}` | **前端取最新**；字段 `body_markdown` |
| 安排读回 | `GET /api/arrangements`（mock-only） | **无**对应 HTTP 端点 | **已拍 A2**：前端自行投影，不增后端端点 |
| Provider | `/api/provider` 系列 | **路由未实现**（`routes_settings.py` 空壳；仅 S0-07 存储层） | **后端补设置路由**（10.3） |
| 静态托管 | Vite dev + mock 插件 | 生产需 FastAPI 托管 `frontend` 构建产物 | **后端接静态托管**；运行时无 Node |
| 计划/档案/记录 | 聚合形状 | `/api/plan`、`/api/plan/guidance`、拆分端点 | 按 F1/F5/F6 映射 |
| 草稿 kind | mock 三类偏旧 | `profile_update`/`plan`/`training_record`/`arrangement` | F2 |
| revise/confirm | 部分形状偏旧 | revise 必带 `revision`；confirm 返回提交凭据 | F3/F4/F10 |
| recalc | 已带 `{client_request_id}`（与 S4-08 一致） | 同左 | 保持；联调验证幂等与 busy |

### 3.2 后端集成缺口（S4-09 / Stage 6 前置）

| 缺口 | 现状 | 处理 |
| --- | --- | --- |
| 作废即终态拦截 | `app/confirm.py` 仍接受向作废身份追加修订（2026-09-13 拍 A 未落地） | **后端必做**；定向回归后方可联调更正链路 |
| Provider HTTP 路由 | 未实现 | 后端按 10.3 补查询／PUT／DELETE；只回 `has_api_key` |
| 静态文件托管 | 未接线 | 后端托管前端 `dist`；SPA 回退到 `index.html`（实现细节） |
| 真实模型流式 | 离线桩已验，真实 Provider 流式未验 | 联调时实测保活、断流、`finish_reason`、思考分片 |
| 复盘 Run 与前端 | SSE 仅状态；正文经 `GET /api/reviews` | 前端按此口径；不改后端事件白名单 |

### 3.3 授权与费用边界（2026-09-13 owner 已拍 B）

- **真实联调端点（改拍）**：非 DeepSeek 官方；采用 owner 指定 **OpenAI 兼容端点**（阿里云百炼 / MaaS compatible-mode）。
  - Base URL：`https://ws-o2404joh7zvfydxz.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
  - 模型 ID（owner 示例）：`qwen3.7-flash`（执行前以 env `MODEL_NAME` 为准核实）
  - 凭据：环境变量 `MODEL_API_KEY` / `MODEL_BASE_URL` / `MODEL_NAME`（owner 已填入本地 env；**Key 不进入计划、日志、仓库、证据**）
  - 官方示例要点：OpenAI SDK + `stream=True`；思考经 `extra_body={"enable_thinking": True}`；流式 `delta.reasoning_content`（思考）与 `delta.content`（正文）分列——**隐藏推理仍不进产品页与 SSE**（沿用 08 章）
- **Stage 6 真实联调额度**：累计 **USD 50**；请求前预留费用上界，完成后按真实 usage 结算，未知 usage 按预留额保守扣账，余额足够可继续；跨重启持久账本累计，换会话/重试不重置；包含正常请求、摘要、重试与纠错；**不含** 09 章正式测评。Stage 4 的 10 美元额度**不恢复、不累加**。
- **模型窗口 / profile / 计费**：不得沿用 deepseek-flash 旧拍（窗口 1,000,000、DeepSeek 思考默认等）；联调前只读核对 `qwen3.7-flash`（或 env 实际模型）的窗口、计价与是否默认思考，再定 Harness 容量参数是否仍适用；不适用则停下列缺项等拍，不静默套用。
- **本文不授权**「已发布」签字；Windows 成品验收按 §7/F6-10 清单执行并留证。
- 09 章 Agent 正式测评（pass³、裁判校准、20 元预算）**已拍 C1：不并入本阶段**。

## 4. 明确不做

- 不建浏览器 E2E、vitest 组件测试、模型排行榜、测评后台（存档 B6、09 章、PLAN 边界）。
- 不引入 Redis／队列／多 Worker／WebSocket／账号系统／局域网访问／exe。
- 不新增公开建草稿、Agent 直接确认、第二套恢复机制、Queue/Steer。
- 不把 mock `/api/dev/*` 控制面带入生产产物；真实验收失败注入优先用可重复的业务路径或后端已拍错误码场景，不为演示新开未拍业务端点。
- 不在未拍板时发明新业务语义、新错误码或医学阈值。
- 不修改 `pre-prj/` 决策正本；冲突列出后停下等拍。

## 5. 任务拆分与逐项验收

> 执行顺序：F6-00（前置收口）→ F6-01（后端联调底座）→ F6-02（前端真实适配）→ F6-03（Provider 闭环）→ F6-04–F6-08（五条业务闭环真实复验）→ F6-09（安全与恢复）→ F6-10（Windows 与发布验收）→ F6-11（证据与结项）。
> 每项完成条件：有命令/退出码或 owner 浏览器走查记录 + 明确未覆盖项；未跑不得标 PASS。

### F6-00：前置收口与联调契约冻结

- **工作**：
  1. Owner 确认本计划范围包（§8）；
  2. 冻结「Stage 6 传输对照表」（§3.1）为联调正本：前端最终消费的路径、方法、请求体、响应投影、错误码；
  3. 列出 handover F1–F10 每项「前端已改 / 本阶段改 / 再拍」结论；
  4. 确认后端 S4-09 启动条件（作废终态拦截是否为硬前置）。
- **依赖**：owner 确认计划。
- **验收标准**：对照表无未决项；待拍事项有选项与推荐；未确认前不改生产代码。
- **验证方式**：文档 diff 评审；无代码。
- **实现状态**：**已完成**（2026-09-13，复审 PASS_WITH_NOTES）；证据：[stage6-transport-freeze.md](stage6-transport-freeze.md)。

### F6-01：后端联调底座（协作，归属 backend / S4-09）

- **工作**（前端不越界改 `backend/`；由后端 owner 接单）：
  1. 实现作废终态拦截 + 定向回归；
  2. 实现 Provider 设置 HTTP 路由（10.3）；
  3. 接线前端构建产物静态托管与 SPA 回退；
  4. **不**为安排读回新增后端端点（已拍 A2，前端投影）；
  5. 跑后端全量自动化门，记录版本与退出码。
- **依赖**：F6-00；S4-01–08 既有实现。
- **验收标准**：作废后追加更正被 `invalid_request` 拒绝；设置路由只回 `has_api_key`；`npm run build` 产物可被后端托管并打开五页；pytest 全绿（无新增 skip 掩盖联调缺口）。
- **验证方式**：后端自动化 + 回环 curl 冒烟；证据归 `pre-prj/stage/evidence/`（后端侧）。
- **实现状态**：**未完成，归后端 owner**（2026-09-14 backend 全量回滚；作废终态 / Provider 路由 / 静态托管均待后端 owner 重做；此前「已完成」作废，不得再当 F6-01 完成证据）。

### F6-02：前端真实 API 适配

- **工作**：
  1. `api.ts` / 对话与看板查询改为真实端点面（§3.1）；SSE 改为按 `run_id` 订阅；恢复改为会话查询（E1）；
  2. 统计双端点前端聚合；复盘取 `/api/reviews` 最新条；draft kind 与 ConfirmResult 凭据字段对齐 F2–F4/F10；
  3. 档案页计划/安全复核改调 `/api/plan` 与 `/api/plan/guidance`（F1）；
  4. **已拍 A2**：去掉生产路径对 `GET /api/arrangements` 的依赖，从计划/记录/草稿等已有投影推导已接受安排展示；
  5. **已拍 F3**：联调通过后删除 `src/mock/` 与 Vite mock 插件挂载；结项不得残留 mock 私有生产调用；
  6. 修正受形状变更影响的既有探针或明确退役（不放松断言冒充通过）。
- **依赖**：F6-01 可并行开发，联调以 F6-01 完成为准。
- **验收标准**：`tsc -b` 与 `npm run build` 零错误；对真实后端（无真实模型时用无 Key 或离线可测路径）查询类端点可读；确认/丢弃/修订请求体与后端 400/409 语义一致；前端无残留对 `/api/stats` 聚合、`/api/review`、`/api/runs` POST、`/api/events` 的生产调用；联调后 `src/mock/` 已删。
- **验证方式**：类型检查 + 构建 + 对真实后端的协议探针（新 `f6-02`，仅回环、无真实模型）。
- **实现状态**：**已完成**（2026-09-13，前端侧）：传输/看板/安排/mock 删除落地；`tsc`+`build` exit 0；探针不依赖 F6-01 断言通过（Provider 路由 / 静态托管缺失记 SKIP，待后端 owner 重做后再验）；已知限制：安排徽章生产恒「尚无安排」（无会话列表+无 `scheduled_session_id`）；RecordsPage 三桶 UI 生产不可达；f2–f5 探针退役。**注意**：2026-09-13 旧探针证据（15/15 PASS 含 provider/静态托管）基于已回滚的后端临时实现，**不得再当 F6-01 完成证据**；当前 [stage6-evidence-assets/f6-02-probe.txt](stage6-evidence-assets/f6-02-probe.txt) 为 2026-09-14 回滚后重跑（13 PASS + 2 SKIP）。

### F6-03：Provider 配置闭环（真实存储 + 兼容端点）

- **工作**：
  1. 设置页经真实路由录入/替换/删除 Key；刷新后 `has_api_key` 保持；
  2. 无 Key 时对话 Run 以可理解失败结束（`model_request_failed`，不发请求）；
  3. UI 任意位置不回显完整 Key；
  4. **联调取数口径**：产品运行时仍按 10.3 从同库 Provider 配置读 Key；开发/验收进程可将 owner env（`MODEL_API_KEY`/`MODEL_BASE_URL`/`MODEL_NAME`）作为**验证进程**注入方式——`.env` 有值 ≠ 进程已读到，须在启动环境核实且不打印值；
  5. 执行前最小连通冒烟（官方示例式单轮，计费入 USD 50 账）：确认端点可达、模型 ID 有效、流式可解析；失败即停并记原因。
- **依赖**：F6-01 Provider 路由；F6-02 设置页适配。
- **验收标准**：录入后查询仅 `has_api_key=true`；删除后 `false` 且跨重启一致；网络响应、浏览器控制台、后端日志抽样无 Key 明文；冒烟通过或失败原因可复现。
- **验证方式**：协议探针 `f6-03` + owner 浏览器走查 + 可选单轮冒烟；日志抽样人工核对。
- **验证证据**：探针输出 + 走查记录 + usage/费用汇总；不落 Key 值。

### F6-04：建档闭环（真实）

- **工作**：空库 → 未建档提示 → 对话收集 → 草稿 → 内联纠错 → 确认 → `/profile` 更新。
- **依赖**：F6-02。
- **验收标准**：确认前正式档案不变；缺失事实不编造；确认幂等；失败不半写；对话仍是唯一变更入口。
- **验证方式**：真实后端协议探针 `f6-04` + owner 浏览器剧本。

### F6-05：计划启用闭环（真实）

- **工作**：基于档案提出计划 → 处方与日程展示 → 确认启用 → `/profile` 当前计划与安排可查；限制/红旗冲突阻断。
- **依赖**：F6-04。
- **验收标准**：生效范围清楚；换计划同事务取消旧版未来未锁定日程；安全阻断不输出可执行处方。
- **验证方式**：探针 `f6-05` + 浏览器剧本。

### F6-06：训练、安排与更正闭环（真实）

- **工作**：查看当次安排 → 对话调整确认 → 打卡记录草稿 → 确认 → 统计刷新；记录页更正/作废 → 草稿 → 确认 → 历史与统计刷新；作废身份再更正被拒（终态）。
- **依赖**：F6-05；F6-01 作废终态。
- **验收标准**：区分原计划／接受安排／实际记录；待补全、无安排、同日多练可走通；不新增训练身份；stale→recalc→子草稿再确认；作废终态 fail-closed。
- **验证方式**：探针 `f6-06` + 浏览器剧本；覆盖失败注入（经业务错误路径或后端已有测试夹具，不新增未拍 dev 端点）。

### F6-07：复盘与后续调整闭环（真实模型）

- **工作**：显式生成复盘 → `/review` 最新条与冻结数字 → 建议不自动生效 → 后续安排/新计划草稿确认 → stale 与重新生成 → 接回／渐进（若模型路径覆盖）。
- **依赖**：F6-06；**已批 USD 50**（§3.3）。
- **验收标准**：正文数字来自冻结 basis；保存失败不落；SSE 复盘 Run 仅状态、正文经查询；无确认不改计划。
- **验证方式**：探针（无模型路径）+ 真实模型最小剧本 + 浏览器走查；费用按预留结算记账。
- **验证证据**：usage/费用汇总（无 Key）；正文质量仅作走查记录，不作 09 章测评替代。

### F6-08：渐进、接回与长会话（真实，可选包）

- **工作**：在 F6-07 稳定后，真实模型走接回三档、渐进建议、多轮纠错、压缩提示。
- **依赖**：F6-07；费用仍计入 USD 50 总账，不自动追加额度。
- **验收标准**：无记录不猜重；红旗阻断；压缩有界面提示；长会话不崩溃。
- **验证方式**：浏览器剧本 + 后端日志；**明确**：非 09 章 pass³。

### F6-09：安全、失败与重启恢复

- **工作**（对齐 roadmap 验收重点）：
  1. 密钥不回显：设置、错误、SSE、日志；
  2. 失败可理解：无 Key、超时、busy、stale、invalid、interrupted_by_restart 文案可读；
  3. 重启可恢复查看：杀进程重启后 pending/running→failed+`interrupted_by_restart`；刷新经查询恢复消息/Run/草稿/看板，不重放 SSE；
  4. 回环：非回环 Host/Origin 403；仅监听 127.0.0.1。
- **依赖**：F6-02–07 主路径可用。
- **验收标准**：清单逐项 PASS 或记缺口；不把 Linux 探针冒充 Windows。
- **验证方式**：协议探针 `f6-09` + Windows/本机人工步骤。

### F6-10：Windows 人工验收与发布形态

- **工作**：
  1. 按 9.2／10.1：Windows 已克隆仓库、同一代码版本；安装依赖、构建前端、启动后端；
  2. 清单实测：安装、启动、Provider 配置、五页只读、对话闭环抽样、重启恢复、回环限制、数据目录位置（`%LOCALAPPDATA%\Fit-Agent\app.db`）；
  3. 确认运行时不需要 Node.js（仅构建期）；
  4. 证据写入 `pre-prj/stage/evidence/S4-evidence-windows.md` 或本阶段 `stage6-evidence.md` 的 Windows 节（与后端约定一处正本，避免双份矛盾）。
- **依赖**：F6-01–09 主路径完成；owner 在 Windows 执行。
- **验收标准**：清单每项有结果与版本号；失败项如实记录；无虚假 PASS。
- **验证方式**：人工清单 + 截图/口述（与 Stage 5 口径一致：口述可接受，须写明）。

### F6-11：全量回归、证据与结项

- **工作**：
  1. 后端全量 pytest + ruff/pyright（或项目既定门）；
  2. 前端 `tsc -b` + `npm run build` + Stage 6 探针；
  3. 抽跑关键 f3–f5 探针（mock 路径）防回归；
  4. 写 `stage6-evidence.md`：结果正本、覆盖对照、拍板链、Windows 节、复现命令、缺口红线；
  5. 更新 [business-roadmap.md](business-roadmap.md) 阶段状态。
- **依赖**：F6-10。
- **验收标准**：证据分层清晰（mock / 协议真实 / 真实模型 / Windows）；未跑项明确标注；owner 结项确认前不得写「已交付」。

## 6. 验证方式与证据要求

| 层级 | 允许声称 | 不得声称 |
| --- | --- | --- |
| 前端构建 | `tsc`/`build` 通过 | 真实联调通过 |
| 协议真实（回环、无/假 Key） | 端点形状、幂等、错误码 | 模型行为正确 |
| 真实模型 | 该剧本在额度内跑通 | 09 章测评通过、长期质量 |
| Windows 人工 | 清单项实测结果 | 代替 Linux 自动化或代替测评 |

- 探针输出归档 `plans/stage6-evidence-assets/`（或与后端合并证据目录，结项时定一处）。
- 真实调用记录：模型 id、请求次数、usage 汇总、费用结算方式；**无 Key 明文**。
- 开发与验收同一 git 版本（记录 commit SHA）。

## 7. 阶段闭环演示剧本（结项门槛，真实链路）

前置：F6-01–03 完成；Windows 或开发机按 10.1 启动；空库或可重置数据目录；**额度已批**。

1. **启动与静态托管**：按文档启动 → 浏览器打开应用（非 Vite dev）→ 五页可导航。
2. **Provider**：设置页录入 Key → `has_api_key=true` → 删除/再录入；界面无明文。
3. **建档**：对话建档 → 草稿纠错 → 确认 → `/profile` 更新。
4. **计划**：生成计划 → 确认启用 → 当前计划与日程可见。
5. **打卡与安排**：查看今日 → 调整安排确认 → 打卡确认 → `/review` 统计变化。
6. **更正**：记录页发起更正 → 确认 → 统计刷新；作废 → 再更正被拒。
7. **复盘**：对话「生成复盘」→ 最新条与数字一致 → 建议不自动生效 → 后续调整草稿确认。
8. **失败与 busy**：无 Key 时 Run 可理解失败；运行中再发请求 `conversation_busy`。
9. **断线与刷新**：运行中刷新 → 查询恢复；断开 SSE 不取消 Run。
10. **重启**：杀进程重启 → 中断 Run 显示未完成原因；业务数据可查看。
11. **回环**：非本机 Host 访问 403。
12. **归档**：证据文件 + 版本 SHA + 费用汇总（无 Key）。

## 8. 已拍沿用与 2026-09-13 owner 拍板

### 已拍（直接沿用，非新拍）

对话唯一变更入口；确认原子/幂等/`context_version`；单 Run 互斥；断线查询恢复不重放；复盘仅显式；建议不自动生效；作废即终态（实现归 S4-09）；Provider 同库 Key、`has_api_key`；仅回环；Python 发布；Windows 成品验收；不建 E2E。

### 2026-09-13 owner 已拍（本阶段范围包）

| # | 问题 | 决定 | 边界与后续 |
| --- | --- | --- | --- |
| A | 安排读回 | **A2 前端自行投影** | 不增 `GET /api/arrangements`；从计划/记录/草稿已有数据推导；若投影不足须列缺项停下，不得静默改业务语义 |
| B | 真实联调 Provider 与额度 | **改拍**：OpenAI 兼容端点（阿里云百炼 compatible-mode，Base URL 见 §3.3）+ 模型示例 `qwen3.7-flash` + env `MODEL_API_KEY`/`MODEL_BASE_URL`/`MODEL_NAME`；额度 **USD 50** | 非 DeepSeek；窗口/计价/思考默认须重核，不得沿用 deepseek-flash；Key 不落文档；`PLAN.md`/设计正本未同步 |
| C | 09 章 Agent 测评 | **C1 不并入** | Stage 6 只做产品真实联调 + Windows；测评另阶段 |
| D | 发布验收清单 | **D2 不扩写** | 只执行 PLAN 已拍部分 + Windows 清单 |
| E | 断线恢复 API | **E1 前端适配 `GET /api/sessions/{id}`** | 不新增 `/api/runs/active` |
| F | mock 去留 | **F3 联调后删除 `src/mock/`** | 删插件挂载与 mock 源；探针若依赖 mock 须改真实或明确退役 |

条件性（实施中触发即停）：必须新增业务端点／依赖／与已拍语义冲突才能演示时，先列缺项停下等拍板。

### 契约文档同步（2026-09-13 已完成文档侧）

B 档改 Provider 范围已写入：`PLAN.md`（技术栈表 + 未拍索引）；`pre-prj/design-decisions.md`（3.1A/思考模式行 +「Stage 6 真实联调 Provider 范围」）；`pre-prj/architecture/08-agent-runtime.md`（思考默认、容量重核说明、Stage 6 费用护栏）；`10-deployment-credentials.md`。同步 ≠ 代码已适配或已发起计费。

## 9. 阶段完成条件

- [x] §8 A–F 经 owner 拍板（2026-09-13）
- [x] owner 明示「按 stage6.md 开工」（F6-00–02 已实施，2026-09-13）
- [x] F6-00 + F6-02 前端侧完成，传输对照表冻结且前端无 mock 私有生产调用（F3 后 mock 已删）（2026-09-13，复审 PASS_WITH_NOTES；F6-01 除外）
- [ ] F6-01 后端缺口（作废终态、Provider 路由、静态托管）完成并有后端证据（**未完成，归后端 owner**；2026-09-14 backend 全量回滚）
- [ ] F6-03–F6-07 主闭环在真实后端通过（含 `qwen3.7-flash` 或 env 实际模型；费用计入 USD 50）
- [ ] F6-09 安全/失败/重启清单通过或缺口入证据
- [ ] F6-10 Windows 清单实测完成，版本一致
- [ ] F6-11 `stage6-evidence.md` 归档；build/pytest/探针退出码齐全
- [ ] 未把 mock 或 Linux 证据写成真实/Windows 通过；未含 Key；未越权改决策正本
- [ ] owner 浏览器按 §7 剧本走查 + 结项确认

## 10. 风险与协作

| 风险 | 缓解 |
| --- | --- |
| S4-09 未开工导致前端空转 | F6-02 可先按冻结契约改客户端；联调门以 F6-01 为准 |
| 换商后 Harness 容量/思考/计价不适用 | F6-03 前只读核对模型规格；不适用即停，不沿用 deepseek-flash 参数 |
| 真实模型行为与 mock 剧本不一致 | 以领域校验与确认事务为权威；模型差记缺陷不静默改已拍语义 |
| 费用失控 | 调用前预留；未知 usage 保守扣账；USD 50 封顶 |
| Windows 与 Linux 行为差 | Windows 为成品验收平台；发现差项开缺陷不降级宣称 |
| 契约双源（contract.ts vs handover） | F6-00 冻结对照表为联调正本；冲突以 handover+后端实现为准并回写 contract.ts |
| 正本仍写 DeepSeek | 已于 2026-09-13 同步 PLAN/design-decisions/08/10；代码与实测仍未换商 |

---

**下一步**：A–F 已拍。请 owner 一句确认「按 stage6.md 开工」；是否同步改 `PLAN.md` 等正本另示。确认前不实施、不发起计费请求。
