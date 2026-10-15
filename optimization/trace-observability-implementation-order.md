---
title: Fit-Agent Trace 实施清单
lang: zh
template: doc
status: 设计文档
---

## 1. 交付范围

实现依据为[采集方案](trace-observability-plan.md)与[数据与报告契约](trace-observability-contract.md)。
交付包含后端采集、单表存储、本地 JSON 报告、前端运行用时和验证证据。
保持现有聊天 HTTP、SSE、业务 JSON 与确认保存契约。
内存收集、收尾整批写入及异常退出可能缺样已确认。
观测故障隔离已确认为目标契约，沿用 Pi 的被动适配器语义。
实施前置：观测故障隔离按仓库 AGENTS.md 的观测故障例外条款执行，该条款采用 Pi 的观测错误机制。

新增两个业务模块：

- `backend/app/agent/telemetry.py`：Span 模型、运行级记录器、生命周期托管、单表写入与清理。
- `backend/app/agent/trace_report.py`：本地查询、指标计算与 JSON 输出。

复用项目已有的 Pydantic、aiosqlite、argparse、statistics 和计时工具。
每次运行只持有自己的内存记录器。
记录器在根操作收尾并尝试落库后释放，纯观测故障允许缺样。

## 2. 采集与写入接线

| 位置 | 工作 |
|---|---|
| `interfaces/http.py` | 受理时记录起点并创建上下文；coordinate 根据真实终态结算根操作并整批写入 |
| `agent/agent_loop.py` | 使用入口传入的 Trace 身份；Turn、工具与压缩显式传递父 Span |
| `agent/tool.py` | 包住准备、执行与后处理；复用已有分段耗时，处理返回错误与取消 |
| `agent/cache_observation.py` | 复用真实调用用途与用量来源，向对应模型 Span 补充属性 |
| `ai/types.py`、`ai/stream.py`、模型适配器 | 显式传递模型观测上下文，覆盖真实请求、首正文、流消费和关闭 |
| 压缩与摘要调用入口 | Compaction 下建立 model 子 Span，覆盖 threshold 与 overflow 路径 |
| 确认 HTTP 入口与确认服务 | 已受理确认建立独立根操作，沿用授权、幂等与保存结果 |
| 前端 historyToRounds、运行状态合并与 ChatTranscript | 传递已有起止时间，在回答消息区域显示运行用时 |

根操作的 callback 覆盖现有 coordinate 收尾。
根计时使用受理时的起点，包含线程调度和请求投影时间。
首正文记录在现有公开事件完成安全检查后更新。

采集使用显式父对象，所有需要等待的子任务在父生命周期内完成。
并行工具的登记保护共享内存，数据库只由主事件循环写入。
成功收尾后一次事务尝试提交整条 Trace。
按业务终态提交、根 Span 结算、尝试写入 Trace、终态通知的顺序接线。
纯观测故障保持 callback 的返回值、原始异常与既有终态通知不变。
业务终态提交失败时根记录 failed，业务状态恢复复用现有机制。
失败与协作取消也必须释放资源并尝试完成观测收尾。
创建记录失败时业务 callback 仍恰好执行一次。
相关调用方、现有事件身份和测试同步更新。

前端复用已有运行与历史接口中的 started_at、finished_at。
传递字段至消息区域，时间差转换为秒，有完整起止时间时展示。
终态后的现有历史刷新与重新打开会话使用同一展示口径。

## 3. 报告接线

实现 metrics 与 show 两个子命令，参数和 JSON 遵循数据契约。
复用根记录的业务身份定位链路。
按实际采集样本计算用量、失败率、耗时分位数和最慢调用。
报告明确样本数量，未知值保持 null。

## 4. 验证与验收

实现代码由用户派发的 agent 编写与修正。
测试代码编写与实际运行由 subagent 负责，主会话依据命令、退出码与输出判定结果。
文档由原主会话统一维护。
测试使用真实 SQLite、HTTP 和配置的模型服务。
证据写入仓库已忽略的 `tmp/` 或 `backend/data/`。

必须验证：

- callback 恰好执行一次，返回值与原始业务异常保持原样。
- 创建、属性更新、结算及观测数据库故障均隔离，业务 callback 的结果保持原样。
- 业务与观测同时失败时，原始业务异常继续传播，取消与进程退出保持原有语义。
- 根、Turn、模型、工具与压缩的父子关系正确。
- 并行工具各自计时；工具返回错误、取消和后处理结果真实。
- 模型首正文、用量、length、失败、提前关闭与取消的口径正确。
- 摘要调用与溢出恢复均进入对应链路。
- 投影失败、断连和确认幂等保持现有业务语义。
- 单表整批事务、30 天清理、业务身份查询与白名单有效。
- 提交阶段锁冲突后本次事务已收尾，同一连接恢复保存后续 Trace，失败批次保持原子。
- 初始化过期清理失败时新建连接已关闭，初始化错误保持传播，观测句柄获取返回空。
- 报告的空样本、单样本、缺失用量、分位数与 Top 20 正确。
- 观测写入故障隔离，已提交业务结果和业务终态通知保持原值。
- 业务终态提交失败时，Span 失败记录与既有状态恢复保持正确边界。
- 前端耗时覆盖终态、历史刷新、重新打开会话和结束时间未知。
- 实际写入耗时、存储增长和真实模型运行的额外开销。

任务一交付的采集核心、运行内存记录器与单表存储已完成自检同步，提交阶段事务收尾与初始化连接关闭两项存储边界已并入存储回归。
任务二交付的 Agent 根运行接线已完成自检同步并通过验收修复：interfaces/http.py 在受理成功时建立唯一记录器与 agent_run 根 Span，受理起点复用真实业务身份，根 callback 覆盖坐标业务执行与终态提交并按真实业务结局结算；收尾按业务终态提交、根结算、整批写入 Trace、发布原有终态事件的固定顺序执行；服务端首正文延迟以公开投影正文为准，在公开队列边界只记录首次非空正文，安全投影为空的原始短正文与始终未出现正文均保持未知；观测连接在主事件循环获取并随服务关闭释放；agent/config.py 与 agent/agent_loop.py 接收入口传入的根上下文统一内部 Trace 身份，独立 Agent 调用沿用无记录上下文语义；check_trace_storage.py 的生产路径断言改为按测试前基线核查，生产观测库存在与否两种基线均可用。
任务三交付 Turn、模型、工具与压缩子 Span 接线并完成实际验证：Turn 在根下按每轮实际业务边界建立并显式结算；普通与恢复后的模型请求、各工具调用、本轮压缩与压缩内两类摘要挂在其显式父对象下；模型 Span 覆盖真实请求至底层响应流关闭，记录真实 provider、model、call_kind、首个非空 text_delta 的首正文延迟、stop_reason 与缓存拆分用量，未知值保持省略；工具 Span 从准备开始至执行与后处理收尾，tool_name 未核实为 null，准备拒绝时执行耗时保持未知；压缩 Span 包住准备、摘要与检查点提交，reason 取 threshold 或 overflow。观测登记、结算、故障隔离与释放复用同一记录器，独立调用沿用无记录上下文。check_trace_core、check_trace_storage、check_trace_run 与新增 check_trace_spans 通过，受影响的 check_cache_observation、check_compaction_loop、check_business_context_compaction、check_model_request_dispatch、check_overflow_recovery 通过；check_trace_run 分别验证单根唯一与整链记录数量。
任务四交付确认保存的独立根 Trace 接线并完成实际验证：interfaces/http.py 显式传入主事件循环已有 TraceStore 与现有凭据检查，application/business/confirmation.py 在受理事务返回后的执行负责人边界建立一次独立 RunTrace 与 confirmation 根 Span；根 span_id 为 1、parent_span_id 为 null、run_id 为 null，session_id 与 operation_id 取实际受理操作身份，attributes 只记录 business_kind；根覆盖已受理后的校验、保存与结果确定，收尾顺序为业务终态提交、根结算、尝试整批写入、返回原确认响应，saved 记 completed、已受理保存失败显式记 failed、真实取消记 cancelled，错误码只用既有受控代码；同键处理中/成功/失败复用与已保存快照复用不新增记录器或根 Span，授权拒绝整体回滚零提交且无 Trace，观测与业务故障隔离、双重失败保留原始业务错误并释放记录器，独立 ConfirmationService 调用沿用无记录语义；confirm 与 _execute 仅新增可选关键字参数，既有构造方、调用方与测试保持兼容；check_structured_confirmation、check_structured_confirmation_http、check_confirmation_serialization、check_profile_confirmation 通过，新增 check_trace_confirmation 覆盖真实三类保存与修正、真实业务身份与白名单、失败/取消/断连终态、幂等复用零新增、授权类拒绝零提交、观测与业务双重失败、凭据白名单、HTTP 响应返回即完成写入尝试与生产路径零改动，check_trace_core、check_trace_storage、check_trace_run、check_trace_spans 通过。任务四验收依据为实际 subagent 命令、退出码、输出及 backend/temp/trace-confirmation/evidence.json。
任务五交付 backend/app/agent/trace_report.py 与 backend/test/check_trace_report.py 并通过验收：metrics/show 只读查询既有观测库，按根时间窗选链，输出分组指标、用量、分位数与最慢 20 条；专项覆盖空库、窗口边界、两类身份、参数错误、五类分组、统计公式、未知值与零值、缓存共同样本、慢调用排序、数据/结构/缺失库错误、连接释放及只读边界。当前终稿由 subagent 运行 backend/temp/trace-report-acceptance/run_checks.py 的 module、pytest、null 三个检查入口，分别调用原 check()、真实 pytest 与原 12 场景 main()，仅调整测试输出路径，三项均退出 0；pytest 实际 18 项全部通过，12 个真实 SQLite/TraceStore/main 场景全部通过，Ruff 退出 0。Token、首正文与缓存共同样本以非 None 值筛选，显式 null 与省略等价，零值及失败/取消调用的已知用量保留。证据位于 backend/temp/trace-report-acceptance/，生产数据、既有证据与相关源码执行前后基线一致；原始 -m 与定向脚本直接命令本轮未运行，采集核心和存储自检沿用已回报通过的证据。离线写入耗时与存储增长在任务六取证，真实模型运行的额外观测开销待用户实跑；check_react_loop、check_loop、check_agent_regression、check_http_contract、check_stop_paths 按既有共享请求预算未装配状态保持失败，等待用户明确授权。

任务六已交付前端运行用时接线并通过函数及真实 React 服务端渲染检查：ReActRound 传递可选 started_at/finished_at，historyToRounds 按真实 run_id 传递起止时间，mergeRunTimes 保留已知测量，mergeHistoryRounds 的 unknown 分支合并同 run_id 的时间并沿用 incoming 的状态、身份、条目及其它字段，RunSnapshot 与 attachRun 复用既有查询。ChatTranscript 在回答条目之后显示一行“用时 X.X 秒”，running 隐藏用时且保留测量，终态使用真实测量，零值有效，SSE 不补客户端时间，不新增轮询、定时器或持久化字段。subagent 实际执行 node temp/run-duration-boundary/check.mjs 的 resume-render、merge-running、merge-ledger 三场景均退出 0，scripts/run-duration-check.mjs 的 16 块检查两次退出 0；证据位于 frontend/temp/run-duration-boundary/，相关既有自检逐项结果见该目录 existing-checks.log 及 evidence/，chat-store 使用既有 webstorage 参数通过，npm run build、git diff --check 与 tsc -b 回报退出 0。主会话核对三场景观察 JSON、专项日志、源码及测试，四项实现文件与专项脚本 SHA256 均匹配稳定执行基线，定向脚本 SHA256 与原测试入口一致。范围外 session-run-steering-list-check、profile-joint-wire-check 与 model-request-budget-live-replay-check 失败保持只记录。运行查询与重复受理的专项覆盖定为合并 helper 级，以下真实链路保持未确认并由用户按真实浏览器验收，AI 侧不记为通过：SessionRunRecord 的真实 HTTP 运行查询轮询、操作查询与状态收敛、重复受理、浏览器中的终态刷新、重开会话与自动滚动（frontend/src/features/chat/utils/sessionRunManager.ts 的 attachRun/watchRun 与执行分派区间）。scripts/plan-adjustment-check.mjs 由 subagent 本轮两次执行均退出 0，日志与执行前后 SHA256 记录在 frontend/temp/run-duration-boundary/。离线两项性能取证由真实 TraceStore.save 与真实 SQLite 在隔离库完成，环境为 Windows、backend/.venv 的 Python 与 SQLite 3.50.4、journal 模式 delete、synchronous=2、page_size=4096，测量样本的 p95 采用 nearest_rank_ceil，报告命令的分位数使用 inclusive，两者口径不同：12 span 链单事务整批写入 261 次样本 min 7.28、median 10.59、p95 12.36、max 16.52 毫秒，3/6/12/20 span 的 median 依次 10.36、10.47、10.93、11.08 毫秒、全距 0.72 毫秒，6000 行整批 119.67 毫秒约 0.02 毫秒每行，单事务内清理、序列化、插入与提交尚未分阶段测量；存储增长净增 1232896 字节对 261 条链即 4723.74 字节每 Trace、393.64 字节每 span，跨形状斜率 381.53 字节每额外 span（R² 0.999115）；同事务清理一条 31 天过期链使 median 由 10.56 增至 11.10 毫秒，批量 40 根过期删 480 行耗 10.54 毫秒；journal 为 delete 模式，单链写入峰值 33344 字节，全程无 -wal/-shm 且关闭后无残留；记录器快照 records() median 0.15 毫秒。证据位于 backend/temp/trace-perf-offline/，生产代码与既有测试时间戳零改动。性能测量一律写隔离库，backend/data/trace.db 只做 mode=ro 只读核查：本轮测量脚本曾经默认 open_trace_store 路径写入一条 3 span 构造链，被真实 trace_report metrics 计入 agent_run 与 stepfun 模型分组，构成样本污染；该链已按用户授权删除，观测库回到 45056 字节、96 行、60 条 Trace（sha256 fa3720ce789b4e877779593e88ec4b8bf0791b66d64ca2dba0b24bf8da1821be）且 integrity_check 为 ok；真实 trace_report 复核显示精确窗口 groups 为空、默认窗口 agent_run 由 31 降至 30、stepfun/step-3.7-flash/normal 由 7 降至 6，input/output/cache_read/cache_write 总量分别减少 9216/248/32768/2048，输出内已无该 trace_id，show 按该 run_id 与 operation_id 均返回空 spans；测量脚本的生产阶段改为 mode=ro 只读核查，复跑返回 unchanged 为真、字节与行数零变化，删除证据为 backend/temp/trace-task6-closeout-review/remove-chain.json，复核证据为同目录 post-cleanup.json 与 production-readonly-phase.json，写入成本与首建成本均改由隔离库与隔离空路径的同函数实测给出，首建两次为 17.51 与 21.49 毫秒、schema 初始化后均 20480 字节。真实模型运行的额外观测开销由用户实跑取证。unknown 且起止时间齐备的轮次显示用时并同时保留提交结果未知提示与显式重试入口，该展示口径已确认为契约口径。实现由用户派发的 agent 完成，测试编写、修正及执行由 subagent 完成，文档由原主会话维护。
