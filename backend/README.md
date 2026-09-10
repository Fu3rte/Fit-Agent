# Fit-Agent Backend

进程内异步单体（PLAN.md「架构形状」）：Browser → 单个 Python 进程（FastAPI + 单 Uvicorn Worker）→ 模型 HTTP 端点。SQLite + aiosqlite + 手写 SQL，PydanticAI 为 Agent 基座。

## 分层与依赖方向（只能向下）

```
api/       FastAPI 薄路由 + DTO + 静态托管（只做协议转换）
  ↓
runtime/   Agent 运行时（architecture/08）：Run 状态机/单 Run 互斥/取消/SSE 投影；PydanticAI 只进这层
  ↓
app/       共用应用层（architecture/01）：草稿生命周期、context_version、确认事务——唯一正式写入事务点
  ↓
domain/    五项业务职责（architecture/02–06）：纯确定性规则，禁止依赖 Agent 框架/FastAPI
  ↓
storage/   SQLite 连接/单锁/编号迁移/repo（architecture/07）
```

## 目录

```
backend/
├── config.py                 # 数据目录(platformdirs)、固定业务时区、Harness 本地配置+硬边界（07 7.3/08 8.5/10.2）
├── main.py                   # 进程入口：装配 api.app，单 Worker 回环监听
├── storage/
│   ├── db.py                 # 唯一 aiosqlite 连接 + asyncio.Lock + 事务入口（07 7.1）
│   ├── migrations.py         # 编号迁移 + PRAGMA user_version（07 7.2）
│   ├── migrations/           # 001_*.sql 按序，user_version +1
│   ├── run_repo.py           # 运行时四表 conversations/messages/runs/run_events（07 7.4/7.5）
│   └── setting_repo.py       # 业务时区、Provider 配置（密钥只回 has_api_key，10.3）
├── domain/                   # 每个模块统一四件套 schema/rules/repo/service
│   ├── profile/              # 02 档案与安全限制（红旗、动作限制）
│   ├── actions/              # 03 动作目录（种子写入不走草稿）
│   ├── plan/                 # 04 计划版本/日程锁定/安排快照
│   ├── records/              # 05 打卡事实/待补全转正式/更正作废
│   └── stats/                # 06 完成率/三桶/PR（只读计算）
├── app/
│   ├── drafts.py             # Pending/Committed/Discarded、修订版本、幂等凭据、一键重算（01 1.3/1.6）
│   ├── draft_repo.py         # business_drafts 最小读写：快照/revision/来源/提交凭据（01 1.3）
│   └── confirm.py            # 幂等→版本检查→领域复查→原子提交（01 1.4/1.5）
├── runtime/
│   ├── agent_factory.py      # PydanticAI 装配：system prompt、工具注册、Harness 参数（08 8.5/8.6）
│   ├── run_service.py        # client_request_id 幂等、全局单 Run 槽、409 conversation_busy（08 8.2）
│   ├── run_task.py           # 状态流转、取消/draining、重启标 failed（08 8.1/8.3/8.4）
│   ├── tools.py              # 领域工具适配：只产 Pending 草稿，不写正式事实（不变量 7）
│   ├── context.py            # 每轮从业务表确定性投影档案/限制/计划到上下文
│   └── events.py             # SSE 产品语义白名单 + 15s heartbeat（08 8.7）
├── api/
│   ├── app.py                # FastAPI 工厂 + lifespan（连接/迁移/回环/静态托管）
│   ├── routes_chat.py        # 会话/消息/SSE
│   ├── routes_drafts.py      # 草稿纠错/确认/丢弃业务接口（01 1.2）
│   ├── routes_readonly.py    # 只读看板：档案/计划/记录/统计
│   ├── routes_settings.py    # Provider 配置（只回 has_api_key）
│   └── routes_media.py       # 本地动作媒体目录（PRD 5.11）
├── tests/                    # 集成测试；单元测试就近放各包
└── archive/
    └── agent_core_pi_port/   # pi 移植路线 A 残留，2026-09-09 归档，不参与构建
```

## 解耦硬规则

1. `domain/` 不 import PydanticAI/FastAPI；领域规则是确定性 Python，不用 Prompt 当写入校验（01）。
2. Agent 工具只能创建 Pending 草稿；正式事实只能经 `app/confirm.py` 事务（不变量 7）。
3. SQL 只在 repo；事务不跨模型请求/工具执行/SSE（07 7.1）。
4. 跨模块读可以、跨模块写只能在 `app/` 组合事务（受限组合草稿，01 1.5）。
5. SSE 是产品语义白名单投影，前端不依赖 PydanticAI 内部事件（08 8.7）。
6. 模块读依赖：`actions` ← `plan`/`records`；`profile` → `plan`；`stats` → `records`+`plan`（只读）。
