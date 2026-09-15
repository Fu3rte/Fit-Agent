# Fit-Agent

基于 FastAPI、SQLite 与 LangGraph 重构的本地单用户健身训练助手。

## 当前进度

已完成 LangGraph 重构阶段 0：新依赖与本地运行基线。训练记录、统计和 Agent 工作流将在后续阶段按 `LANGGRAPH_REFACTOR_PLAN.md` 实现。

## 本地配置

复制 `.env.example` 为 `.env`：

```dotenv
MODEL_API_KEY=
MODEL_BASE_URL=
MODEL_MODEL=
```

- 模型配置只从环境变量或本地 `.env` 读取，不写入数据库、不回显具体值。
- `MODEL_API_KEY` 缺失时非模型业务仍可启动。
- 可用 `FIT_AGENT_DATA_DIR` 覆盖本地数据目录。

## 数据库

新版使用 `fit_agent_langgraph.db`，与旧版 `app.db` **不兼容**，不会读取或迁移旧数据。首次启动会在本地数据目录创建新文件；业务 Schema 从阶段 1 的新版 `001_initial.sql` 开始。

## 运行

```bash
cd backend
uv sync --group dev --locked
uv run main.py
```

服务仅允许监听 `127.0.0.1`、`localhost` 或 `::1`，默认地址为 <http://127.0.0.1:8000>。

阶段 0 验证：

```bash
cd backend
uv run pytest tests/test_langgraph_stage0.py
uv run ruff check api/app.py config.py tests/test_langgraph_stage0.py
```

旧版基线命令和结果见 [`BASELINE.md`](BASELINE.md)。
