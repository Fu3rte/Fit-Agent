# Stage 4 证据（Agent 运行时）

> 本文件只记录**证据**：切片状态、最小命令与结果、真实调用用量与花费、缺口。
> 契约与验收正本在 `../stage4.md`（§5 切片、§6 前端契约、§7 完成定义）与 `../../architecture/08-agent-runtime.md`；此处不复制契约、不贴原始日志或代码。
> 基线：代码 HEAD `5675ad3`（工作区含 owner 未提交改动）；Linux；CPython 3.13.15；pydantic-ai-slim 2.41.0、openai 3.13.0、httpx2 2.12.0；pytest 9.1.1。

## 切片状态

| 切片 | 状态 | 最小证据 | 缺口／待办 |
|---|---|---|---|
| S4-01 | **已完成**（离线断言＋真实最小联调） | `pytest tests/test_stage4_{harness_offline,sdk_retries_offline,runtime_contract,smoke_preflight}.py -q` → 44 passed；`uv lock --check` → 38 包一致 | 真实流式断线、真实 HTTP 分类／`finish_reason` 未验证；120s／300s 时限与输出上限实现归 S4-05 |
| S4-02 | **已完成** | `pytest tests/test_stage4_run_service.py -q` → 14 passed（幂等先于 busy、冲突不落库、并发唯一活跃 Run、手动重试、`running`→`failed`、重启恢复与回滚、lifespan 装配） | 执行驱动、取消与 draining 当时未实现，已由 S4-03 补齐 |
| S4-03 | **已完成**（离线可控阻塞桩） | `pytest tests/test_stage4_run_task.py -q` → 9 passed（pending/running 取消、终态重复取消、cancel/complete 与 cancel/fail 竞态、draining busy、无观察者、无泄漏） | SSE 断开等价物只有「无观察者仍推进到 completed」留证（SSE 归 S4-07）；摘要与草稿就绪通知的取消断言归 S4-06/S4-07；未分类异常不映射错误码（Run 停 `running`，重启恢复收口） |
| S4-04 | **已完成**（离线用例全绿；独立复审通过；离线桩模型＋临时库） | 2026-09-12：`backend/.venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 760 passed（exit 0）；同目录 `-m pytest -q`（全量收藏，含旧 `agent_core` 等用例）→ 1042 passed（exit 0）；定向命令见「S4-04b／S4-04d」 | 已实现：肌群＋24 行映射迁移（013）；安排创建与确认两处确定性安全复核；四种处置快照；长期调整显式同意；生效日生成时固定；其余实现（长期修订以当前 payload 为基线、替换等价门、迟到确认、安全投影、C 层兜底、同 Run 草稿命中即阻断、只读 guidance 边界）见「S4-04b／S4-04d」；独立复审 2026-09-12 两次静态 REVIEW_ACCEPTED、无 P0/P1（`pyright` 未跑、未复跑测试、测试文件基线不可 diff）；B 经用户拍板暂缓未实现（C 不解析否定句、不覆盖「功能受限」）；S4-05 当时未开始；真实 Provider 工具调用未验证 |
| S4-05 | **已完成**（S4-05a 配置/硬边界/派生容量/Run 期冻结＋S4-05b 预算、墙钟、退避与分类纠错，均离线；P1 复审修复已入） | 2026-09-12：`backend/.venv/bin/python -m pytest tests/test_stage4_budget.py -q` → 49 passed（exit 0，11.1s）；`pytest tests/test_stage4_{budget,agent_wiring,runtime_contract,s4_04_repair,run_task,run_service,harness_config}.py tests/test_stage1_profile_write.py -q` → 174 passed（exit 0，13.7s）；`pytest tests/test_stage4_harness_config.py -q` → 42 passed | 实现、P1 修复与缺口见「S4-05a」「S4-05b」节；真实流式断线／真实 `Retry-After` 与完整 SSE 路径未验证（S4-07）；思考 token 计入输出——可见 `max_tokens` 不是总输出上界 |
| S4-06 | **已完成**（S4-06a 摘要持久化＋S4-06b 估算/压缩/投影，离线；静态复审 accepted、无 P0/P1、P2 处置见「S4-06b」节） | 2026-09-12：`backend/.venv/bin/python -m pytest tests/test_stage4_compression.py tests/test_stage4_summary_repo.py tests/test_stage4_budget.py tests/test_stage4_harness_config.py -q` → 117 passed（exit 0，12.6s）；受影响 Stage 4 回归 96 passed（exit 0）；最终门全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 882 passed（exit 0，28.55s） | 全量套件已由最终门复跑（见「S4-06a」最终门节；随后同一命令在仅换行的一次复跑中另现旧 flake 1 例，单跑通过）；全仓 `pyright` 未跑；真实 Provider 流式/usage 未验证；usage 锚点不跨 Run（未新增用量持久化）；费用账本未实现（摘要只共用请求计数/重试/超时接缝）；压缩只在 Run 起点尝试（循环中途只由容量闸挡），流式路径未接（S4-07） |
| S4-07 | 待办 | — | SSE／对话 API 未接入（传输拼写见 `../stage4.md` §6） |
| S4-08 | 待办 | — | 一键重算与显式复盘未实现 |
| S4-09 | 待办 | — | 联调与全量回归未开始；Windows 同版本证据将另写 `S4-evidence-windows.md` |

## S4-01／S4-02 关键证据

- 依赖（2026-09-12 用户授权）：`openai>=3.8,<4` 加入并锁定 `uv.lock`（装 3.13.0）；`pydantic-ai-slim 2.41.0`、`httpx2 2.12.0` 未变（`uv lock --check` 无漂移）。
- SDK 隐式重试 0（回环假服务端，无真实请求）：生产客户端 HTTP 500 → 恰好 1 次发送；对照组 SDK 默认 `max_retries=2` → 3 次（断言非空）；框架路径同样 1 次。
- Harness 四类离线断言：纠错请求计入 `RunUsage.requests`；工具预算 per-tool 不叠加且每次尝试可见；取消在纠错请求前生效（不启动新尝试）；写入后报错需状态核对（框架无幂等层）。
- 模型 profile：框架按 `deepseek-v4-*` 前缀推断，会把 `deepseek-flash` 误报为不支持思考；目录覆盖 `supports_thinking=True`、`openai_reasoning_enabled_by_default=True`、`thinking_always_enabled=False`（两种模式都支持；不提供按 Run 开关、不传关闭参数），窗口 1,000,000。
- 普通失败 `running`→`failed`：单事务写 `error_code` 与失败事件；`pending` 与全部终态拒绝且不留事件；`interrupted_by_restart` 只由启动恢复写入。
- 全量回归 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 716 passed（exit 0）；`ruff` 全通过；`pyright` 仅剩既有 `tests/test_stage3_plan_reads.py:112`（未改动文件）。

## S4-03 关键证据（离线可控阻塞桩，无真实模型请求）

- 执行驱动 `runtime/run_task.py`：`pending→running→唯一终态`；成功在 `complete_run` 同事务写框架消息与 `completed`；可分类失败抛 `ExecutionFailure`（限普通失败码）后走 `fail_run`。
- 竞态（可控阻塞桩＋门控 repo 写入让取消先提交）：`cancel/complete`、`cancel/fail` 两种次序均只有一个终态，落败方得 `RunStateConflict` 被丢弃；取消后不写迟到 Assistant 成功消息、不追加失败事件。
- draining：底层吞掉取消未退出时 `active_run_id` 仍非空、库内已无 `pending/running`，新请求仍 `ConversationBusy`（同一 `client_request_id` 幂等重发仍先于 busy 返回已有 Run）；退出后名额释放、新请求可创建；占用期间再次 `start` 抛 `RuntimeError`，无静默排队。
- 取消后零追加写入：除 `user_request` 与 `cancelled` 事件外无消息／事件；已发生业务副作用代理事件保留，不写「已回滚」痕迹；不启动第二次尝试（桩内尝试计数 1）。
- 无泄漏：每场景断言名额空闲且无新增存活 asyncio 任务；未分类异常向上抛、Run 停 `running`（不发明错误码，由启动恢复收口）。
- 命令（exit 0）：`pytest tests/test_stage4_run_task.py -q` → 9 passed；`pytest tests/test_stage4_{run_task,run_service,runtime_contract}.py -q` → 36 passed；全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 725 passed；`ruff check`＋`ruff format --check` 本片改动文件通过；`pyright` 本片改动文件 0 错误。

## S4-04 关键证据（离线桩模型＋`tmp_path` 临时库，无真实模型请求）

- **上下文**（`runtime/context.py`）：当前事实每 Run 从应用层重读；正式档案读取前后夹 `PlanReadService.read_current_plan_guidance`，仅业务版本一致时采用（防并发确认撕裂档案与安全复核）；同时查会话草稿当前状态，使失败／取消前已落盘草稿下一轮可核实恢复。事实为本次唯一一条 `SystemPromptPart`，注入文本与 `facts_prompt(当刻事实)` 逐字相同（断言）。历史经 `ModelMessagesTypeAdapter` 原生往返（不造第二套 transcript）；反序列化剔除旧系统事实部件、排除 `partial`；failed/cancelled Run 保留用户请求事实并加 `[系统标注]`；只把 `result.new_messages()` 交给 `complete_run`，不重复回写历史。
- **工具**（`runtime/tools.py`）：只读 4 个（计划指导、训练记录、周完成率、PR）＋Pending 草稿创建 4 个（`profile_update`／`plan`／`training_record`／`arrangement`）；草稿复用 Stage 3 服务（校验与事务语义原样复用）；计划处方由 `generate_ppl_plan` 产出，阻断时返回 `block.code`（不给处方、不落库）；领域／应用层拒绝翻成「需追问」，存储与未知异常照旧上报。
- **装配与执行**（`runtime/agent_factory.py`）：`build_agent` 注册 8 个工具（清单单一来源 `agent_tool_functions`），`retries={"tools": 0, "output": 0}`（框架默认 per-tool 1，显式归零）；`build_run_work` 即 S4-03 的 `work`：读用户请求应用事实 → 读当前事实 → 载历史 → 注入事实 → 执行 → 返回新消息。
- **取消兼容**：工具在任何读取／写入前检查取消标记，取消后抛 `CancelledError`；集成测试证明取消后不落迟到框架消息、不新建草稿、名额释放，下一次请求保留被取消请求并带中断标注。
- **最小集成（守卫收窄）**：`tests/test_stage1_profile_write.py` 的「runtime 全禁引用档案领域」按既有「显式扩展、不删测试、不放宽」口径收窄为白名单 `{context.py, tools.py}`（注释即写明归 Stage 4）；档案写入旁路与唯一版本推进点守卫未改。
- 修复前确定性复现：取消 Run 前已有 1 条已落盘草稿，下一轮上下文工具结果数为 0；两次事实读取间提交正式档案后投影出现 `profile context_version=2`、`guidance context_version=3`；修复后新增测试锁定草稿状态投影与同版本读取。
- 命令（均 exit 0）：`pytest tests/test_stage4_agent_wiring.py -q` → **12 passed**；`pytest tests/test_stage4_{agent_wiring,run_task,run_service,runtime_contract}.py tests/test_stage3_plan_{reads,drafts}.py tests/test_stage3_arrangement_confirm.py tests/test_stage3_{record_drafts,business_api}.py -q` → **119 passed**（修复前）；仓库根 `pytest backend/tests/test_stage4_agent_wiring.py -q` → **10 passed**（修复前；旁路扫描按 `Path(__file__).resolve().parents[1]` 锚定，不依赖 cwd）；全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → **737 passed**（修复后；无新增 skip/xfail）；`pytest tests/test_stage4_{agent_wiring,run_task,run_service}.py -q` → **35 passed**；ruff 修复文件通过；pyright 修复文件 0 错误；LSP 0。
- 未覆盖（不得当成已验证）：框架 per-tool 纠错预算（本片置 0，归 S4-05）、超时与 20 次请求预算、压缩、SSE 投影、真实 Provider 工具调用与流式行为。

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

- **基线**：开工前把 62 个已改／未跟踪文件快照到 `/tmp/s4-04b-baseline/`（`_changed-files.txt`＋`_md5.txt`）；`/tmp/s4-04/repair-baseline/` 保留作累计对照；未做 reset/checkout/clean/stash/commit/push，owner 改动原样保留。
- **P1-1 长期修订可达且以当前 payload 为基线**：新增 `domain/plan/service.revise_plan_payload`＋`rules.plan_item_revision`（keep／deload／equivalent_replace／local_skip；未列出条目、训练日集合与循环逐字保留），`runtime/tools.propose_plan_draft` 新增 `adjustments` 并改走该接缝；`app/plan_drafts.require_long_term_revision` 删除「有档案补丁即可」豁免，payload 与基线相同一律拒绝（补丁不能代替计划真实变化）。定向用例：未受影响条目与训练日逐字相同、受影响训练日内部确有真实变化、独立与受限组合各一次确认只推进一次 `context_version`。
- **P1-2 替换等价门**：`rules.replacement_violations` 删除旁记录口径（`record_type`）相等门；修订路径的 `equivalent_replace` 直接换成替代动作身份，能承载才照抄负荷，承载不了转 `NeedsCalibration`（跨自重→外加负重已测）；替代动作仍需映射三类处方口径。
- **P1-3 迟到确认**：`app/confirm._commit_pending_plan` 与 `app/plan_drafts.create_plan_draft` 两处要求生效范围尚未整体过去（整窗已过拒绝）；日程从 `max(starts_on, 业务日期)` 投影（确认日当天保留、确认日前不落日程、不进分母），`starts_on`／`review_on` 行字段不变；定向用例：确认==开始、确认>开始无前置日程、确认>=复核拒绝、旧日程已过去／存储锁定不被取消、未来未锁定才取消。
- **安全投影（已拍部分）**：会话内待确认档案草稿的拟议条件（含限制与红旗）纳入 guidance 安全复核（`PlanReadService.read_current_plan_guidance(safety_profile=…)`），未确认期间 `payload=None`；`_FACTS_HEADER` 同步路由规则与安全投影口径。
- **C 层文本兜底（2026-09-12 用户拍板）**：仅扫当前 Run 最新用户消息，精确子串、独立命中即本 Run 强制 safety 不可用（注入事实与 `read_plan_guidance` 两条路），模型仍可追问／说明。终版词表 10 条（不含「功能受限」）：胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛、疼痛持续加重、明显肿胀、卡锁、关节失稳。负例不命中：普通肌肉酸痛／肌肉酸痛／酸痛／无痛且无功能异常的关节异响。已知局限：不解析否定句（「没有麻木」仍会命中并保守阻断）、不覆盖正本「功能受限」、无语义理解；**B（结构化评估／必做步骤）按用户拍板暂缓、未实现**，不得把 C 当成 B 的替代保证。命中项以 `message` 来源并入安全复核红旗结论（`PlanSafetyRecheck.red_flags`）：两条路的 `safety.red_flag_blocked` 均为 `true`、`payload=None`，投影只读不落库（不推进 `context_version`）。
- **测试变更**：新增 `tests/test_stage4_s4_04_repair.py` 15 例（共 21 例）与 `tests/test_migrations.py` 的 013 失败回滚 1 例；按已拍决策修正 3 处旧断言（plan_confirm 新版日程集合、arrangement_confirm 替换确认日移入窗口内、agent_wiring 受限组合草稿补上真实 `adjustments`）；不删测试、不加 skip/xfail、不放宽守卫（白名单仍为 `{context.py, tools.py}`，消息扫描入口放白名单内的 `context.py`）。
- **命令与结果（2026-09-12 本机，exit 0）**：`pytest tests/test_stage4_s4_04_repair.py -q` → 21 passed；`pytest tests/test_stage4_{agent_wiring,s4_04_repair}.py tests/test_stage3_plan_{drafts,confirm,reads}.py tests/test_stage3_arrangement_confirm.py tests/test_migrations.py -q` → 101 passed；全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 760 passed；`pytest -q`（全量收藏，含旧 `agent_core` 等）→ 1042 passed；`ruff check`／`ruff format --check` 本轮改动文件（`domain/profile/safety.py`、`domain/plan/service.py`、`runtime/context.py`、`runtime/tools.py`、`runtime/agent_factory.py`、`app/plan_reads.py`）全部通过；`pyright` 本轮未重跑（`backend/.venv/bin` 内无 pyright），上一轮 0 errors 待独立复审复核。未跑全量 ruff／pyright；真实 Provider、SSE、S4-05–09、Windows 未验证。
- **状态**：本片修复完成（全量 760 passed、全量收藏 1042 passed），其后 S4-04c 收尾经独立复审通过（结论见 S4-04d 节）；已知历史误写日期 2026-09-13 已按官方业务日期 2026-09-12 更正。

## S4-04d 修补证据（2026-09-12；实现；独立复审通过；未实现项不当成已验证）

- **基线**：改动前快照 `/tmp/s4-04d-baseline/`（含 `git-status-before.txt` 与受影响文件 md5）；owner 改动原样保留，未做 reset/checkout/clean/stash/commit/push。
- **C 覆盖面扩展到同 Run 处方草稿创建（2026-09-12 用户拍板）**：`runtime/tools.py` 的 `propose_plan_draft`（首次建档／长期修订／受限组合三条路由）与 `propose_arrangement_draft` 在 `_require_active()` 之后、任何解码与读取之前检查 `ToolIdentity.message_red_flags`；命中即复用既有安全阻断形状（`_blocked`＋`PlanGenerationBlocked(code="red_flag", red_flags=命中词)`，辅助函数 `_message_red_flag_block`），返回 `created=false`、`needs_user_input=true`、`block.code="red_flag"`，不落草稿行、无处方。记录／档案草稿不变；确认路径原有正式安全复查不变；未引入跨 Run 消息状态、未重扫文本。
- **只读 guidance 接口边界（不改行为）**：`GET /api/plan/guidance`（`api/routes_readonly.py` → `app/plan_reads.py`）只服务正式事实（含待确认档案草稿投影），不携带聊天 Run 的消息兜底状态；聊天内 C 层命中只经 Run 工具面（注入事实与 `read_plan_guidance`）生效，边界已在 `design-decisions.md`／`architecture/04`／`08` 记明。
- **测试变更**：`tests/test_stage4_s4_04_repair.py` 新增 4 例（首次建档阻断；长期修订与受限组合阻断且零计划草稿行；当次安排阻断且零安排草稿行；非命中消息计划与安排草稿均照常创建），该文件当前 26 例（未逐条核对改动前计数）全绿；终版词表与负例沿用上节，未新增词。
- **命令与结果（2026-09-12 本机）**：`pytest tests -q` → 764 passed（exit 0，17.7–18.1 秒两次一致）；`pytest tests/test_stage4_s4_04_repair.py -q -k "message_red_flag_blocks_first_time or message_red_flag_blocks_long_term or message_red_flag_blocks_arrangement or benign_message_keeps_draft"` → 4 passed；`ruff check runtime/tools.py tests/test_stage4_s4_04_repair.py` 与 `ruff format --check` 同名文件 → 通过（exit 0）。真实 Provider、SSE、S4-05–09 未验证。
- **状态**：本片修复完成（全量 764 passed、ruff 改动文件干净），并经独立复审 **REVIEW_ACCEPTED**（静态复审、无 P0/P1；备注：`pyright` 未跑、复审未复跑测试、测试文件基线不可 diff；P2 文档计数措辞已按复审建议订正）。

## S4-05a 证据（2026-09-12；Harness 配置、硬边界、派生容量与 Run 期冻结）

- **范围**：08 8.5「本地配置 + 硬边界」、08「Stage 4 已拍 Harness 策略」参数表与「容量、估算与溢出」。只做 S4-05a：配置加载、边界/交叉校验、派生容量、每 Run 冻结；不实现预算调度、错误分类重试、超时执行与压缩（S4-05b／S4-06），未加 Queue／Steer／恢复框架。
- **实现**：`config.py` 新增 `<数据目录>/harness.toml`（缺省即全部默认值；严格 TOML：未知键、未知模型、类型不符（字符串／bool／整数项给小数）、越界值一律 `HarnessConfigError` 拒绝启动，不钳制）；`HARNESS_BOUNDS` 固化 7 个可配置项已拍边界（模型请求 1–24/20、工具 1–40/32、输出 512–8192/8192、Run 30–600s/300s、单请求 10–180s/120s、连接 1–30s/10s、有效输入 32768–524288/250000）；不可变 `EffectiveHarness` 派生触发点 80%、保留目标 10%、摘要输出 6144、安全余量 `max(4096, ceil(10%×估算))`，`request_timeout_bounded` 取单请求时限与 Run 剩余时间的小者；`require_capacity_invariants` 做启动交叉校验（保留目标<触发点≤上限；摘要请求上界（触发点−保留目标+旧摘要上限+提示词余量）≤上限；上限+输出预留+安全余量≤模型窗口 1,000,000）；`freeze_effective_harness` 为启动与每次 Run 开始共用入口。`api/app.py` lifespan 启动即加载+冻结，非法配置拒绝对外服务。
- **实现细节取值**：摘要请求的提示词余量正本只给公式未给数值，取 2048（已拍下限 32,768 仍满足该不变量）；`harness.toml` 位置/格式与字段名为实现细节（08 8.5）。
- **测试**：新增 `tests/test_stage4_harness_config.py` 42 例：默认值与派生值、边界表逐项 low/high 接受与 low−1/high+1 拒绝（含边界值仍过交叉校验）、类型不匹配 8 例、未知键、未知模型 5 例、TOML 语法错、容量不变量违规 4 例+合法 1 例、安全余量与请求时限边界、Run 期冻结抗后续文件变更、启动装配接受（进 `app.state.harness_config`）与拒绝。
- **首轮实现时的命令与结果（2026-09-12 本机，exit 0；当时字节随后被 pi-lens 自动改写，仅作历史）**：`pytest tests/test_stage4_harness_config.py -q` → 42 passed；`pytest tests/test_config.py tests/test_app.py tests/test_stage4_runtime_contract.py -q` → 24 passed；全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → 806 passed（20.2s；S4-04d 基线 764＋新增 42）；`ruff check`／`ruff format --check`（`config.py`、`api/app.py`、`tests/test_stage4_harness_config.py`）→ 通过。
- **工具根因与已批准修复（2026-09-12）**：pi-lens 的 `hasRuffConfig()` 在工作目录及上级中查找字面量 `[tool.ruff]`（或 `ruff.toml`／`.ruff.toml`）；`backend/pyproject.toml` 当时只有 `[tool.ruff.lint]`，因此 pi-lens 判定无项目配置并注入自带 `config/ruff/core.toml`（无 target-version），把 Python 3.11+ 标准库 `tomllib` 当成第三方，agent_end 自动改写先后两次把 `import tomllib` 移出标准库块。修复（owner 批准）：在 `backend/pyproject.toml` 的 lint 表之前加入空的 `[tool.ruff]` 表（不新增、不放宽任何规则；后续已确认无需显式 target-version），并把 `import tomllib` 放回标准库块。修复后 pi-lens 能发现项目配置（`hasRuffConfig(backend)` 为真，不再注入 core.toml），ruff 由 `requires-python >= 3.13` 推导 target-version。
- **最终字节只读验证（2026-09-12）**：`[tool.ruff]` 字面量存在且 `pyproject.toml` 可解析，`lint.select` 保持 `["E4","E7","E9","F","I"]` 未变；`ruff check --show-settings backend/config.py` → `unresolved_target_version = 3.13`（无需显式 target-version）；`ruff check --select I config.py` 通过（venv ruff 0.16.7 与 PATH ruff 0.16.6）；`ruff check`＋`ruff format --check`（`config.py`、`api/app.py`、`tests/test_stage4_harness_config.py`）通过；`pytest tests/test_stage4_harness_config.py -q` → 42 passed（1.01s）；本次未复跑全量。验证字节：`config.py` `e00523ff5b19e51c7214a04f087de5b47f2e88db3fcb698f1dd4830e95a2f36c`、`pyproject.toml` `c9a7a2866b24a05aa25345affaf66826d74f7a928b1994b6f1dabafa8c24900c`、`api/app.py` `074261f9…`、`tests/test_stage4_harness_config.py` `4e5c19bb…`、`S3-evidence-windows.md` `ce34ecf5…` 与基线一致。
- **独立静态复审（2026-09-12）**：**REVIEW_ACCEPTED**，无 P0/P1（静态审阅；复审未运行测试、ruff 或 git，命令结果按本文件记录）；P2 备注（直接构造 `HarnessConfig` 可绕过边界校验、冻结接缝尚未被 Run 消费、无效 UTF-8 报 `UnicodeDecodeError` 而非 `HarnessConfigError`、容量不变量在已拍边界内不可达），未发现直接契约冲突，未因此改源。
- **未覆盖（不得当成已验证）**：S4-05b 的 20 次请求预算、重试池 1／纠错池 2、白名单分类、退避、120s/300s 时限执行与假服务端覆盖当时未实现，已由 S4-05b 补齐（见下节）；`pyright` 未跑；真实 Provider、SSE、S4-06–09、Windows 未验证。

## S4-05b 证据（2026-09-12；统一执行预算、墙钟、退避与分类纠错；离线）

- **范围**：08 8.5/8.6「Stage 4 已拍 Harness 策略」的执行侧——把 S4-05a 冻结配置接进 S4-03 执行驱动与 S4-04 PydanticAI／工具路径；未加 Queue／Steer／事件重放／通用恢复框架／第二套消息存储。
- **单一 Run 权威**：`runtime/budget.py` 的 `RunBudget`（每 Run 一个实例）统一放行并计数：实际模型请求（普通／摘要后续／暂时故障重试／输出与工具参数纠错）共用默认 20 次预算、工具调用 32、重试池每 Run 1、纠错池每 Run 2（输出与工具参数共享，逐字对应已拍 2A）、退避与两段墙钟。`BudgetedModel`（`WrapperModel` 子类）在 `request()`／`request_stream()` 入口计数：SDK 已 `max_retries=0`，框架 `retries` 取纠错池上限（2）且每次带 `RetryPromptPart` 的追问仍由适配层扣共享池，池尽即终态失败（框架不可能多出未计数尝试）。
- **墙钟**：不新增首事件／空闲计时器（已拍 A）。单次请求总时限由适配层 `asyncio.timeout` 包住整次请求实现（不依赖传输层 read timeout），取 `min(120s, Run 剩余时间)`；Run 总时限 300s。每个新 effect（模型请求／工具／重试／纠错／退避）之前检查：取消→`CancelledError`（不启动、不映射错误码）；Run 到期→`run_timeout`；池尽→`model_request_failed`；剩余时间不够等待→`model_request_timeout`。
- **分类（只按框架异常类型／HTTP 状态／`finish_reason`／是否已向消费方产出事件）**：可重试白名单＝连接建立失败与连接期超时（框架 `ModelAPIError`、裸 `httpx2.ConnectError`）、HTTP 429／500／503，且仅在本尝试尚无输出时；`Retry-After` ≤10s 按其值等待、缺失或非法用 1s、>10s 结束而不提前重发；永久失败＝HTTP 400/401/402/404/422 及其余白名单外状态、`finish_reason` 为 content_filter／length／insufficient_system_resource／aborted／error、已输出后的流中断、框架纠错预算耗尽（`UnexpectedModelBehavior`）、`ContentFilterError`。未分类异常不发明原因码（保持 S4-03 语义）；取消原样上抛。
- **复审 P1 修复（2026-09-12，静态复审 finding）**：框架只映射 OpenAI 五个 chat finish_reason，DeepSeek 的 `aborted`／`insufficient_system_resource` 会被归一化成 `None`、原值仅留在 `provider_details['finish_reason']`（框架 `_map_provider_details` 两条路径都写）；只看归一化值会把 Provider 中断的回答当正常结束接受。修法：`require_supported_finish_reason(response)` 同时查归一化值与 `provider_details['finish_reason']` 原值（流式与非流式两处调用点都改为传整个响应）。**可复现证据**：临时禁用原值检查后（仅本地临时改写，已用 sha256 逐字节恢复：`4098e172…0511`）新增的两条 SSE 用例均 `DID NOT RAISE`（2 failed，2.45s）；恢复后全绿。另有一条机制钉用例在不经适配层的原始框架路径上断言 `stream.finish_reason is None` 且 `provider_details['finish_reason']=='aborted'`。
- **新错误码（2026-09-12 用户拍板）**：`model_request_failed` 进 08 章、`stage4.md` §6 契约与 `design-decisions.md` 增量表；`error_codes.py` 与 S4-01 契约测试同步为六个码（`conversation_busy` ＋ 五个 Run 终态原因）。不改前端。
- **写入不确定**：适配层不重发工具调用（没有工具级自动重试）；核对状态走既有只读工具，计入工具池（`BusinessTools._require_active` 每工具执行前扣减）。
- **本轮改动文件**：`runtime/budget.py`（新）、`runtime/error_codes.py`、`runtime/tools.py`、`runtime/agent_factory.py`、`tests/test_stage4_budget.py`（新）、`tests/test_stage4_agent_wiring.py`（三个 `build_run_work` 调用点补冻结配置）、`tests/test_stage4_runtime_contract.py`（封闭集合六个码）；文档：`architecture/08-agent-runtime.md`、`stage4.md`、`design-decisions.md`、本文件。
- **覆盖的离线用例（`tests/test_stage4_budget.py`，49 例）**：默认值与派生值（20／32／1／2／120s／300s）；请求池与工具池上限；重试池与纠错池独立且受限；取消与 Run 到期不启动新 effect；退避默认 1s／10s 上限／非法值／剩余时间不足；分类表（429／500／503／400／401／402／404／422／连接类／`ReadTimeout`／`ContentFilterError`／`UnexpectedModelBehavior`／未分类异常）；`finish_reason` 永久结果不重试；假服务端（回环 127.0.0.1，合成占位凭据）覆盖 500→恰好两次实际发送、503 重试成功、429 有／无 `Retry-After`、`Retry-After=60` 不重发、五个永久状态只发一次、连接被拒重试一次；**新增 SSE（text/event-stream）覆盖**：`finish_reason=aborted`／`insufficient_system_resource` 的流均终态 `model_request_failed` 且只发 1 次、`finish_reason=stop` 对照正常完成、以及原始框架路径的归一化丢值机制钉；桩模型路径的请求超时映射；流式建流阶段尚未 yield 给消费方时失败可重试（用重试池，两次）／已 yield 给消费方后即使无文本块也不重放（一次）；与 S4-03 驱动接通后的终态：请求池耗尽（`max_model_requests=1`，只发 1 次请求）→ `failed`＋`model_request_failed`，工具池耗尽（`max_tool_calls=1`，第二次工具调用被拦）→ `failed`＋`model_request_failed`，请求超时→ `failed`＋`model_request_timeout`，取消→ `cancelled` 且不启动新尝试。
- **命令与结果（2026-09-12 本机，exit 0）**：`pytest tests/test_stage4_budget.py -q` → **49 passed**（11.1s）；`pytest tests/test_stage4_budget.py tests/test_stage4_agent_wiring.py tests/test_stage4_runtime_contract.py tests/test_stage4_s4_04_repair.py tests/test_stage4_run_task.py tests/test_stage4_run_service.py tests/test_stage4_harness_config.py tests/test_stage1_profile_write.py -q` → **174 passed**（13.7s）；`ruff check runtime/ tests/test_stage4_budget.py` 与 `ruff format --check runtime/budget.py tests/test_stage4_budget.py` → 通过。修复前的旧命令（43 passed／168 passed）已被本次取代。
- **未覆盖（不得当成已验证）**：未跑全量套件与 `pyright`（本机 `backend/.venv` 内无 pyright，PATH 上的 `~/.local/bin/pyright` 未纳入本轮证据）；真实 Provider 的 `Retry-After`／断流／`finish_reason` 未验证；真实 SSE 流式路径与首块前断流的真实行为归 S4-07。
- **流式重放边界（2026-09-12 用户拍板选 1，已入 08 章「流式重放边界」与 `design-decisions.md`）**：流水交给消费方后即使尚无文本块也不重放（fail-closed，取本次失败对应的终态原因码）；非流式 `agent.run` 仍按已拍 A 在未产出输出时用重试池 1 次。首个业务块前的更高层重放由 S4-07 按真实 SSE 联调证据另行相机；本片不预建 S4-07 入口。
- **复审 P2 处置（不得当成已验证）**：① 300s Run 墙钟按已拍 08 措辞只做「到时禁止新调用」的准入闸，未加 `asyncio.timeout` 硬包整次 Run；该终态码只有预算对象级证据（假钟），无驱动级端到端用例（工具调用本身未被单独限时）。② `connect_timeout_seconds` 与可见 `max_output_tokens` 目前只在配置层与派生层存在、无运行期消费者（生产模型构造与请求模型参数的接线在 S4-07）；本片不自行接线，不计为已验证。③ `EffectiveHarness.request_timeout_bounded` 已改为 `begin_model_request` 的唯一实现，不再重复内联。④ 本片对 `architecture/08-agent-runtime.md`、`stage4.md`、`design-decisions.md` 的改动均为 owner 2026-09-12 明确授权的新码与流式边界登记（授权原文见交接消息）；无其他正本改动。
- **最终独立静态复审（2026-09-12，orchestrator 分发；P1 修复后）**：**REVIEW_ACCEPTED**、无 P0/P1；复审为静态阅读，**reviewer 未运行任何命令**、未复跑本文件记录的命令结果（reviewer 自报口径，本文件不据此声称复跑）；P2 处置见上条。

## S4-06a 证据（2026-09-12；摘要持久化 schema 与仓库契约；离线）

- **范围**：07 7.4「Stage 4 已拍：摘要持久化」——014 编号迁移两表＋`storage/summary_repo.py`（提交与读取 seam）。本片只做存储结构、提交契约与条件校验：不生成模型摘要、不估算 token、不做上下文投影、不接压缩触发（S4-06b/c），未加 Queue／Steer／事件重放／第二套消息存储。
- **结构（`014_stage4_summaries.sql`）**：`summaries(id, conversation_id, run_id, content, covered_from_seq, covered_to_seq, created_at)`＋`summary_sources(summary_id, message_id)`，索引 `(conversation_id, covered_to_seq)`；覆盖范围精确到会话消息 seq 区间，来源逐条指向 `messages` 行（外键拒绝删除仍被引用的原消息，来源不可能悬空）；不改 `messages`／`runs`／`run_events`。
- **提交契约（`SummaryRepo.commit_summary`）**：单事务内依次——生成该摘要的 Run 必须仍为 `running`（取消先提交→`RunStateConflict`，不落库、不补写任何诊断快照）；覆盖区间必须完整落在已保存消息上；覆盖必须是**会话前缀的扩展**（首条从会话最早消息起，后续包含旧覆盖且终点前移，防止投影静默丢历史）；来源必须落在覆盖区间内。任一步失败（含底层存储错误）整体回滚并把异常上抛：调用方必须终止压缩，不得降级继续。最新有效摘要＝`covered_to_seq` 最大、同终点取后写（rowid）的一条，确定性。
- **测试**：`tests/test_stage4_summary_repo.py` 8 例——提交内容/范围/来源与有效摘要读取；提交后原消息逐条不变＋被引用消息删不掉；仅 running 可提交（pending／completed 拒绝）；取消先发生→零写入且无诊断快照；提交先发生→随后取消不改写/不删除已提交摘要；两版摘要最新有效选择与旧摘要、旧来源保留；非扩展或中途起点覆盖拒绝；非法输入与不存在 Run；来源关联步骤失败整体回滚且事务可继续重试。`tests/test_migrations.py` 增 014 失败回滚用例（注入真实 014 全文＋非法 SQL：表、索引、第二张表整片回滚、版本停在 013；修复重跑用真实 014 原文，不需删库）并显式扩展表集合断言；Stage 1–3 既有表集合/迁移测试按新编号同步扩展（不删测试、不加 skip/xfail、不放宽守卫）。
- **命令与结果（2026-09-12 本机，exit 0；均在 `backend/` 下运行）**：`backend/.venv/bin/python -m pytest tests/test_stage4_summary_repo.py tests/test_migrations.py -q` → **21 passed**（0.9s）；`backend/.venv/bin/python -m pytest tests/test_stage1_schema.py tests/test_stage1_actions_seed.py tests/test_stage2_draft_storage.py tests/test_stage3_plan_migrations.py tests/test_stage3_record_migrations.py tests/test_stage3_reviews.py tests/test_stage3_stats.py tests/test_app.py tests/test_db.py tests/test_migrations.py tests/test_stage4_summary_repo.py -q` → **169 passed**（3.5s）；`backend/.venv/bin/python -m pytest tests/test_run_repo.py tests/test_db.py tests/test_stage4_run_service.py tests/test_stage4_run_task.py tests/test_migrations.py tests/test_stage4_summary_repo.py -q` → **72 passed**（2.1s）；`backend/.venv/bin/python -m ruff check` 与 `ruff format --check` 本轮改动的 7 个 `.py` 文件（`storage/summary_repo.py`、`tests/test_stage4_summary_repo.py`、`tests/test_migrations.py`、`tests/test_stage1_schema.py`、`tests/test_stage2_draft_storage.py`、`tests/test_stage3_plan_migrations.py`、`tests/test_stage3_record_migrations.py`）→ 通过。
- **未覆盖（不得当成已验证）**：摘要生成（模型调用与 6,144 输出上限）、token 估算与 200,000 触发点、上下文投影、连续摘要的模型侧输入、压缩失败/取消后的继续策略（S4-06b/c）；`pyright` 与真实 Provider 未验证；取消/提交只覆盖单锁串行下的两种次序（先取消、先提交），无 gated 交错并发用例。
- **独立静态复审（2026-09-12，orchestrator 分发）**：**REVIEW_ACCEPTED**、无 P0/P1；3 条 P2（014 注入脚本与测试说明不符——已就地修正为真实全文注入＋真实原文重跑；`SummaryRepo` 尚无生产消费者、覆盖/来源未按 kind 过滤 partial——按复审建议归 S4-06b）。复审为静态审阅、未执行命令，本文件记录的命令结果未被复审复跑。
- **最终门（parent gate）回归与修复（2026-09-12）**：parent 全量门在 `tests/test_provider_settings.py` 报 2 failed（`_SCANNED_TABLES` 未包含 014 新增表）——该守卫要求扫描清单与库内表集合逐表相等，不得放宽。修复：`_SCANNED_TABLES` 补入 `summaries`、`summary_sources`，并在 `all_text_cells` 中按既有逐表 `SELECT *` 方式把两表全部列逐单元格纳入扫描（不删断言、不加 skip/xfail、不改生产行为）。命令（本机，`backend/`）：`backend/.venv/bin/python -m pytest tests/test_provider_settings.py::test_key_never_copied_into_logs_trace_run_events_or_other_tables tests/test_provider_settings.py::test_internal_key_value_is_not_written_back_anywhere -q` → **2 passed**（0.7s，exit 0）；`timeout 220 .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → **882 passed**（28.55s，exit 0，修复后代码字节）；同一命令在 `ruff format` 仅换行后复跑一次得 **1 failed, 881 passed**，失败项为既有的非复现 flake `tests/test_app.py::test_lifespan_runtime_exception_closes_connection`（单独跑 1 passed、整文件 5 passed；按纪律不再重复长跑）；`ruff check`＋`ruff format --check` 本轮改动文件（`tests/test_provider_settings.py`）→ 通过；`pytest tests/test_provider_settings.py -q` → **12 passed**（2.5s）。
- **owner 改动**：本轮对本文件只追加本节与同步 S4-06 行／缺口计数；`S3-evidence-windows.md` 与既有 owner 未提交改动未触碰；未做 reset/checkout/clean/stash/commit/push。

## S4-06b 证据（2026-09-12；活跃 prompt 估算、旧历史安全压缩与请求投影；离线）

- **范围**：stage4.md S4-06 验收与 08「容量、估算与溢出」「压缩 A 与失败 B」。只做估算／触发点、安全摘要区间、有界收缩、条件提交启用与请求前容量闸；未加 Queue／Steer／事件重放／通用恢复框架／第二套消息存储，未实现 split-turn（已拍暂缓）；未改任何冻结产品/架构决策。
- **实现**：
  - `runtime/compression.py`（新）：`PromptEstimator` 按 `max(ceil(0.6×字符数), 最近真实 usage.input_tokens + ceil(0.6×新增字符))` 估算（未知 usage 不锚定；压缩重写上下文后锚点失效）；`request_characters` 计框架原生消息 JSON + Provider 同源工具定义（name/description/parameters）；`ordinary_request_fits`（估算输入 ≤ 有效输入上限；估算输入 + 输出预留 8,192 + 安全余量 ≤ 模型窗口）；`summary_request_fits`（估算输入 + 摘要输出上限 6,144 ≤ 有效输入上限，与启动交叉校验同一公式）；`plan_summary_range`（只摘要最老的完整交互；从最新往回保留到 10% 目标、至少保留最新一条完整交互；交互即 Run 边界，不拆工具对；摘要请求放不下时逐条收缩；缩到无可摘要返回 None）；`ContextCompressor`（同一上下文状态最多尝试一次；提交成功才替换投影；可恢复生成失败保留旧上下文；存储失败与取消上抛不降级；摘要投影显式标注为辅助历史）。
  - `runtime/context.py`：`load_conversation_interactions`（按 Run 分组：已完成 Run 原生反序列化并剔除旧系统事实、失败／取消 Run 只留用户请求 + 中断标注、`kind='partial'` 不进上下文；`after_seq` 支持「摘要 + 尾段」投影；当前 Run 排除）；原先的 whole-history 加载函数（`load_conversation_history`）已**被它取代并删除**，不是薄封装保留；S4-04 端到端用例不引用该函数名（只 import `INTERRUPTION_MARKER`／`facts_prompt`／`read_business_facts`），故未改仍全绿，仓库中已无该符号引用。来源行只登记真正进投影的行（不含 partial），即 S4-06a 复审 P2 的产品侧落实；`SummaryRepo` 现有生产消费者。
  - `runtime/budget.py`：`BudgetedModel` 每次实际请求发送前重算容量闸（超限 `context_budget_exceeded` 且不发送；无 provider 溢出应急重试，`finish_reason=length` 仍属永久结果）；成功请求记录真实 `usage.input_tokens` 与本次字符数锚点；新增 `request_summary`（共用 Run 请求计数、暂时故障重试池、取消检查、单次请求时限、Run 总时限与费用接缝；`max_tokens` = 摘要输出上限；摘要请求不更新投影锚点）；`RunBudget._require_running` 提为公开 `require_running`（无行为变化）。
  - `runtime/agent_factory.py`：Run 起点读有效摘要 → 只取覆盖终点之后的消息 → 达到触发点先压缩 → 用条件提交成功后的投影 `agent.run(user_prompt=...)`；当前事实仍每 Run 只注入一次一条系统事实部件（摘要不冒充档案／安全／计划／草稿事实）。
- **测试**：新增 `tests/test_stage4_compression.py` 18 例（缩小阈值直接构造 `EffectiveHarness`＋`tmp_path` 临时库＋脚本桩，无真实 Provider）：估算与锚点、两个容量方程、超限不发送、摘要共用请求预算、未达触发点不动历史、只摘要最老完整交互（工具调用/结果成对、部分回答不进、失败／取消请求保留中断标注、当前 Run 不压缩、当前事实不进摘要输入）、提交成功才启用的覆盖范围/来源并集/保留尾段、连续摘要前缀扩展与来源可追溯、有界收缩、无安全切点、缩到无可执行区间、可恢复生成失败保留旧上下文、同一状态不重复尝试、提交失败上抛不降级、整链路压缩后投影生效（摘要 + 保留尾段、事实仍只注入一次）、第二个 Run 的「摘要 + 尾段」不重复投影、摘要请求占用共享请求预算、summary 生成中取消零提交、最终 `context_budget_exceeded` 不发送。
- **命令与结果（2026-09-12 本机，exit 0；均在 `backend/` 下运行）**：`backend/.venv/bin/python -m pytest tests/test_stage4_compression.py tests/test_stage4_summary_repo.py tests/test_stage4_budget.py tests/test_stage4_harness_config.py -q` → **117 passed**（12.6s）；`backend/.venv/bin/python -m pytest tests/test_stage4_agent_wiring.py tests/test_stage4_run_task.py tests/test_stage4_run_service.py tests/test_stage4_runtime_contract.py tests/test_stage4_s4_04_repair.py tests/test_migrations.py tests/test_stage1_profile_write.py -q` → **96 passed**（3.7s）；`ruff check` 与 `ruff format --check` 本轮改动文件（`runtime/compression.py`、`runtime/budget.py`、`runtime/agent_factory.py`、`runtime/context.py`、`tests/test_stage4_compression.py`）→ 通过；`pyright` 同 5 文件 → **0 errors**。
- **机制钉（缺陷注射）**：把 `runtime/agent_factory.py` 的摘要覆盖过滤 `after_seq` 临时改为 `0`（其余不动），`test_next_run_projects_summary_plus_tail_without_duplicating_history` 失败（重复投影已摘要历史），恢复后单跑通过；证明该用例真的覆盖产出路径而非同义反复。
- **未覆盖（不得当成已验证）**：全量套件已由最终门复跑（见「S4-06a」最终门节，`pytest tests -q -W error::…` → 882 passed，exit 0）；全仓 `pyright` 未跑；真实 Provider（流式容量闸、真实 `usage.input_tokens` 锚点、真实摘要生成质量）未验证；usage 锚点只在当前进程内跟随真实请求——本片**未新增用量持久化**，跨 Run 首个请求退化为官方字符上界（若要求跨 Run 锚点需另拍持久化语义）；费用账本（10 美元护栏）未实现，摘要只共用请求计数/重试/超时接缝；压缩只在 Run 起点投影时尝试（agent 循环中途达到触发点只由普通容量闸挡，当前请求/当前 Run 不压缩）；`request_stream` 已接容量闸与锚点但生产流式路径归 S4-07。
- **独立静态复审（2026-09-12，orchestrator 分发）**：**accepted**、无 P0/P1；复审为静态阅读，**未执行任何命令**、未复跑本文件记录的命令结果（这是 reviewer 自报口径，本文件不据此声称复跑）。4 条 P2 处置：① 本文件原写「`load_conversation_history` 改为薄封装」与实情不符（该函数已删除）——按实际状态修正（见上）；② `runtime/agent_factory.py` 的 `_with_current_facts` 在本片改动后已无引用（死代码）——复审后**不再改动已审字节**，保留作后续清理项；③ `ContextCompressor._conversation_id` 已赋值未读（死状态）——同上处理；④ 缩小阈值 fixture 直接构造的 `EffectiveHarness` 故意越过启动容量不变量（仅测试旁路；断言本身有效）——在此声明：**不得把测试用的缩小配置当作已通过启动校验的配置**。本轮未为 P2 改动任何源/测试字节。
- **owner 改动**：本轮只追加本节、同步 S4-06 行与缺口 1；`S3-evidence-windows.md` 与既有 owner 未提交改动未触碰；未做 reset/checkout/clean/stash/commit/push。

## 缺口（不得当成已验证）

1. S4-07–S4-09 未实现：SSE／对话 API、一键重算与显式复盘、联调与全量发布验收均无证据（S4-06a 摘要持久化 schema＋仓库契约与 S4-06b 估算/压缩/投影已完成，见上节；S4-05a 配置与 S4-05b 预算/墙钟/分类纠错已完成，见上节；S4-04 工具接线与上下文已完成）。
2. 思考 token 计入输出（实测 1 个词的回答消耗 15–23 输出 token）：S4-05 不得把可见 `max_tokens` 当作总输出上界。
3. 真实调用只覆盖单轮、无工具、小输出；多轮、工具调用、压缩、失败重试的真实 Provider 行为未验证。
4. Windows 人工验收未执行（另写 `S4-evidence-windows.md`，未创建前即为未执行）。
5. 非复现 flake：`tests/test_app.py::test_lifespan_runtime_exception_closes_connection` 已在全量运行中失败**两次**（首次后单跑、按文件跑与连续 3 次全量运行均通过；2026-09-12 最终门修复后、仅换行的一次全量复跑再现 1 次），两次出现后的单独与整文件重跑均通过；根因未定位。
