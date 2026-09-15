 Stage 0 已完成基线

 - [x] 已位于 refactor/langgraph 分支，基线提交：680ef6e
 - [x] 已移除新运行路径中的 PydanticAI 依赖，加入 LangGraph / SQLite Checkpointer
 - [x] 新数据库隔离为 fit_agent_langgraph.db
 - [x] 模型配置改用环境变量，API Key 不回显、不写日志
 - [x] FastAPI 当前仅装配数据库、时区、健康检查和静态前端
 - [x] 无 API Key 时仍可启动
 - [x] Stage 0 测试、Ruff、依赖导入检查通过
 - [ ] 当前新数据库仍是空库，app.py 尚未调用迁移
 - [ ] 旧业务模块、旧迁移、旧测试和前端旧记录流程仍保留，Stage 1 开始替换

 ────────────────────────────────────────────────────────────────────────────────

 Stage 1：最小 Schema 与表单写入计划清单

 1. 编码前需确认的业务决策

 以下内容在权威文档中没有冻结，不能自行假设：

 - [ ] 训练记录删除采用物理删除还是 deleted_at 软删除
 - [ ] 身体指标、重量、次数、组数允许的具体数值范围
 - [ ] athlete_profile 中“未填写”和“明确为空”的具体存储表达
 - [ ] plans.structured_content 的 Stage 1 最小 JSON 形状
 - [ ] 旧动作种子中哪些继续保留，以及每个动作的 min_load_increment

 2. 重置数据库迁移

 - [ ] 删除旧 backend/storage/migrations/001–016
 - [ ] 新建 backend/storage/migrations/001_initial.sql
 - [ ] 一次建立：
     - athlete_profile
     - exercises
     - body_metrics
     - workout_sessions
     - workout_sets
     - plans
     - plan_sessions
 - [ ] 不在业务迁移中创建 LangGraph Checkpoint 表
 - [ ] 为状态、组类型、数值范围和外键增加 SQLite 约束
 - [ ] 增加“最多一个 active 计划”的部分唯一索引
 - [ ] 增加“同一计划日程最多关联一条有效训练”的唯一约束
 - [ ] 筛选并校验动作种子：稳定 ID、负重口径、器械、是否可规划、最小加重单位
 - [ ] 在 backend/api/app.py lifespan 中执行 await db.migrate()
 - [ ] 更新 Stage 0 的“空数据库”测试，使其验证新版表和 user_version=1
 - [ ] 测试重复启动不会重复建表或覆盖数据

 3. 业务时间

 - [ ] 新建 backend/business_time.py
 - [ ] 从旧 setting_repo.py 迁出：
     - business_date(instant, timezone_name)
     - IANA 时区校验
 - [ ] lifespan 启动时只采样一次本机时区
 - [ ] 将冻结时区注入 API 用例
 - [ ] repo、domain service 中禁止直接调用 date.today()
 - [ ] 时区无效时明确失败，不回退 UTC
 - [ ] 添加跨 UTC 日期边界和固定时钟测试

 4. 动作目录 domain/actions

 - [ ] 按新 exercises 表重写最小 Schema、repo、rules、service
 - [ ] 保留稳定动作 ID 和明确负重口径
 - [ ] 新增并校验 min_load_increment
 - [ ] 提供动作列表和按 ID 查询
 - [ ] 写入训练前验证动作存在且负重口径匹配
 - [ ] 删除旧计划模板、RIR 和不再使用的动作目录语义

 5. 用户画像 domain/profile

 - [ ] 按新版字段重写画像模型：
     - 用户目标
     - 每周训练次数
     - 可用器械
     - 明确偏好
     - 当前水平
     - 已知伤病
     - 禁用动作 ID
 - [ ] 删除 context_version
 - [ ] 保证“未填写”与“明确为空”可区分
 - [ ] 禁用动作必须引用有效稳定 exercise_id
 - [ ] 实现单用户画像读取与更新
 - [ ] 添加结构、范围、外键和空值语义测试

 6. 身体指标 domain/body_metrics

 - [ ] 新建 schema、repo、rules、service
 - [ ] 实现体重/体脂：
     - 新增
     - 查询
     - 修改
     - 删除
 - [ ] 校验业务日期、必填字段和数值范围
 - [ ] 无数据保持空值，不补零
 - [ ] 添加完整 CRUD 和非法输入测试

 7. 训练记录 domain/records

 - [ ] 用 workout_sessions / workout_sets 替换旧修订链模型
 - [ ] 定义组类型：work / warmup / assisted
 - [ ] 彻底移除 RIR、辅助次数、旧三桶判定及修订草稿字段
 - [ ] 实现一次训练及其组的原子新增
 - [ ] 实现训练及训练组查询、修改、删除
 - [ ] 校验：
     - 日期
     - 动作 ID
     - 负重口径
     - 重量
     - 次数
     - 组数
     - 组类型
 - [ ] 支持可空 plan_session_id
 - [ ] NULL 明确表示额外训练
 - [ ] 自动关联仅限“当天恰好一个未完成日程”
 - [ ] 零个或多个候选时不得猜测
 - [ ] 添加事务回滚、关联歧义及重复完成测试

 8. 计划只读能力 domain/plans

 - [ ] 以新 plans / plan_sessions 重写 schema 和 repo
 - [ ] 支持读取：
     - 当前 active 计划
     - draft 计划
     - 历史计划版本
     - 计划日程
 - [ ] 保存来源计划 ID、单调版本号和最小评估字段
 - [ ] 暂不开放计划创建、确认、拒绝或激活入口
 - [ ] Stage 1 测试只通过 fixture/直接 repo 写入计划数据
 - [ ] 计划激活事务留到 Stage 5

 9. 表单 API

 - [ ] 精简 backend/api/deps.py，移除 PydanticAI、Provider 数据库设置和旧 Runtime 依赖
 - [ ] 精简或重写 backend/api/dto.py
 - [ ] 新增并注册：
     - routes_profile.py
     - routes_records.py
     - routes_plans.py
 - [ ] 补充身体指标和动作目录所需接口
 - [ ] 表单写入直接调用业务 service，不创建 Agent Run 或通用草稿
 - [ ] 统一将 Pydantic、领域规则和数据库约束错误映射为明确的 4xx
 - [ ] 添加 API CRUD、缺字段、非法类型和不存在资源测试

 10. 前端记录页

 - [ ] 重写 frontend/src/features/records/RecordsPage.tsx
 - [ ] 增加训练记录新增、编辑、删除表单
 - [ ] 增加身体指标新增、编辑、删除表单
 - [ ] 提供动作选择、负重口径、重量、次数、组类型输入
 - [ ] 支持添加/删除训练组
 - [ ] 支持选择计划日程或明确标记额外训练
 - [ ] 删除：
     - “通过对话更正”入口
     - 旧修订历史
     - 三桶判定
     - 完成率文案
     - RIR 相关字段
 - [ ] 更新 frontend/src/lib/api.ts 和 contract.ts
 - [ ] 写入成功后失效记录和身体指标 Query
 - [ ] 保留加载、空态、提交失败和删除确认

 11. 旧代码清理

 - [ ] 删除被新版替代的旧 records/profile/actions/plan 实现
 - [ ] 删除对应的草稿确认、修订链和旧统计耦合代码
 - [ ] 删除对应旧测试，改为新版确定性测试
 - [ ] 清除后端运行代码、迁移、DTO、前端契约中的 RIR 字段
 - [ ] 不保留新旧双写、兼容迁移或旧数据库读取路径

 12. Stage 1 Gate

 - [ ] 临时目录首次启动创建 fit_agent_langgraph.db 和完整业务 Schema
 - [ ] 用户画像可读取和更新
 - [ ] 身体数据可新增、查询、修改、删除
 - [ ] 训练及训练组可新增、查询、修改、删除
 - [ ] 非法日期、重量、次数、组数和缺字段均被拒绝
 - [ ] 额外训练和计划日程关联规则正确
 - [ ] 数据库事务失败不留下半条训练
 - [ ] 全仓运行时代码、迁移、DTO及前端契约无 RIR 字段
 - [ ] uv run pytest 对 Stage 1 新测试通过
 - [ ] uv run ruff check . 通过
 - [ ] npm run build 通过

 建议实施顺序：确认未决口径 → 001_initial.sql → business time → domain/repo/service → API → 前端 → 删除旧路径 → Gate。