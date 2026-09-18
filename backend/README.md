# Fit-Agent Backend

本地单用户异步单体：Browser → 单个 Python 进程（FastAPI ＋ 单 Uvicorn Worker，仅回环监听）→
OpenAI 兼容模型端点。SQLite ＋ aiosqlite ＋ 手写 SQL；LangGraph 提供 State、Checkpointer 与计划子图。

## 分层与依赖方向

```text
api/       FastAPI 工厂、DTO 与统一错误形状、表单路由、Agent 四端点（只做协议转换）
  ↓
graph/     LangGraph 运行时：Router、MemoryAssembler、SkillLoader、Planner／Evaluator 子图、
           Run 事件流与唯一模型入口
  ↓
domain/    actions / body_metrics / plans / profile / records / stats：纯确定性规则与事务
  ↓
storage/   SQLite 连接与串行锁、编号迁移、事务入口
```

`graph/` 只经 `domain/` 的服务读写业务事实；`domain/` 不依赖 FastAPI、LangGraph 或模型 SDK
（由 `tests/test_stage4_plan_schema_and_rules.py::FORBIDDEN_DOMAIN_IMPORTS` 守卫）。

## 目录

```text
backend/
├── config.py                 # 数据目录、fit_agent_langgraph.db / langgraph_checkpoints.db 文件名、
│                             # MODEL_* 环境变量常量、60s/180s/5 次 Run 上限
├── business_time.py          # 业务时区校验与业务自然日
├── main.py                   # 进程入口：装配 api.app，单 Worker，仅回环地址
├── api/
│   ├── app.py                # FastAPI 工厂 ＋ lifespan（迁移、唯一业务连接、独立 Checkpointer、运行时装配）
│   ├── deps.py               # 业务日期依赖注入（客户端不传日期）
│   ├── dto.py                # 请求体模型、领域对象 ↔ 传输对象、统一错误形状与状态映射
│   ├── routes_agent.py       # /api/agent/run（SSE）、confirm、reject、confirm-workout
│   ├── routes_records.py     # 训练记录、身体指标、动作目录
│   ├── routes_plans.py       # 计划与计划日程只读
│   ├── routes_profile.py     # 画像读取与整份覆盖写
│   └── routes_stats.py       # PB、趋势、月历只读
├── domain/
│   ├── actions/              # 动作目录：稳定 exercise_id、记录口径、负重口径、增重单位
│   ├── body_metrics/         # 身体指标事实
│   ├── plans/                # 计划统一 PlanDraft／版本／日程／激活与拒绝事务／确定性渐进规则
│   ├── profile/              # 画像三态事实与急性关键词封闭词表
│   ├── records/              # 训练事实与组、日程关联（plan_session_id／auto_link）
│   └── stats/                # PB、趋势、月历的只读确定性计算
├── graph/
│   ├── checkpointer.py       # 独立 SQLite Checkpointer 的生命周期与 thread_config
│   ├── context.py            # MemoryAssembler：六类上下文的固定装配范围
│   ├── model.py              # 唯一模型入口与严格 JSON 解析（不回显密钥／端点／模型名）
│   ├── router.py             # 五类 intent 的确定性词表 ＋ 零／多命中一次分类
│   ├── skills.py             # SkillLoader：启动只注入元数据，命中后加载正文
│   ├── state.py              # WorkflowState（11 字段冻结）
│   ├── nodes.py              # 计划子图节点：安全、装配、Skill、Planner、Evaluator、修订、持久化、确认
│   └── workflow.py           # 子图拓扑、Run 事件流与自然语言打卡分支
├── skills/
│   ├── workout-planning/SKILL.md ＋ references/
│   └── plan-adjustment/SKILL.md
├── storage/
│   ├── db.py                 # 唯一 aiosqlite 连接 ＋ asyncio.Lock ＋ 显式事务入口
│   ├── errors.py             # 存储层错误
│   ├── migrations.py         # 编号迁移，按 PRAGMA user_version 逐个文件在同一事务内执行
│   └── migrations/           # 001 初始 Schema、002 计时组与新动作、003 rejected 状态
└── tests/                    # pytest（按 Stage 冻结契约的断言 ＋ 行为测试）
```

## 硬规则

1. `domain/` 不 import FastAPI／LangGraph／模型 SDK：领域规则是确定性 Python，写入校验不经 Prompt。
2. 模型调用只在 `graph/model.py` 的唯一入口；节点不写业务库，写入由 `domain/*/service.py` 的短事务负责。
3. 业务库与 Checkpointer 是**不同文件、不同连接**；Checkpoint 表由 saver 自管，不写业务迁移。
4. SQL 只在 `domain/*/repo.py` 与 `storage/`；事务体内不做模型请求、不推 SSE。
5. SSE 只发 `node`／`message`／`waiting`／`done`／`error` 五类产品事件，不暴露原始 LangGraph 事件、
   系统提示词或 Provider 配置。
6. 模型配置只从 `MODEL_API_KEY`／`MODEL_BASE_URL`／`MODEL_MODEL` 读取，取值不回显、不落库、不进 State。

## 运行与测试

```bash
cd backend
uv sync --group dev --locked
uv run main.py

uv run pytest
uv run ruff check .
```
