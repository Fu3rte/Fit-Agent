# Fit-Agent Trace 数据与报告契约

设计见 [Trace 采集方案](trace-observability-plan.md)。

## 1. Span 记录

所有操作保存到 `backend/data/trace.db` 的 `spans` 表，根操作结束后一次事务尝试提交整条 Trace。执行期间使用内存记录器，强制退出或纯观测故障可能缺样。

全部字段必填：

- `trace_id`：UUIDv4 的 32 位小写 hex，一次执行的观测身份。
- `span_id`：Trace 内唯一正整数，根为 1。
- `parent_span_id`：同一 Trace 内的父编号，根为 `null`。
- `session_id`：所属会话的标准 UUID。
- `run_id`：Agent 运行的标准 UUID，确认保存为 `null`。
- `operation_id`：已受理业务操作的标准 UUID。
- `name`：`agent_run/turn/model/tool/compaction/confirmation`。
- `started_at/finished_at`：UTC 毫秒整数。
- `duration_ms`：`perf_counter` 实测有限非负毫秒数。
- `status`：`completed/failed/cancelled`。
- `error_code`：受控错误码或 `null`。
- `attributes`：按 name 校验的 JSON 对象。

主键为 `(trace_id, span_id)`，父子关系限定在同一 Trace 内，业务身份由根上下文统一传递。根 name 为 `agent_run` 或 `confirmation`，分别按 `run_id` 或确认 `operation_id` 唯一；业务身份查询索引覆盖根记录。

SQLite 结构版本使用 `user_version`，初始值为 1。过期判定为根 `started_at` 早于当前时间减 30 天，恰好 30 天的记录保留。

### 1.1 属性白名单

- `agent_run`：`first_text_ms`。
- `turn`：空对象。
- `model`：`provider/model/call_kind/stop_reason/first_text_ms/input_tokens/output_tokens/cache_read_tokens/cache_write_tokens`。
- `tool`：`tool_call_id/tool_name/execution_ms/postprocess_ms`。
- `compaction`：`reason`。
- `confirmation`：`business_kind`。

模型开始时必填真实配置的 `provider/model/call_kind`；工具必填真实协议 `tool_call_id`，`tool_name` 为核实后的注册名，未核实时为 `null`；压缩必填 reason，确认必填 business_kind。

枚举及数值约束：

- `call_kind`：`normal/history_summary/turn_prefix_summary`。
- `stop_reason`：`stop/length/toolUse/error/aborted`。
- `reason`：`threshold/overflow`。
- `business_kind`：`profile/plan/workout`，训练记录新增和修正均为 workout。
- Token 为非负整数，延迟和分段耗时为有限非负数。

仅补充真实取得的字段。未知属性省略或为 `null`，读取时统一视为未知，零值为有效样本；额外字段拒绝写入。

## 2. 采集与生命周期

### 2.1 记录器

`backend/app/agent/telemetry.py` 负责 `RunTrace`、Span、SQLite 存储和整批写入。

显式传入父上下文，`start_span(name, attributes, callback)` 恰好执行一次 callback，保留业务返回值和原始异常。观测创建失败时使用无记录上下文。已结算 Span 的迟到更新无效，父结算等待必要子操作及线程收尾。

### 2.2 Agent 根

`interfaces/http.py` 在受理成功时建立唯一 `agent_run` 根，身份使用真实 `session_id/run_id/operation_id`，受理起点传入 `RunTrace`。`agent/config.py` 和 `agent/agent_loop.py` 接收同一根上下文；独立 Agent 调用使用无记录上下文。

根计时覆盖线程调度、请求投影、执行和业务终态提交。结局沿用真实运行状态，终态提交失败记 failed；Trace 状态不回写业务库，恢复沿用 coordinate 和 recover_interrupted。

`first_text_ms` 在公开队列边界以受理 `perf_counter` 为起点，仅记录安全投影后的首次非空正文；投影为空时保持未知。

### 2.3 子 Span

- turn：根下按实际 `turn_start/turn_end` 建立及结算，属性为空。
- model：普通和恢复请求挂在真实 turn 下，两类摘要挂在 compaction 下；覆盖请求至底层流关闭，异常、取消和提前关闭均等待收尾。
- model 首正文：请求开始至首次非空 `text_delta`；结局、stop_reason 及拆分用量来自真实适配器和逐调用缓存观测。
- tool：从准备至执行、后处理收尾，并行调用共享真实 turn；复用现有执行与后处理测量，准备拒绝时执行耗时未知，`is_error` 记 failed。
- compaction：包含准备、摘要及检查点提交，校验或提交失败记 failed。

模型合法 length 响应记 completed，Agent 输出上限结局沿用业务失败状态。编辑及重新生成建立新 Trace，Steering 使用当前运行上下文。

### 2.4 确认根

`application/business/confirmation.py` 在已受理确认的执行负责人边界建立独立 `RunTrace`：根 name 为 confirmation、编号 1、父编号和 run_id 为 `null`，业务身份取真实受理操作，属性仅含 business_kind。

计时从受理事务返回后开始，覆盖校验、保存及结果确定。成功记 completed，已受理保存失败记 failed，真实取消记 cancelled；错误码使用既有受控代码。

HTTP 显式传入主事件循环已有 `TraceStore` 和凭据检查。同键及已保存快照复用不新增记录器或根，独立 `ConfirmationService` 调用使用无记录上下文。`confirm/_execute` 的观测参数为可选关键字参数，保持调用兼容。

## 3. 存储与故障隔离

- 数据库连接由 HTTP 主事件循环获取并随服务关闭释放；线程显式接收记录器及父 Span，共享内存登记受保护。
- 创建、补充属性、结算、初始化、写入和清理中的纯观测普通故障隔离，业务异常保持原样，协作取消与进程退出沿用现有控制流。
- 写入前校验字段、父子关系和凭据；拒绝记录连同整棵子树剔除，保持父子完整性及敏感信息白名单。
- 根收尾后一次事务提交，失败允许整条 Trace 缺失，记录器仍须释放。BEGIN 至 COMMIT 任一环节失败均回滚并结束本次事务，同一连接继续承接后续 Trace。
- 启动及写入时按根时间整体清理过期链；写入事务先清理再插入，本次插入的过期链在下一次写入时清除。
- 初始化任一步骤失败均关闭新连接；`open_trace_store` 传播原始错误，`connect_trace_store` 隔离观测初始化故障并返回空句柄。

成功顺序为业务终态提交、根结算、尝试写入 Trace、发送终态通知或返回确认响应。根耗时截止业务终态提交，写入及通知等待属于后续开销。

观测故障不改变已提交业务状态或终态通知。终态提交失败时根记 failed 并尝试保存诊断，业务恢复使用原有操作接口。

隔离仅限观测适配器；业务输入、公开内容、授权、幂等和保存前校验保持现有规则。本地报告的参数、数据校验及数据库读取错误直接报错。诊断仅允许受控错误码及错误类型，禁止对话、伤病、附件、工具参数和结果、凭据及原始异常详情。

## 4. 本地报告命令

`backend/app/agent/trace_report.py` 只读访问本地诊断库。从 `backend/` 执行 `python -m app.agent.trace_report`，输出 JSON；时间及 UUID 参数使用 argparse 和现有类型校验，连接在结束时释放。

### 4.1 metrics

- 可选 `--from/--to/--session-id`，时间为 UTC 毫秒整数。
- 默认 from 为当前时间减 30 天，to 为当前时间，有效范围限制在最近 30 天，采用 `[from, to)`。
- 按根 `started_at` 选取整条链，session-id 限定根会话。
- 固定顶层字段 `window/groups`，window 包含 from、to；合法无记录时 groups 为 `[]`。
- 空区间、反向或越界时间及非法参数直接报错。

### 4.2 show

- 必须提供 `--run-id` 或 `--operation-id` 中的一项，分别查询 Agent 根和确认根。
- 可选 `--session-id` 限定根会话，遵循最近 30 天范围。
- 固定顶层字段 `spans`，返回根及全部子操作，按 span_id 升序；合法身份无记录时为 `[]`。
- 数据通过 Span 模型校验后导出，JSON 可重定向至仓库已忽略的本地目录。

## 5. 分组统计

统计 `agent_run/model/tool/compaction/confirmation`，turn 不单独成组。模型按 `provider/model/call_kind`，工具按 `tool_name`，其余按 name 分组；未核实工具名归入 `tool_name=null`。

并行工具耗时之和表示工作量，运行总耗时取根 Span。

每组字段：

- `key`：`name/provider/model/call_kind/tool_name`，不适用项为 `null`。
- `calls`：已记录调用数。
- `status_counts`：completed、failed、cancelled 数量。
- `failure_rate`：failed / calls。
- `cancellation_rate`：cancelled / calls。
- `duration_ms`：成功调用的耗时统计。
- `first_text_ms`：Agent 和模型成功调用的首正文统计，其余为 `null`。
- `usage`：模型用量汇总，其余为 `null`。
- `slow_calls`：所有终态中最慢的最多 20 条。

### 5.1 耗时

固定字段为 `samples/mean/p50/p95/max`。仅 completed 且测量已知的样本参与；空样本四个数值均为 `null`，单样本均等于该值。p50 使用 `statistics.median`，p95 使用 `statistics.quantiles` 的 inclusive 线性插值。

### 5.2 用量

四项 Token 各自包含 `total/samples`，仅汇总该字段已知的调用，零用量有效，无样本时 total 为 `null`。

另含 `cache_hit_rate/cache_samples`，仅使用三项输入用量均已知的共同样本：

```text
sum(cache_read) / sum(input + cache_read + cache_write)
```

无共同样本或分母为零时命中率为 `null`。

### 5.3 最慢调用

每项包含 `trace_id/span_id/started_at/duration_ms/status`，按 `duration_ms/started_at/trace_id/span_id` 降序取最多 20 条。

所有指标仅描述已落库样本，强制退出和纯观测故障可能缺样。

## 6. 前端运行用时

- 使用同一真实运行的 `started_at/finished_at`，历史重建、运行查询、操作查询及终态刷新共用 UTC 毫秒口径。
- 同 run_id 合并较早 running/unknown 快照或未知账本时保留已知起止时间，其余状态、身份、条目和字段采用传入数据。
- 状态非 running 且起止齐备时显示，取毫秒差除以 1000，保留一位小数，零差值有效；running 隐藏用时但保留测量。
- unknown 且时间齐备时显示用时，同时保留结果未知提示与显式重试入口。
- 确认保存的独立轮次和未受理占位不携带 Agent 用时，测量不完整时保持原界面；启动恢复的中断运行结束时间为空。
- 文本位于回答条目之后、状态及未知操作提示之前，沿用既有样式。
- SSE 终止事件不补客户端时间，不新增轮询、定时器、持久化或公开协议字段。

## 7. 数据保护与验证

验证覆盖真实模型、并行工具、失败、取消、断连、压缩、确认幂等、父子关系、计时、未知用量、统计和 30 天清理。存储边界使用真实隔离 SQLite，覆盖提交锁冲突后的事务恢复及初始化故障后的连接关闭。

性能和存储测量仅写隔离库，生产 `backend/data/trace.db` 使用 `mode=ro` 核查，禁止构造样本进入生产统计。核查兼容生产库存在及不存在两种基线，保留已有数据和证据。
