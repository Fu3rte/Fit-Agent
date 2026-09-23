# Fit-Agent

本地运行的健身训练助手：管理用户画像、训练计划、训练记录与身体指标，并通过对话生成计划和辅助记录训练。前端使用 React、TypeScript 和 Vite；后端使用 FastAPI、LangGraph 和 SQLite。

## 功能

- 用户画像：训练目标、每周训练次数、训练方式（徒手／器械）、偏好、水平、伤病及禁用动作。器械训练按拥有全部器械处理。
- 训练计划：基于画像和训练记录生成七天计划草稿，确认后生效；支持查看计划和训练日历。
- 训练记录：表单记录与对话辅助记录；查看历史、个人最佳和体重趋势。
- 对话：保存会话历史，支持计划确认与打卡确认。
- 模型配置：在界面内填写 API Key、Base URL、模型名、API 协议和结构化输出方式，并测试连通性。

## 本地开发

需要 Python 3.13+、uv 和 Node.js 22+。从仓库根目录分别启动两个终端：

```bash
cd backend
uv sync --group dev
uv run python main.py
```

```bash
cd frontend
npm ci
npm run dev
```

在浏览器访问 Vite 终端显示的本地地址。开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`。启动后可在侧栏的「模型配置」中填写模型信息；使用对话功能前需要完成配置。健康检查地址为 `http://127.0.0.1:8000/healthz`。

## 构建与运行

在 `frontend/` 执行 `npm ci && npm run build`，然后在 `backend/` 执行 `uv sync && uv run python main.py`。后端会托管 `frontend/dist/`，浏览器访问 `http://127.0.0.1:8000`。服务仅监听回环地址，默认端口为 8000；可通过 `--port` 修改。数据库迁移在后端启动时执行。

## 数据与配置

业务数据库、LangGraph checkpoint 数据库和 `provider.json` 保存在操作系统的用户数据目录。可用环境变量 `FIT_AGENT_DATA_DIR` 指定数据目录。模型凭据保存在该目录的 `provider.json` 中；此文件包含 API Key，请勿提交到版本库或共享。

后端仅接受本地回环 Host／Origin，适合单机使用。默认不暴露 OpenAPI 文档页面。