# Stage 4 证据（Agent 运行时）

> 本文件只记录**证据**：切片状态、最小命令与结果、真实调用用量与花费、缺口。
> 契约与验收正本在 `../stage4.md`（§5 切片、§6 前端契约、§7 完成定义）与
> `../../architecture/08-agent-runtime.md`；此处不复制契约、不贴原始日志或代码。
> 基线：代码 HEAD `5675ad3`（工作区含 owner 未提交改动）；Linux；CPython 3.13.15；
> pydantic-ai-slim 2.41.0、openai 3.13.0、httpx2 2.12.0；pytest 9.1.1。

## 切片状态

| 切片 | 状态 | 最小证据 | 缺口／待办 |
|---|---|---|---|
| S4-01 | **已完成**（离线断言 + 真实最小联调） | `pytest tests/test_stage4_harness_offline.py tests/test_stage4_sdk_retries_offline.py tests/test_stage4_runtime_contract.py tests/test_stage4_smoke_preflight.py -q` → 44 passed；`uv lock --check` → 38 包一致 | 真实流式断线、真实 HTTP 分类／`finish_reason` 未验证；120s／300s 时限与输出上限实现归 S4-05 |
| S4-02 | **已完成** | `pytest tests/test_stage4_run_service.py -q` → 14 passed（幂等先于 busy、冲突不落库、并发唯一活跃 Run、手动重试、`running`→`failed`、重启恢复与回滚、lifespan 装配） | 执行驱动、取消与 draining 当时未实现，已由 S4-03 补齐 |
| S4-03 | **已完成**（离线可控阻塞桩） | `pytest tests/test_stage4_run_task.py -q` → 9 passed（pending/running 取消、终态重复取消、cancel/complete 与 cancel/fail 竞态、draining busy、无观察者、无泄漏） | SSE 断开等价物只有「无观察者仍推进到 completed」留证（SSE 本身归 S4-07）；摘要与草稿就绪通知的取消断言归 S4-06/S4-07；未分类异常不映射错误码（Run 停 `running`，重启恢复收口） |
| S4-04 | **已完成（离线用例全绿；独立复审通过）**（离线桩模型 + 临时库） | 2026-09-12：`backend/.venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 760 passed（exit 0）；同目录 `-m pytest -q`（全量收藏含旧 `agent_core` 等用例）→ 1042 passed（exit 0）；定向命令与结果见「S4-04b 修补证据」一节 | 已实现：肌群字段与 24 行映射迁移（013）、安排创建与确认两处确定性安全复核、四种处置快照、长期调整显式同意、长期修订以当前 payload 为基线（只改受影响条目、档案补丁不能代替计划真实变化）、生效日生成时固定、迟到确认不补确认日前日程/不进分母/整窗已过在创建与确认两处拒绝、删除替换旁记录口径相等门（跨自重／外加负重允许，承载不了负荷转未校准）、待确认档案草稿与 C 层消息兜底纳入安全投影、同 Run 处方草稿创建（`propose_plan_draft`／`propose_arrangement_draft`）命中即不落草稿；只读 `GET /api/plan/guidance` 只服务正式事实、不携带聊天 Run 状态（边界已记录、行为不变）；已完成：独立复审（2026-09-12 两次静态复审均 REVIEW_ACCEPTED、无 P0/P1；`pyright` 未跑、复审未复跑测试、测试文件基线不可 diff）；B（结构化评估／必做步骤）经用户拍板暂缓、未实现（C 不解析否定句、不覆盖「功能受限」，已知局限）；S4-05 起未开始；真实 Provider 工具调用未验证 |
| S4-05 | 待办 | — | 依赖 S4-01/03/04；预算须计入思考 token——可见 `max_tokens` 不是总输出上界 |
| S4-06 | 待办 | — | 依赖 S4-04/05；压缩未实现 |
| S4-07 | 待办 | — | SSE／对话 API 未接入（传输拼写见 `../stage4.md` §6） |
| S4-08 | 待办 | — | 一键重算与显式复盘未实现 |
| S4-09 | 待办 | — | 联调与全量回归未开始；Windows 同版本证据将另写 `S4-evidence-windows.md` |

## S4-01／S4-02 关键证据

- 依赖（2026-09-12 用户授权）：`openai>=3.8,<4` 已加入并锁定进 `uv.lock`（安装 3.13.0）；
  同组合下 `pydantic-ai-slim 2.41.0`、`httpx2 2.12.0` 未变（`uv lock --check` 无漂移）。
- SDK 隐式重试为 0（回环假服务端，无真实请求）：生产客户端 HTTP 500 → 恰好 1 次发送；
  对照组 SDK 默认 `max_retries=2` → 3 次（证明断言非空）；框架路径同样 1 次。
- Harness 四类离线断言：纠错请求计入 `RunUsage.requests`；框架工具预算 per-tool 不叠加且每次尝试可见；
  取消在纠错请求前生效（不启动新尝试）；写入后报错需状态核对（框架无幂等层）。
- 模型 profile 真实性：框架按 `deepseek-v4-*` 前缀推断，会把 `deepseek-flash` 误报为不支持思考；
  目录覆盖为 `supports_thinking=True`、`openai_reasoning_enabled_by_default=True`、
  `thinking_always_enabled=False`（Provider 两种模式都支持；本阶段不提供按 Run 开关、不传关闭参数），
  窗口 1,000,000。
- 普通失败 `running`→`failed`：单事务写 `error_code` 与失败事件；`pending` 与全部终态拒绝且不留事件；
  `interrupted_by_restart` 只由启动恢复写入。
- 全量回归：`pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 716 passed（exit 0）；
  `ruff` 全通过；`pyright` 仅剩既有 `tests/test_stage3_plan_reads.py:112`（未改动文件）。

## S4-03 关键证据（离线可控阻塞桩，无真实模型请求）

- 执行驱动 `runtime/run_task.py`：`pending→running→唯一终态`；成功在 `complete_run` 同事务写框架消息与 `completed`，可分类失败抛 `ExecutionFailure`（限普通失败码）后走 `fail_run`。
- 竞态用可控阻塞桩 + 门控的 repo 写入确定性地让取消先提交：`cancel/complete`、`cancel/fail` 两种次序下均只有一个终态，落败一方得到 `RunStateConflict` 被丢弃；取消后不写迟到 Assistant 成功消息、不追加失败事件。
- draining：底层调用吞掉取消未实际退出时 `active_run_id` 仍非空；库内已无 `pending/running`，新请求仍 `ConversationBusy`（同一 `client_request_id` 幂等重发仍先于 busy 返回已有 Run）；退出后名额释放、新请求可创建；占用期间再次 `start` 抛 `RuntimeError`，无静默排队。
- 取消后零追加写入：除 `user_request` 与 `cancelled` 事件外无消息/事件；已发生业务副作用代理事件保留，不写「已回滚」痕迹；取消后不启动第二次尝试（桩内尝试计数为 1）。
- 无泄漏：每个场景断言名额空闲且无新增存活 asyncio 任务；未分类异常向上抛、Run 停 `running`（不发明错误码，由启动恢复收口）。
- 命令（exit 0）：`pytest tests/test_stage4_run_task.py -q` → 9 passed；`pytest tests/test_stage4_run_task.py tests/test_stage4_run_service.py tests/test_stage4_runtime_contract.py -q` → 36 passed；全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 725 passed；`ruff check` 与 `ruff format --check` 本片改动文件通过；`pyright` 本片改动文件 0 错误。

## S4-04 关键证据（离线桩模型 + `tmp_path` 临时库，无真实模型请求）

- **上下文**（`runtime/context.py`）：当前事实每 Run 从应用层重读；正式档案读取前后夹住
  `PlanReadService.read_current_plan_guidance`，仅在业务版本一致时采用，避免并发确认造成档案与计划安全复核撕裂；
  同时查询会话草稿当前状态，使失败/取消前已经落盘的草稿操作在下一轮可核实恢复。事实作为本次唯一一条
  `SystemPromptPart` 注入；注入文本与 `facts_prompt(当刻事实)` 逐字相同（测试断言）。历史用
  `ModelMessagesTypeAdapter` 原生往返（不造第二套 transcript 格式）；反序列化时剔除历史里的旧系统事实部件、
  排除 `partial` 部分回答；未完成（failed/cancelled）Run 保留用户请求事实并加 `[系统标注]` 中断标注。
  只把 `result.new_messages()` 交给 `complete_run`，不重复回写已加载历史。
- **工具**（`runtime/tools.py`）：只读查询 4 个（计划指导、训练记录、周完成率、PR）+ Pending 草稿创建 4 个
  （`profile_update`/`plan`/`training_record`/`arrangement`）。草稿一律复用 Stage 3 草稿服务（校验与事务语义原样
  复用，本层不复制）；计划草稿处方由确定性生成器 `generate_ppl_plan` 产出，阻断时返回 `block.code`（不给处方、
  不落库）；领域/应用层拒绝翻成「需追问」结果，存储与未知异常照旧上报。
- **装配与执行**（`runtime/agent_factory.py`）：`build_agent` 注册 8 个工具（清单单一来源 `agent_tool_functions`），
  `retries={"tools": 0, "output": 0}`（框架默认 per-tool 为 1，本片显式归零，不留隐式额外请求）；
  `build_run_work` 即 S4-03 的 `work`：读用户请求应用事实 → 读当前事实 → 载历史 → 注入事实 → 执行 → 返回新消息。
- **取消兼容**：工具在执行任何读取/写入前检查驱动的取消标记，取消后抛 `CancelledError`；集成测试证明取消后
  不落迟到框架消息、不新建草稿、名额释放，且下一次请求保留被取消的请求并带中断标注。
- **最小集成（守卫收窄）**：`tests/test_stage1_profile_write.py` 的「runtime 全禁引用档案领域」按该文件既有
  「显式扩展、不删测试、不放宽」口径收窄为白名单 `{context.py, tools.py}`（原文注释即写明 runtime 归 Stage 4）；
  档案写入旁路守卫与唯一版本推进点守卫未改。
- 审查修复的确定性复现（修复前）：取消 Run 前已有 1 条已落盘草稿，但下一轮上下文工具结果数为 0；
  两次独立事实读取间提交正式档案后，投影出现 `profile context_version=2`、`guidance context_version=3`。
  修复后新增测试分别锁定草稿状态投影与同版本读取。
- 命令与结果（均 exit 0）：`pytest tests/test_stage4_agent_wiring.py -q` → 12 passed；
  `pytest tests/test_stage4_agent_wiring.py tests/test_stage4_run_task.py tests/test_stage4_run_service.py
  tests/test_stage4_runtime_contract.py tests/test_stage3_plan_reads.py tests/test_stage3_plan_drafts.py
  tests/test_stage3_arrangement_confirm.py tests/test_stage3_record_drafts.py tests/test_stage3_business_api.py -q`
  → 119 passed（修复前证据）；仓库根目录 `pytest backend/tests/test_stage4_agent_wiring.py -q` → 10 passed（修复前证据；能力旁路扫描的
  源码读取按 `Path(__file__).resolve().parents[1]` 锚定，不依赖 pytest 工作目录）；全量
  `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 737 passed（审查修复后；无新增
  skip/xfail）；`pytest tests/test_stage4_agent_wiring.py tests/test_stage4_run_task.py tests/test_stage4_run_service.py -q`
  → 35 passed；`ruff check` 与 `ruff format --check` 修复文件通过；`pyright` 修复文件 0 错误；LSP 诊断 0。
- 未在本片覆盖（不得当成已验证）：框架 per-tool 纠错预算（本片置 0，归 S4-05）、超时与 20 次请求预算、
  压缩、SSE 投影、真实 Provider 的工具调用与流式行为。

## 真实 `deepseek-flash` 最小联调（2026-09-12 授权，累计上限 USD 10）

| 项 | 值 |
|---|---|
| 请求数（累计） | 3（单轮、无工具、非敏感提示；均 `requests=1`） |
| model id | 请求 `deepseek-flash`／服务端返回 `deepseek-flash`（provider `deepseek`） |
| profile | 窗口 1,000,000；`supports_thinking=true`、`openai_reasoning_enabled_by_default=true`、`thinking_always_enabled=false`；响应含 `ThinkingPart`（服务端默认开启思考） |
| SDK `max_retries` | 0 |
| usage（输入／输出 tokens） | 37/23、37/19、37/15 |
| 费用字段 | `usage.cost` 为 `null`（genai-prices 不含该 id）→ 手算 |
| 定价依据 | 官方 Models & Pricing 页面（2026-09-12 只读核对）：输入 cache miss 峰 $0.30／谷 $0.15、输出 峰 $1.20／谷 $0.60（USD per 1M）；峰时段 01:00–04:00、06:00–10:00 UTC 周一至五；取峰价作上界 |
| 估算花费 | 累计实际 ≈ **$0.000102**；单次预检上界 $0.5358（模型总输出上限 384,000 + 有效输入上限 250,000 取峰价） |
| USD 10 上限 | **符合**（累计实际与累计预检上界 $0.535873 均低于上限；低预算场景 fail-closed 已验证） |
| 脚本／凭据 | `backend/scripts/deepseek_smoke.py`；凭据仅从环境变量读取，未打印／未落盘（78 个文件扫描无凭据泄漏） |

## S4-04b 修补证据（2026-09-12；实现与 C 收尾；独立复审通过；未实现项不当成已验证）

- **基线**：本片开工前把全部已改／未跟踪文件（62 个）快照到 `/tmp/s4-04b-baseline/`（含 `_changed-files.txt` 与 `_md5.txt`）；上一片修复前副本 `/tmp/s4-04/repair-baseline/` 保留作累计对照。本片未做任何 reset/checkout/clean/stash/commit/push，owner 未提交改动原样保留。
- **P1-1 长期修订可达且以当前 payload 为基线**：新增 `domain/plan/service.revise_plan_payload` + `rules.plan_item_revision`（keep／deload／equivalent_replace／local_skip；未列出条目、训练日集合与循环逐字保留），`runtime/tools.propose_plan_draft` 新增 `adjustments` 并改走该接缝；`app/plan_drafts.require_long_term_revision` 删除「有档案补丁即可”豁免，payload 与基线相同一律拒绝（补丁不能代替计划真实变化）。定向用例断言：未受影响条目与训练日逐字相同、受影响训练日内部确有真实变化、独立与受限组合两种情形各一次确认只推进一次 `context_version`。
- **P1-2 替换等价门**：`rules.replacement_violations` 删除旁记录口径（`record_type`）相等门；修订路径的 `equivalent_replace` 直接换成替代动作身份，能承载才照抄负荷，承载不了转 `NeedsCalibration`（跨自重→外加负重已测），替代动作仍需映射三类处方口径。
- **P1-3 迟到确认**：`app/confirm._commit_pending_plan` 与 `app/plan_drafts.create_plan_draft` 在两处要求生效范围尚未整体过去（整窗已过拒绝）；日程从 `max(starts_on, 业务日期)` 投影（确认日当天保留、确认日前不落日程、不进分母），`starts_on`／`review_on` 行字段不变；定向用例断言确认==开始、确认>开始无前置日程、确认>=复核拒绝、旧日程已过去／存储锁定不被取消、未来未锁定才取消。
- **安全投影（已拍部分）**：会话内待确认档案草稿的拟议条件（含限制与红旗）纳入 guidance 安全复核（`PlanReadService.read_current_plan_guidance(safety_profile=…)`），未确认期间 `payload=None`；`_FACTS_HEADER` 同步路由规则与安全投影口径。
- **C 层文本兜底（2026-09-12 用户拍板）**：仅扫当前 Run 最新用户消息，精确子串、独立命中即本 Run 强制 safety 不可用（注入事实与 `read_plan_guidance` 两条路），模型仍可追问／说明。终版词表（10 条，不含「功能受限」）：胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛、疼痛持续加重、明显肿胀、卡锁、关节失稳。负例已测不命中：普通肌肉酸痛／肌肉酸痛／酸痛／无痛且无功能异常的关节异响。已知局限（如实记录）：不解析否定句（「没有麻木」仍会命中并保守阻断）、不覆盖正本「功能受限」、无语义理解；**B（结构化评估／必做步骤）按用户拍板暂缓、未实现**，不得把 C 当成 B 的替代保证。命中项以 `message` 来源并入安全复核红旗结论（`PlanSafetyRecheck.red_flags`）：注入事实与 `read_plan_guidance` 两条路的 `safety.red_flag_blocked` 均为 `true`、`payload=None`，投影只读不落库（不推进 `context_version`）。
- **测试变更**：新增 `tests/test_stage4_s4_04_repair.py` 15 例（共 21 例）与 `tests/test_migrations.py` 的 013 失败回滚 1 例；按已拍决策改变的合同修正 3 处旧断言（plan_confirm 新版日程集合、arrangement_confirm 替换确认日移入窗口内、agent_wiring 受限组合草稿补上真实 `adjustments`）；不删测试、不加 skip/xfail、不放宽守卫（Stage 1 runtime 档案引用白名单仍为 `{context.py, tools.py}`，消息扫描入口放白名单内的 `context.py`）。
- **命令与结果（均 2026-09-12 本机，exit 0）**：`backend/.venv/bin/python -m pytest tests/test_stage4_s4_04_repair.py -q` → 21 passed；`backend/.venv/bin/python -m pytest tests/test_stage4_agent_wiring.py tests/test_stage3_plan_drafts.py tests/test_stage3_plan_confirm.py tests/test_stage3_plan_reads.py tests/test_stage3_arrangement_confirm.py tests/test_migrations.py tests/test_stage4_s4_04_repair.py -q` → 101 passed；`backend/.venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 760 passed（exit 0）；`backend/.venv/bin/python -m pytest -q`（全量收藏，含旧 `agent_core` 等用例）→ 1042 passed（exit 0）；`ruff check`／`ruff format --check` 本轮改动文件（`domain/profile/safety.py`、`domain/plan/service.py`、`runtime/context.py`、`runtime/tools.py`、`runtime/agent_factory.py`、`app/plan_reads.py`）全部通过（exit 0）；`pyright` 本轮未重跑（`backend/.venv/bin` 内无 pyright），上一轮记录的 0 errors 待独立复审复核。未跑全量 ruff/pyright，真实 Provider、SSE、S4-05–09、Windows 均未验证。
- **状态**：本片修复完成（`pytest tests -q` 全绿 760 passed、`pytest -q` 全量收藏 1042 passed），其后 S4-04c 收尾经独立复审通过（结论见 S4-04d 节）；已知历史误写日期 2026-09-13 已按官方业务日期 2026-09-12 更正。

## S4-04d 修补证据（2026-09-12；实现；独立复审通过；未实现项不当成已验证）

- **基线**：改动前快照 `/tmp/s4-04d-baseline/`（含 `git-status-before.txt` 与受影响文件 md5）；owner 未提交改动原样保留，本片未做 reset/checkout/clean/stash/commit/push。
- **C 覆盖面扩展到同 Run 处方草稿创建（2026-09-12 用户拍板）**：`runtime/tools.py` 的 `propose_plan_draft`（首次建档／长期修订／受限组合三条路由）与 `propose_arrangement_draft` 在 `_require_active()` 之后、任何解码与读取之前检查 `ToolIdentity.message_red_flags`；命中即复用既有安全阻断结果形状（`_blocked` ＋ `PlanGenerationBlocked(code="red_flag", red_flags=命中词)`，辅助函数 `_message_red_flag_block`），返回 `created=false`、`needs_user_input=true`、`block.code="red_flag"`，不落草稿行、无处方。记录草稿与档案草稿不变；确认路径原有正式安全复查不变；未引入跨 Run 消息状态、未重扫文本。
- **只读 guidance 接口边界（不改行为）**：`GET /api/plan/guidance`（`api/routes_readonly.py` → `app/plan_reads.py`）只服务正式事实（含待确认档案草稿投影），不携带聊天 Run 的消息兜底状态；聊天内 C 层命中只经 Run 工具面（注入事实与 `read_plan_guidance`）生效，边界已在 `design-decisions.md`／`architecture/04`／`08` 记明。
- **测试变更**：`tests/test_stage4_s4_04_repair.py` 新增 4 例（首次建档阻断；长期修订与受限组合阻断且零计划草稿行；当次安排阻断且零安排草稿行；非命中消息计划与安排草稿均照常创建），该文件当前 26 例（本片新增 4 例，未逐条核对改动前计数）全绿。终版词表与负例沿用上一节，未新增词。
- **命令与结果（均 2026-09-12 本机）**：`backend/.venv/bin/python -m pytest tests -q` → 764 passed（exit 0，17.7–18.1 秒两次一致）；`backend/.venv/bin/python -m pytest tests/test_stage4_s4_04_repair.py -q -k "message_red_flag_blocks_first_time or message_red_flag_blocks_long_term or message_red_flag_blocks_arrangement or benign_message_keeps_draft"` → 4 passed；`ruff check runtime/tools.py tests/test_stage4_s4_04_repair.py` 与 `ruff format --check` 同名文件 → 全部通过（exit 0）。真实 Provider、SSE、S4-05–09 未验证。
- **状态**：本片修复完成（全量 `pytest tests -q` 764 passed、ruff 改动文件干净），并经独立复审 **REVIEW_ACCEPTED**（静态复审、无 P0/P1；备注：`pyright` 未跑、复审未复跑测试、测试文件基线不可 diff；P2 文档计数措辞已按复审建议订正）。

## 缺口（不得当成已验证）

1. S4-05–S4-09 未实现：Harness 预算与纠错、压缩、SSE/对话 API、一键重算与显式复盘、联调与全量发布验收均无证据
   （S4-04 的工具接线与上下文已完成，其证据见上节）。
2. 思考 token 计入输出（实测 1 个词的回答消耗 15–23 输出 token）：S4-05 不得把可见 `max_tokens`
   当作总输出上界。
3. 真实调用只覆盖单轮、无工具、小输出；多轮、工具调用、压缩、失败重试的真实 Provider 行为未验证。
4. Windows 人工验收未执行（另写 `S4-evidence-windows.md`，未创建前即为未执行）。
5. 一次非复现 flake：`tests/test_app.py::test_lifespan_runtime_exception_closes_connection` 曾在一次
   全量运行中失败，随后单跑、按文件跑与连续 3 次全量运行均通过；根因未定位。
