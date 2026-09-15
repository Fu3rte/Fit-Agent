# 旧版基线

基线提交：`b54a13fe8933e10d8ae41039ab486a27775c11c8`

记录日期：2026-09-15

## 可运行命令

```bash
cd backend && uv run pytest
cd backend && uv run ruff check .
cd frontend && npm run build
```

## 重构前结果

- 后端测试：`1235 passed, 1 warning in 43.91s`
- Ruff：失败，既有代码共 13 个问题（11 个可自动修复）；主要位于 `agent_core/ai/` 与 `backend/scripts/`
- 前端构建：成功，Vite 构建 1990 个模块；存在单个 JS chunk 大于 500 kB 的既有警告
