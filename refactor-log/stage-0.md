# 阶段 0：建立重构基线

| 目标 | 情况 | 遗留问题 |
| --- | --- | --- |
| 建立 LangGraph 重构分支与运行基线；替换 PydanticAI 依赖；隔离新版数据库和模型环境变量 | 已完成（`680ef6e`）；新库 `fit_agent_langgraph.db`；阶段 Gate 8 项测试、Ruff、LangGraph/Checkpoint 导入均通过 | 旧业务代码和旧测试仍依赖 PydanticAI，待对应新版 Gate 通过后分阶段删除；新版业务 Schema 留待阶段 1 |
