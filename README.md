# Fit-Agent

Fit-Agent 是一个通过自然语言管理个人画像、训练计划和实际训练记录的个人力量训练助手。MVP 面向本地运行、单用户使用，覆盖徒手及常见器械力量训练。

产品定位、业务功能、业务规则及业务验收场景见 [PRODUCT.md](PRODUCT.md)。

## 功能特性

- 对话驱动画像收集与更新、计划录入/生成/调整及训练记录整理；完整展示快照后，用户点击对应的“确认保存”按钮，后端直接落库。普通聊天文字不产生保存授权。
- 会话支持历史查看、编辑与重新生成、运行中补充信息（Steering）、取消与删除。
- 附件：一条消息可挂多个 UTF-8 编码的 `.md`、`.txt` 文件，总大小上限 100,000 字节，超限拒绝提交。详细约定见 `tmp/design/attachment-session-interaction.md`。
- 训练计划在待确认、当前版本与历史版本统一使用只读看板：移动端单列，`lg` 断点起三列。展示约定见 `tmp/design/page-layout-display-conventions.md`。
- 模型服务通过应用内弹窗配置，所有会话共用一套全局配置，支持 OpenAI Completions 与 Anthropic Messages 协议。
- 运行预算与上下文压缩：每次正式运行最多发起 64 次模型请求，覆盖阈值压缩、溢出恢复及终态分类。细节见 `tmp/design/model-request-budget.md`。
- 持久化 SQLite，会话与已确认业务数据在重新打开应用后可查看。

## 快速开始

前置环境：Python 3.13+、uv、Node.js 与 npm。在仓库根目录安装依赖：

```bash
npm install
npm --prefix frontend install
uv sync --project backend
```

启动前后端开发服务：

```bash
npm run dev
```

打开 `http://localhost:5173`。后端监听 `127.0.0.1:8000`，Vite 将 `/api` 请求代理到后端。首次使用先通过侧栏配置入口保存模型配置。

## 配置

### 模型配置

- 弹窗必填协议、Base URL、模型 ID、API Key，可填服务标识 `provider`（如 `stepfun`、`openrouter`）。省略、空字符串或仅空白时使用所选协议值；有内容时去除首尾空白。保存、测试、正式运行与配置读取使用同一规则。
- Base URL 允许 HTTP 和 HTTPS，可指向本机、局域网及公网。HTTP 明文传输 API Key 和对话数据，存在泄露风险。
- 打开弹窗明文展示当前 Key；保存与测试均拒绝空 Key，更换 Base URL 时必须明确提交对应 Key。
- 保存与测试独立。测试检查流式响应和工具调用并展示结果与耗时，不自动保存，不修改业务数据，失败保持已保存配置完整。测试可能产生费用。
- 无有效模型配置时阻止新的模型运行，禁止自动切换服务；配置入口、业务数据浏览及有效快照的按钮确认仍可使用。确认保存不调用模型。
- 清除凭据移除全部配置字段，保持业务数据与会话完整。配置重启后保留，运行期间修改只影响后续运行。
- 本地访问保护及模型凭据保护属于基础要求。

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
uv run python -m uvicorn app.interfaces.http:app --reload --host 127.0.0.1 --port 8000
```

## 技术栈

- **Agent：单 Agent Loop**。基于 ReAct 范式的最小执行循环及工具调用机制。
- **后端：FastAPI**。提供对话执行、会话历史、编辑与重新生成、Steering、取消、会话删除及业务查询接口。Agent 使用查询和准备工具；独立确认 HTTP 接口负责结构化授权、直接保存与原操作状态查询。
- **数据校验：Pydantic**。定义并校验接口输入、工具参数及业务数据结构。
- **持久化：SQLite**。通过 aiosqlite 保存个人画像、只读动作目录、会话消息、运行与操作记录、画像快照及保存幂等结果。迁移按版本执行，外键和结构校验保持开启；启动恢复中断操作，业务保存使用事务。
- **前端**。React、TypeScript、Vite 及 Shadcn 布局和组件。

## 项目结构

```text
backend/app/            FastAPI 接口、应用、领域、Agent、AI 及基础设施分层
backend/dataset/        只读动作目录数据
backend/skills/         Agent 技能定义
backend/test/           pytest 测试与 check_* 自检脚本
frontend/src/           React 应用（app、components、features、lib）
scripts/dev.mjs         前后端统一启动与端口探测脚本
tmp/                    本地设计与证据目录（gitignore）
```

## 开发与验证

- 后端在 `backend` 目录执行 `uv run python -m pytest` 运行测试用例，`uv run ruff check .` 做静态检查。主题验证以 `test/check_*.py` 自检脚本逐个运行，形如 `uv run python -m test.check_compaction_core`；部分 `check_*` 端到端脚本依赖真实模型服务。
- 前端执行 `npm --prefix frontend run build` 完成类型检查与构建；按主题的场景验证入口为 `verify:plan`、`verify:profile`、`verify:workout`、`verify:runs`、`verify:markdown` 等脚本。

## 文档职责

- [PRODUCT.md](PRODUCT.md)：产品定位、业务功能、业务规则及业务验收场景。
- README.md：运行方式、配置、技术栈及验证入口。
- [结构化保存确认方案](tmp/optimization/structured-save-confirmation.md)：已拍板的确认授权、HTTP、历史扩展及验收基线，待实现和联合验证。实现 Agent 只修改代码、迁移与测试，不修改文档。
