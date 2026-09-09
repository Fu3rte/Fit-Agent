# Fit-Agent

产品一句话：本地自部署、单用户的健身计划、打卡与复盘 Web Agent。

> 本文件是项目契约正本：产品范围、架构形状、技术栈、系统边界、项目级验收标准、未拍决策唯一索引、当前授权范围、技术基座、粗略阶段路线。
> 设计级已拍决策正本见 `pre-prj/design-decisions.md` 及 `pre-prj/architecture/01–10`；`pre-prj/architecture-decisions.md`（v1）已于 2026-09-08 冻结为只读历史 ADR，仅用于溯源。冲突时项目级以本文件为准，设计级以 design-decisions.md 为准。
> 本文件于 2026-09-08 重写收窄为项目契约，此前含有的设计级决策已迁出（正本见 architecture/ 各章），收窄前原文见 git 历史。

## 产品范围

- 产品一句话：见文首。
- 产品需求正本：`pre-prj/PRD.md`（只读引用，本文件不搬运 PRD 内容）；项目背景与求职目标见 PRD 第 1 节。
- 首版交付范围：P0——对话为唯一业务变更发起入口，业务页面为纯只读看板；草稿工具与卡片、变更 Diff、关键字段内联纠错、幂等确认提交、context_version 版本拦截、一键重算；P1 及其他后期内容不在交付范围（见「未拍决策唯一索引」）。

## 架构形状

- 进程内异步单体：本地单用户场景，不引入跨进程协调。
- 系统拓扑：Browser → 单个 Python 进程 → 模型 HTTP 端点；本地和云端模型均在 Agent 进程之外。
- ASGI 服务为单 Uvicorn Worker；同步阻塞 SDK 必须经 `asyncio.to_thread()` 执行，禁止阻塞事件循环。
- 业务模块划分为五项业务职责（档案与安全限制、动作目录、计划与训练指导、训练记录与更正、统计与复盘数据）加共用应用层草稿确认流程；业务规则独立于 Agent 框架，应用层负责流程与事务编排。
- Agent 基座：PydanticAI（2026-09-06 定案，spike 三缺口实测通过）。
- 全局硬约束清单（含七条核心不变量）正本：`pre-prj/design-decisions.md`「架构不变量（全局硬约束）」节。

## 技术栈

| 层 | 选型 | 备注 |
|---|---|---|
| 后端 / Agent 语言 | Python | 前端为 TypeScript |
| Agent 基座 | PydanticAI | LangGraph 对照已取消（2026-09-06） |
| Web 框架 | FastAPI + Uvicorn（单 Worker） | 原生异步、请求校验、ASGI 支持 |
| 流式通信 | SSE（FastAPI/Starlette `StreamingResponse`） | 不引入 WebSocket 和额外 SSE 库 |
| 存储 | SQLite + `aiosqlite` + 手写 SQL | 不引入 ORM；编号迁移 + `PRAGMA user_version`，不用 Alembic |
| 前端 | React + TypeScript + Tailwind CSS + shadcn/ui | 轻量，组件按需添加；其他库逐项决定 |
| 前端构建 | Vite | 开发用 Vite 开发服务器；生产构建产物由 FastAPI 静态托管，运行时不需要 Node.js |
| 数据目录 | `platformdirs` | Windows 数据库路径 `%LOCALAPPDATA%\Fit-Agent\app.db` |
| 模型 Provider | 当前仅 DeepSeek 官方兼容端点完成 spike 验证 | Anthropic（含 cache_control/usage）与本地 Qwen 验证(取消) |

选型溯源：v1 冻结 ADR「已确认决策」表；Agent 基座定案证据见 `pre-prj/architecture-archive/spikes/pydantic-ai-spike.md`。

## 系统边界（明确不做什么）

- 不引入 Redis、Celery、独立 Agent Worker 或其他外部任务队列；任务在单进程内异步执行。
- 全局只执行一个 Agent Run，同一时刻只允许一个会话发起对话；首版不支持 Queue 或 Steer。
- 服务重启后不恢复未完成任务、不自动重试（遗留 `pending`/`running` 统一改 `failed`，错误码 `interrupted_by_restart`）。
- SSE 只负责实时展示；断线或刷新后通过业务接口查询 SQLite 当前状态，不重放事件。
- Agent 只能提出业务变更草稿；正式业务事实必须由用户点击确认按钮、经业务接口校验后提交，自然语言同意不直接生效；Agent 不得绕过确认直接修改正式业务事实。
- 仅本机部署、只监听回环地址，不提供局域网/公网访问，不建设账号系统。
- 不制作 exe 或桌面程序；以 Python 项目形式发布。
- 不建设草稿/编辑双入口、不建完整计划编辑器；不新增记忆系统或通用组合草稿系统（memory 初版设计回填阶段再拍板）。
- 不建设浏览器端到端自动化、模型排行榜、测评后台。
- Token/Cache 指标只留后台，不向产品页面展示。
- web_search 未拍板，现行方案不依赖或授权搜索能力。

## 项目级验收标准

- 首版成品验收 = 核心自动化测试 + Windows 人工验收：自动化覆盖领域校验、事务、草稿过期与幂等，同一套测试可在 Linux 与 Windows 运行；Windows 按清单实测安装、启动、业务流程与重启恢复；开发与验收对应同一代码版本，Linux 检查通过不能代替 Windows 实测证据。
- Agent 行为测评与产品自动化测试互相独立、互不替代：pass³ 主指标至少 16/18（88.9%）案例通过；确定性硬约束全部通过、安全违规零容忍，独立于该门槛；裁判留出集 8/8 通过方可正式使用。
- 发布验收：按 Python 项目安装启动、运行时不需要 Node.js；仅回环监听，局域网/公网不可访问。
- 最终发布验收清单仍待收口（见「未拍决策唯一索引」）。
- 各模块级验收标准见 `pre-prj/architecture/01–10` 各章末「验收标准（待实现验证）」。

## 未拍决策唯一索引

> 明确待拍板的开放项索引；`pre-prj/architecture/01–10` 各章「⚠️ 冲突 / 待拍」节同样有效，拍板后须同步更新本索引。

- Harness 生产参数：有限分类重试与受限纠错原则已确认；次数、超时、退避、错误映射、预算与压缩阈值仍待逐项拍板，先检查框架已有机制（参数管理方式已选 B：本地配置 + 硬边界，2026-09-07）。
- 压缩失败是否终止 Run：由待拍 Harness 策略决定（见 `pre-prj/architecture/08-agent-runtime.md` 8.7）。
- Agent 测评执行前置项：具体输出上限、执行预算/超时/重试参数、币种换算依据及双方实际计费核实；具体案例与 Rubric、证据包待编写及人工校准。
- web_search：尚未拍板；现行测评方案不依赖或授权搜索能力。
- Anthropic（含 cache_control 断点与 usage 浮出）与本地 Qwen——首版不支持，仅接入 DeepSeek 官方 OpenAI 兼容 API。
- 新增业务语义与事务边界：仍须拍板（已拍部分：草稿确认与修订校验、受限组合提交、安排接受即落盘、历史版本、更正统计、11 张业务表职责、固定业务时区与计划日程边界；字段名、索引与传输字段按既定语义整理，不逐项拍板）。
- P1 及其他后期内容（暂缓）：需求、分期、优先级、优化方向和实现细节均待具体情况再拍板；不列入当前成品的交付或验收前提。
- 最终发布验收清单：仍待收口（已拍板部分：核心自动化测试 + Windows 人工验收、Python 项目发布、不做桌面程序、Linux 开发与 Windows 成品验收）。

## 技术基座（2026-09-06 定案）

| 决策 | 选项 | 选了 | 为什么 |
|---|---|---|---|
| 技术选型推进方式 | A 一次收口 / B 分阶段确认分阶段开发 | B（2026-09-06） | 大决策多，先批准内核与最小闭环，未确认部分明确不实现 |
| 选型范围 | 仅技术方案 / 含产品业务规则 | 仅技术方案，PRD 与业务规则保留 | 产品需求已讨论充分，不再重议 |
| 投入重点 | 产品工程 / 内核实现 / 两者兼顾 | 3 兼顾，产品工程:内核 ≈ 7:3 | 求职叙事以完整产品工程为主 |
| Agent 基座路线 | A TS 直用 pi / B Python 现成库 / C Python 移植 pi | B（验证中），PydanticAI 优先、LangGraph 备选 | 排除 A（求职目标是 Python、破坏单进程不变量）与 C（增量投入产出不成立） |
| B 路线定案条件 | 直接采用 / 先 spike 验证 | 先 spike，验证 3 个缺口：常驻层前缀字节对齐、跨 Provider cache 指标穿透、整体 Run 取消语义 | 框架行为未经实测，不能直接承诺 |
| Agent 基座定案（B 路线收口） | A spike 证据直接定案 / B 先跑 LangGraph 对照再定 | A：PydanticAI 定案（2026-09-06） | 三缺口实测通过且独立复核重算一致；LangGraph 对照增量信息低、推迟收口；LangGraph 对照随之取消 |
| FastAPI / SQLite | — | 已拍板（FastAPI + SQLite/单进程异步单体），本轮不重议 | 与基座路线无关 |
| memory 记忆初版设计 | — | 本轮不读、不讨论；以 PRD / architecture-decisions 为准；回填阶段再拍板 | 与 spike 无关且存在冲突 |

spike 的专属决策（验证 Provider、模型分工、费用护栏、跨重启账本）与验收证据见 `pre-prj/architecture-archive/spikes/pydantic-ai-spike.md`。

## 粗略阶段路线

> 按章间依赖推导的参考顺序，非实现授权；开工顺序与批次以后续拍板为准。

| 阶段 | 内容 | 正本 |
|---|---|---|
| 0 底座 | SQLite 连接/事务/编号迁移/运行时表；数据目录与密钥底座 | 07；10.2/10.3 |
| 1 业务域骨架 | 动作目录、档案与安全限制（限制与红旗是推荐、计划、指导的输入） | 03；02 |
| 2 应用层事务 | 共用草稿确认与业务事务（context_version、受限组合、幂等确认、过期拦截与重算） | 01 |
| 3 核心业务流 | 计划与训练指导 → 训练记录与更正 → 统计与复盘数据 | 04 → 05 → 06 |
| 4 Agent 运行时 | Run 状态机、并发互斥、取消与重启恢复、Harness 参数与重试纠错、SSE 事件与恢复 | 08 |
| 5 验收与发布 | 产品自动化测试 + Windows 人工验收 + Agent 测评；Python 项目发布与凭据 | 09；10 |
| 贯穿 | 前端（只读看板 + 草稿卡）随对应后端阶段交付 | — |
