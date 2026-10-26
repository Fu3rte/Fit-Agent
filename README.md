# Fit-Agent

Fit-Agent 是一个通过自然语言管理个人画像、训练计划和实际训练记录的个人力量训练助手。MVP 面向本地运行、单用户使用，覆盖徒手及常见器械力量训练。

产品定位、业务功能、业务规则及业务验收场景见 [PRODUCT.md](PRODUCT.md)。

## 功能与使用

### 画像、计划与训练记录

在对话页描述训练目标、经验、器械条件、可投入时间与伤病情况，助手整理为画像快照并完整展示；点击该快照的“确认保存”后由后端直接保存。

描述或粘贴训练安排，或随消息上传 `.md`、`.txt` 文件，助手整理为计划快照；点击“确认保存”保存为当前计划。只发送附件时，助手先完整读取内容，再询问处理要求。

描述已完成的训练，助手整理训练日期、动作、实际组数、次数、重量与感受；核对后点击“确认保存”写入训练记录。

计划生成与调整由对话发起。调整会参考最近 10 条训练记录，展示修改位置、原值、新值、理由与依据；确认后保存完整新版本。历史计划版本与已有训练记录保持完整。

会话侧栏支持查看历史、编辑消息、重新生成回答、运行中补充文字或附件（Steering）、取消运行与删除会话。

保存只接受对应快照的“确认保存”按钮。聊天文字、附件内容、工具结果与模型参数都不产生保存授权；多份内容待确认时逐份确认。

### 独立页面

- 画像页展示已保存画像与不可用器械清单。
- 计划页按待确认、当前版本与历史版本展示训练日、动作、组次、重量与休息时间。
- 训练记录页按训练日期展示已保存记录。
- 侧栏设置配置模型服务与明暗主题。

### 动作检索

助手在查询动作、生成或调整计划时调用动作检索：输入名称、别名、英文名或描述，结合器械、训练部位与目标肌群筛选，先返回最多 5 条候选及匹配说明，再按动作 ID 读取完整步骤与重量口径。检索使用 Milvus 混合检索（BM25 稀疏向量 + 1024 维稠密向量）、RRF 融合与外部重排模型。检索服务未配置或不可用时，动作查询直接报错，画像、计划、记录与历史浏览继续可用。

### 训练知识库

内置 `training-theory` 技能，覆盖训练原理、计划编排、动作技术、渐进与记录、停滞与调整等主题。系统提示注入技能名称、描述与入口路径，助手按当前任务读取 `backend/skills/training-theory` 下的主题文件。

### 运行约束

每次正式运行最多发起 64 次模型请求，总预算 300 秒；达到上下文阈值时执行压缩，模型溢出时恢复。运行失败、取消或超时都会写入明确的停止原因。

## 快速开始

前置环境：Python 3.13+、uv、Node.js、npm。使用动作检索时需要 Docker 与可用的 Milvus 服务。

在仓库根目录安装依赖：

```bash
npm install
npm --prefix frontend install
uv sync --project backend
```

启动前后端开发服务：

```bash
npm run dev
```

打开 `http://localhost:5173`。后端监听 `127.0.0.1:8000`，Vite 将 `/api` 请求代理到后端。首次使用先在侧栏配置模型服务。

### 启动动作检索

```bash
docker compose -f deploy/milvus/docker-compose.yml up -d
```

在 `backend/.env` 中配置 Milvus、Embedding 与重排参数（见下文“检索配置”），然后构建并发布动作索引：

```bash
cd backend
uv run --env-file .env python -m test.check_retrieval_index build
```

首次构建会调用 Embedding 服务批量编码全部动作，产生相应费用；已有正式索引时该命令直接复用。`npm run dev:backend` 同样读取 `backend/.env`。

## 配置

### 模型服务

- 在侧栏设置中填写协议、Base URL、模型 ID 与 API Key，可填服务标识 `provider`（如 `stepfun`、`openrouter`）。协议为 `openai-completions` 或 `anthropic-messages`。
- `provider` 省略、为空或仅空白时使用协议值；有内容时去除首尾空白。保存、测试、正式运行与配置读取使用同一规则。
- Base URL 支持 HTTP 与 HTTPS，可指向本机、局域网或公网。HTTP 会明文传输 API Key 与对话数据。
- 打开弹窗时 API Key 默认隐藏，可通过右侧按钮切换显示。保存与测试都要求非空 Key；修改 Base URL 时需一并提交对应 Key。
- 保存与测试独立。测试检查流式响应与工具调用并展示结果和耗时，不保存配置、不修改业务数据；测试可能产生费用。
- 未配置有效模型时阻止新的模型运行；配置入口、历史浏览与快照确认仍可使用。确认保存不调用模型。
- 清除凭据移除全部配置字段，业务数据与会话保持完整。配置重启后保留，修改只影响后续运行。

### 检索配置

写入 `backend/.env`：

| 变量 | 作用 |
| --- | --- |
| `FIT_AGENT_MILVUS_URI` | Milvus 地址，本机 Compose 部署为 `http://127.0.0.1:19530` |
| `FIT_AGENT_MILVUS_TOKEN` | Milvus 令牌，本地部署可留空 |
| `FIT_AGENT_MILVUS_TIMEOUT` | Milvus 连接超时秒数，必填且为正数 |
| `DASHSCOPE_API_KEY` | Embedding 服务密钥 |
| `DASHSCOPE_WORKSPACE_ID` | DashScope 工作空间 ID，用于生成 Embedding Base URL |
| `FIT_AGENT_EMBEDDING_MODEL` | Embedding 模型，默认 `qwen3.7-text-embedding-flash` |
| `FIT_AGENT_EMBEDDING_DIMENSIONS` | 向量维度，当前只支持 `1024` |
| `RERANKER_MODEL` | 重排模型 |
| `RERANKER_BASE_URL` | 重排完整请求地址 |
| `RERANKER_API_KEY` | 重排服务密钥 |
| `RERANKER_TIMEOUT` | 重排超时秒数，默认 `10` |

索引清单绑定语料哈希、Embedding 模型、维度、编码规则与 Milvus 版本。配置不兼容、索引缺失或服务不可用时，检索入口直接给出明确错误。

### 评测配置

- 裁判模型：`FIT_AGENT_EVAL_JUDGE_MODEL`、`FIT_AGENT_EVAL_JUDGE_BASE_URL`、`FIT_AGENT_EVAL_JUDGE_API_KEY`。
- Langfuse：`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，可选 `LANGFUSE_BASE_URL`（Cloud 默认 `https://cloud.langfuse.com`）；当前评测平台配置。
- LangSmith：`LANGSMITH_API_KEY`、`LANGSMITH_ENDPOINT`；历史平台配置，langsmith 包经传递依赖保留，历史链路可读。

### 前端端口

前端端口由启动环境变量 `FIT_AGENT_FRONTEND_PORT` 统一配置，默认 `5173`，合法范围 `1–65535`。`npm run dev` 走 `scripts/dev.mjs`：从配置端口起逐个探测空闲端口（同时检查 `127.0.0.1` 与 `::1`，Vite 监听 `localhost` 时可能只占用 IPv6），把选中的端口打印到终端并注入前后端两个进程，两边始终共享同一配置，后端仅允许本地 8000 和该端口的 Host/Origin。Vite 启用 `strictPort`。端口配置在进程启动时读取，修改后重启前后端。

PowerShell 中使用自定义端口：

```powershell
$env:FIT_AGENT_FRONTEND_PORT = "5176"
npm run dev
```

打开 `http://localhost:5176`。`npm run dev:frontend` 直接遵守配置端口，端口被占用时明确报错；需要顺延效果时使用 `npm run dev`。

独立启动时，在两个终端分别执行：

```bash
npm run dev:backend
npm run dev:frontend
```

后端命令等价于在 `backend` 目录执行：

```bash
uv run --env-file .env python -m uvicorn app.interfaces.http:app --reload --host 127.0.0.1 --port 8000
```

## 技术栈

- **Agent：单 Agent Loop**。基于 ReAct 范式的执行循环、工具调用与固定技能库。
- **后端：FastAPI**。提供对话执行、会话历史、编辑与重新生成、Steering、取消、会话删除及业务查询接口。Agent 使用查询、检索与准备工具；独立确认 HTTP 接口负责结构化授权、直接保存与原操作状态查询。
- **动作检索：Milvus + pymilvus**。BM25 稀疏检索与稠密向量混合召回，RRF 融合，外部重排模型二次排序；索引按版本发布并保留既有集合。
- **数据校验：Pydantic**。定义并校验接口输入、工具参数及业务数据结构。
- **持久化：SQLite**。通过 aiosqlite 保存画像、会话消息、运行与操作记录、画像快照、计划、训练记录及保存幂等结果；观测数据独立写入 `backend/data/trace.db`。迁移按版本执行，外键与结构校验保持开启；启动恢复中断操作，业务保存使用事务。
- **前端**。React、TypeScript、Vite、Tailwind CSS 及 Shadcn/Radix 组件。
- **评测（开发依赖）**。MTEB 计算检索指标，Langfuse 管理实验与上传，Ragas 完成本地生成评分，外部裁判处理生成评分。

## 项目结构

```text
backend/app/            FastAPI 接口、应用、领域、Agent、AI 及基础设施分层
backend/dataset/        只读动作目录与版本化评测数据
backend/skills/         Agent 技能定义（training-theory）
backend/evaluation/     检索、场景、技能评测与结果汇总
backend/test/           pytest 测试与 check_* 自检脚本
deploy/milvus/          Milvus Compose 部署
frontend/src/           React 应用（app、components、features、lib）
optimization/           方案、接口契约与操作说明
quantitive-index/       评测口径、基线与结果记录
scripts/dev.mjs         前后端统一启动与端口探测脚本
tmp/                    本地设计与证据目录（gitignore）
```

## 开发与验证

后端在 `backend` 目录执行：

```bash
uv run python -m pytest
uv run ruff check .
```

主题验证以 `test/check_*.py` 自检脚本逐个运行，形如 `uv run python -m test.check_compaction_core`；部分 `check_*` 端到端脚本依赖真实模型服务与检索服务。

评测入口位于 `backend/evaluation`，在 `backend` 目录执行，示例：

```bash
uv run --locked --env-file .env python -m evaluation.retrieval run --dataset dataset/evaluation/rebuild-v1 --groups hybrid --rerank
uv run --locked --env-file .env python -m evaluation.langfuse_scenarios run --purpose validation --scoring-mode external
uv run --locked python -m evaluation.summary report --source backend/temp/evaluation-scenarios/<run_id>
```

动作索引与检索的检查入口为 `python -m test.check_retrieval_index`、`python -m test.check_retrieval_rerank_production` 等模块。

运行观测写入 `backend/data/trace.db`，报告命令在 `backend` 目录执行：

```bash
uv run python -m app.agent.trace_report metrics
uv run python -m app.agent.trace_report show --run-id <UUID>
uv run python -m app.agent.trace_report show --operation-id <UUID>
```

真实业务 Trace 默认只写本地诊断库。需要导出到 Langfuse Cloud 时，在服务端环境显式开启后重启：

```bash
FIT_TRACE_CLOUD_ENABLED=true
LANGFUSE_PUBLIC_KEY=<项目公钥>
LANGFUSE_SECRET_KEY=<项目密钥>
LANGFUSE_BASE_URL=<Langfuse 地址>
FIT_TRACE_ENVIRONMENT=production   # 或 development
FIT_TRACE_RELEASE=<可选版本标记>
```

开关关闭时不创建 Cloud 客户端、不校验上述凭据、不发起网络请求。同一进程只初始化一个业务客户端；评测 CLI 在独立进程中运行。Cloud 导出只上传白名单观测字段，队列满时整条 Trace 丢弃，关闭最多等待 5 秒。

前端执行 `npm --prefix frontend run build` 完成类型检查与构建；按主题的验证脚本包括 `verify:bubble`、`verify:profile`、`verify:workout`、`verify:plan`、`verify:runs`、`verify:stability`、`verify:confirm`、`verify:confirm:http`、`verify:provider`、`verify:markdown`。

## 文档职责

- [PRODUCT.md](PRODUCT.md)：产品定位、业务功能、业务规则及业务验收场景。
- [optimization](optimization)：方案、接口契约、评测操作与验收口径。
- [quantitive-index](quantitive-index)：检索、场景与技能评测的口径、基线和结果记录。
- README.md：运行方式、配置、功能使用、技术栈及验证入口。
