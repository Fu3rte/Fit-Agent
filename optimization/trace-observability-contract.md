---
title: Fit-Agent Trace 数据与报告契约
lang: zh
template: doc
status: 设计文档
---

## 1. Span 记录

所有操作保存到 `backend/data/trace.db` 的 `spans` 表。
一次 Trace 的记录在根操作结束后，用一个事务尝试提交。
执行期间使用内存记录器，已接受强制退出与纯观测故障可能导致缺样。

每条记录包含：

| 字段 | 类型与含义 |
|---|---|
| trace_id | UUIDv4 的 32 位小写 hex，一次执行的观测身份 |
| span_id | 正整数，在该 Trace 内唯一，根为 1 |
| parent_span_id | 同一 Trace 内的父编号，根为 null |
| session_id | 所属会话的标准 UUID |
| run_id | Agent 运行的标准 UUID，确认保存为 null |
| operation_id | 已受理业务操作的标准 UUID |
| name | agent_run、turn、model、tool、compaction、confirmation |
| started_at | UTC 毫秒整数 |
| finished_at | UTC 毫秒整数 |
| duration_ms | perf_counter 实测的有限非负毫秒数 |
| status | completed、failed、cancelled |
| error_code | 受控错误码或 null |
| attributes | 按 name 校验的 JSON 对象 |

所有声明字段均必填。
同一 Trace 的业务身份由根上下文统一传递。
主键为 `(trace_id, span_id)`，父子关系限定在同一 Trace 内。
根 name 为 agent_run 或 confirmation。
根记录按 run_id 或确认 operation_id 建立唯一约束。
查询业务身份的索引只需覆盖根记录。
数据库结构版本使用 SQLite 的 user_version，初始值为 1。
过期判定为根 started_at 早于当前时间减 30 天，恰好 30 天的记录保留。

各类 attributes 允许以下字段：

| name | 字段 |
|---|---|
| agent_run | first_text_ms |
| turn | 空对象 |
| model | provider、model、call_kind、stop_reason、first_text_ms、input_tokens、output_tokens、cache_read_tokens、cache_write_tokens |
| tool | tool_call_id、tool_name、execution_ms、postprocess_ms |
| compaction | reason |
| confirmation | business_kind |

模型开始时必填 provider、model、call_kind。
工具开始时必填 tool_call_id，tool_name 未核实时为 null。
压缩必填 reason，确认保存必填 business_kind。
执行中只补充本次取得的字段。
未知属性值保持省略或 null，读取时按 null 处理。
额外字段拒绝写入。

provider、model 来自真实模型配置。
call_kind 为 normal、history_summary、turn_prefix_summary。
stop_reason 沿用 stop、length、toolUse、error、aborted。
tool_call_id 沿用真实协议身份，tool_name 使用核实后的注册名称。
reason 为 threshold 或 overflow。
business_kind 为 profile、plan、workout。
Token 为非负整数，延迟和分段耗时为有限非负数。

## 2. 本地命令

从 `backend/` 执行 `python -m app.agent.trace_report`。
输出为 JSON，时间与 UUID 参数由 argparse 和现有类型校验处理。

### metrics

参数为可选的 `--from`、`--to`、`--session-id`。
from、to 是 UTC 毫秒整数，默认最近 30 天。
省略 from 时取当前时间减 30 天，省略 to 时取当前时间。
有效范围限制在最近 30 天，使用 `[from, to)`。
按根 Span 的 started_at 选取整条链路。
合法空结果输出空数组，空区间或非法参数直接报错。

固定顶层字段为 `window`、`groups`。
window 包含 from、to。

### show

必须提供 `--run-id` 或 `--operation-id` 中的一项。
run-id 查询 Agent 根记录，operation-id 查询确认根记录。
可提供 `--session-id` 限定所属会话。
查询遵循最近 30 天范围。

固定顶层字段为 `spans`。
匹配时返回根及全部子操作，按 span_id 升序排列。
合法身份无记录时返回空数组。
数据通过 Span 模型校验后导出。

命令只访问本地诊断数据库。
JSON 可重定向到仓库已忽略的 `tmp/` 目录保存。

## 3. 分组指标

统计 agent_run、model、tool、compaction、confirmation。
模型按 provider、model、call_kind 分组，工具按 tool_name 分组。
工具名称未核实的样本归入 tool_name 为 null 的组。
其余操作按 name 分组。
并行工具的耗时之和表示工作量；运行总耗时取根 Span。

每组包含：

- `key`：name、provider、model、call_kind、tool_name；不适用项为 null。
- `calls`：已记录的调用次数。
- `status_counts`：completed、failed、cancelled 的数量。
- `failure_rate`：failed / calls。
- `cancellation_rate`：cancelled / calls。
- `duration_ms`：成功调用的耗时统计。
- `first_text_ms`：Agent 与模型成功调用的首正文延迟统计；其余为 null。
- `usage`：模型用量汇总；其余为 null。
- `slow_calls`：该组所有终态调用中最慢的最多 20 条记录。

耗时统计固定包含 samples、mean、p50、p95、max。
仅 completed 且对应测量值已知的记录进入统计。
空样本的四个数值均为 null，单样本的四个数值均为该值。
p50 使用 statistics.median。
p95 使用 statistics.quantiles 的 inclusive 线性插值。

usage 中的四项 Token 各自包含 total、samples。
每项只汇总该字段已知的调用，零用量属于有效样本。
字段没有有效样本时，total 为 null。
usage 另含 cache_hit_rate、cache_samples。
缓存命中率使用三项输入用量都已知的共同样本：
`sum(cache_read) / sum(input + cache_read + cache_write)`。
共同样本为空或分母为零时，缓存命中率为 null。

slow_calls 每项包含 trace_id、span_id、started_at、duration_ms、status。
按 duration_ms、started_at、trace_id、span_id 降序排列。
统计只描述已落库样本，进程强制退出与纯观测故障可能造成缺样。

## 4. 写入与业务边界

目标契约的观测故障隔离沿用 Pi 的被动观测语义。
创建 Span、补充属性、结算状态、观测数据库初始化、写入和清理中的纯观测故障均隔离。
创建失败时提供无记录上下文，业务 callback 恰好执行一次。
正常返回值保持原样，业务异常以原始异常继续传播。
业务和观测同时失败时，观测故障保持原始业务异常不变。
协作取消与进程退出沿用现有控制流，观测隔离仅处理纯观测操作的普通故障。

写入前校验字段、父子关系与现有凭据规则。
校验失败拒绝相应观测记录，敏感数据保持在白名单边界内。
被拒绝记录连同其整棵子树一并剔除，保持同一 Trace 的父子完整性。
落库事务保持原子性，失败允许整条 Trace 缺失，记录器随后释放。
BEGIN 至 COMMIT 的任一环节失败都在同一收尾范围内结束本次事务，该连接继续承接后续 Trace。
写入事务内的过期清理先于插入执行，本次写入的过期链路在下一次写入时被清除。

成功路径为业务终态提交、根 Span 结算、尝试写入 Trace、发送终态通知或返回确认响应。
根 duration_ms 截止业务终态提交，观测写入与通知等待属于后续开销。
纯观测故障发生时，正常终态通知继续使用真实业务结果。
业务结果已提交时保持真实状态，查询恢复沿用现有操作接口。
业务终态提交失败时，根 Span 记录 failed，并尝试保存诊断；业务异常沿用现有处理。
Trace 的 failed 表示被观测操作失败，业务状态由原有提交与恢复机制维护。

观测隔离限定在业务执行的观测适配器内。
业务校验、授权、幂等及现有错误处理保持现状。
本地报告的参数错误、数据校验与数据库读取失败直接报错。
公开诊断只允许受控错误码与错误类型。

观测数据通过本地命令查询。
前端运行用时复用现有 started_at、finished_at，时间差转换为秒。
HTTP、SSE 和业务 JSON 保持现有契约。
起止时间在前端运行模型内传递，有完整测量且状态非 running 时展示。
前端展示轮次传递同一真实运行的 started_at 与 finished_at，历史重建、运行查询、操作查询及终态后的历史刷新共用 UTC 毫秒口径。
已知结束时间不会被较早 running/unknown 快照或未知账本清除。
用时为一行文本，取毫秒差除以 1000 保留一位小数，零测量为有效展示；测量不完整时保持原有界面。
状态未知且起止时间齐备的轮次展示用时，同一轮次继续保留提交结果未知提示与显式重试入口，该组合已确认为契约口径。
确认保存的独立轮次与未受理占位轮次不携带 Agent 用时。
SSE 终止事件不补客户端时间，不新增轮询、定时器或持久化字段。
故障隔离的实施前置约束见采集方案。
初始化的任一步骤失败时关闭本次新建连接，open_trace_store 传播原始错误，connect_trace_store 返回空句柄。
采集核心、运行内存记录器与单表存储已实现，提交锁冲突后的事务收尾与初始化清理故障的连接关闭均已通过回归自检。
Agent 根运行接线已实现并通过实际回归：受理起点与真实业务身份传入 RunTrace，agent_run 根按真实业务结局结算，根计时覆盖线程调度、投影、执行与业务终态提交，业务终态提交、根结算、整批写入与终态通知按固定顺序执行；首正文判定以公开投影正文为准，在公开队列边界按受理 perf_counter 起点只记录首次非空正文，安全投影为空的原始短正文保持未知。
存储验收按测试前生产基线核查生产数据路径的存在性、大小与修改时间，生产观测库存在与否两种基线均成立，测试数据库只在隔离目录创建、清理与写入。
Turn、模型、工具与压缩子 Span 已接线并通过实际回归：Turn 在根下按每轮实际业务边界建立并显式结算，属性为空对象；普通模型请求与恢复后的普通请求、各工具调用、本轮压缩与压缩内两类摘要分别挂在其真实父对象下；模型 Span 覆盖真实请求至底层响应流关闭，按开始时的真实 provider、model、call_kind 记录，首个非空 text_delta 记首正文延迟，真实结局与缓存拆分用量来自适配器与逐调用缓存观测，stop_reason 沿用五种合法值，未知字段保持省略；工具 Span 从准备开始至执行与后处理收尾，tool_name 用核实注册名、未核实为 null，准备拒绝时执行耗时保持未知；压缩 Span 包住准备、摘要与检查点提交，reason 取 threshold 或 overflow，业务校验或提交失败记 failed。
确认根 Span 已接线并通过实际回归：已受理确认的执行负责人边界建立一次独立 RunTrace，根 name 为 confirmation、span_id 为 1、parent_span_id 为 null、run_id 为 null，session_id 与 operation_id 取实际受理操作身份；根 attributes 只记录 business_kind（profile、plan、workout，训练记录新增与修正同为 workout）；计时从受理事务返回后的执行负责人边界开始，根覆盖已受理后的校验、保存与结果确定，收尾按业务终态提交、根结算、尝试整批写入、返回原确认响应的顺序执行，成功记 completed、已受理保存失败显式记 failed、真实取消记 cancelled，错误码只用既有受控代码；HTTP 显式传入主事件循环已有 TraceStore 与现有凭据检查，同键复用与已保存快照复用不新增记录器或根 Span，独立 ConfirmationService 调用沿用无记录语义。
本地报告命令已按本契约实现并通过验收：metrics 固定顶层 window/groups，show 固定顶层 spans；from/to 为 UTC 毫秒，默认最近 30 天，使用 [from,to)，非法参数与空/反向/越界窗口直接报错；按根 started_at 选取完整链路，session-id 限定根会话，模型按 provider/model/call_kind、工具按 tool_name 分组，turn 不入统计组；耗时使用 median 与 inclusive p95，用量各字段独立计样本，未知属性省略或显式 null 均不进入数值统计，零值为有效样本，缓存共同样本要求三项输入都非 None；slow_calls 按四级键降序取最多 20 条。当前终稿经 subagent 通过无源码修改的隔离 runner 调用真实报告自检、18 项 pytest 与 12 个真实 SQLite/main 场景，三项均退出 0，Ruff 退出 0；只读、错误非零退出、连接释放与生产数据及既有证据基线均通过，证据位于 backend/temp/trace-report-acceptance/。
任务六的函数边界及真实 React 服务端渲染检查通过：running 轮次隐藏用时，已知起止时间保留；unknown 轮次合并较早 running 历史或未知账本时保留同 run_id 的完整测量；unknown 且起止时间齐备的轮次渲染用时并同时保留提交结果未知提示与显式重试入口，该组合已确认为契约口径。subagent 三场景定向检查及 16 块专项自检均退出 0，scripts/plan-adjustment-check.mjs 的 running 侧合并形状两次退出 0，npm run build、git diff --check 与 tsc -b 回报退出 0，证据位于 frontend/temp/run-duration-boundary/；主会话核对当前四项实现及专项脚本 SHA256，与稳定执行基线一致。离线性能取证由真实 telemetry.TraceStore.save 与真实 SQLite 在隔离库完成，环境为 Windows、backend/.venv 的 Python 与 SQLite 3.50.4，journal 模式 delete、synchronous=2、page_size=4096：一条 12 span 链的单事务整批写入 261 次样本为 min 7.28 / median 10.59 / p95 12.36 / max 16.52 毫秒，样本 p95 采用 nearest_rank_ceil，报告命令的分位数使用 inclusive，两者口径不同；链长 3/6/12/20 span 的 median 依次 10.36/10.47/10.93/11.08 毫秒，全距 0.72 毫秒，单事务内的清理、序列化、插入与提交尚未分阶段测量，因此只呈现实测区间；6000 行整批写入 119.67 毫秒约 0.02 毫秒每行；存储增长净增 1232896 字节对 261 条链即 4723.74 字节每 Trace、393.64 字节每 span，跨形状最小二乘斜率 381.53 字节每额外 span（R² 0.999115），空库 schema 初始化后 20480 字节；同事务内清理一条 31 天过期链使 median 从 10.56 增至 11.10 毫秒，批量 40 根过期单次清理删 480 行耗 10.54 毫秒；单链写入期间 journal 峰值 33344 字节，全程无 -wal/-shm 且关闭后无残留；记录器快照 records() median 0.15 毫秒。证据位于 backend/temp/trace-perf-offline/，生产代码与既有测试零改动。性能测量一律写隔离库，backend/data/trace.db 保持 mode=ro 只读核查：本轮测量脚本曾经默认 open_trace_store 路径写入一条 3 span 构造链并进入真实 metrics 的 agent_run 与模型分组，构成样本污染，该链已按用户授权删除，观测库回到 45056 字节、96 行、60 条 Trace（sha256 fa3720ce789b4e877779593e88ec4b8bf0791b66d64ca2dba0b24bf8da1821be）且 integrity_check 为 ok；真实 trace_report 复核确认精确窗口 groups 为空、默认窗口 agent_run 由 31 降至 30、stepfun/step-3.7-flash/normal 由 7 降至 6，input/output/cache_read/cache_write 总量分别减少 9216/248/32768/2048，输出内已无该 trace_id；测量脚本的生产阶段已改为 mode=ro 只读核查，复跑返回 unchanged 为真、行数与字节零变化，证据在 backend/temp/trace-task6-closeout-review/ 的 remove-chain.json、post-cleanup.json 与 production-readonly-phase.json；首建成本由隔离空路径的同函数两次实测 17.51 与 21.49 毫秒、schema 初始化后均为 20480 字节给出。运行查询与重复受理的专项覆盖定为 helper 级，以下真实链路保持未确认状态并由用户按真实浏览器验收，AI 侧不记为通过：SessionRunRecord 的真实 HTTP 运行查询轮询、操作查询与状态收敛、重复受理、浏览器中的终态刷新、重开会话与自动滚动（frontend/src/features/chat/utils/sessionRunManager.ts 的 attachRun/watchRun 与执行分派区间）；真实模型运行的额外观测开销由用户实跑取证。
