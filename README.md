## 本地运行与当前对话入口

运行环境：Python 3.13+、uv、Node.js 与 npm。在仓库根目录安装现有依赖：

```bash
npm install
npm --prefix frontend install
uv sync --project backend
```

在 `backend/.env` 配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`；凭据仅供后端读取。

启动前后端开发服务：

```bash
npm run dev
```

打开 `http://localhost:5173`。后端监听 `127.0.0.1:8000`，Vite 将 `/api` 请求代理到后端。保持默认端口；HTTP Host/Origin 校验允许本地 8000、5173 端口。

独立启动时，在两个终端分别执行：

```bash
npm run dev:backend
npm run dev:frontend
```

后端命令等价于在 `backend` 目录执行：

```bash
uv run python -m uvicorn app.interfaces.http:app --reload --host 127.0.0.1 --port 8000
```

产品定义与验收场景见 [PRODUCT.md](PRODUCT.md)。

## 技术栈

- **Agent：单 Agent Loop**。基于 ReAct 范式的最小执行循环及工具调用机制。
- **后端：FastAPI**。当前提供对话执行、会话历史、编辑与重新生成、Steering、取消、会话删除及画像查询接口。画像准备、自然语言确认后的保存及状态查询通过 Agent 工具执行。
- **数据校验：Pydantic**。定义并校验接口输入、工具参数及业务数据结构。
- **持久化：SQLite**。当前 schema 版本 5，通过 aiosqlite 保存个人画像、只读动作目录、会话消息、运行与操作记录、画像快照及保存幂等结果。迁移按版本执行，外键和结构校验保持开启；启动恢复中断操作，业务保存使用事务。
- **前端**。React、TypeScript、Vite 及 Shadcn 布局和组件。
