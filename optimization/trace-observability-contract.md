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

本地 `spans` 表不新增 `client_attempt` 名称。客户端观测节点只进入 Cloud Trace，见第 6、7 节。

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

成功顺序为业务终态提交、根结算、尝试写入本地 Trace、提交 Cloud 导出队列、发送终态通知或返回确认响应。根耗时截止业务终态提交，本地写入、Cloud 入队及通知等待属于后续开销。本地写入与 Cloud 导出相互独立：本地失败仍提交已生成的快照，Cloud 失败不改变本地记录与业务终态。

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

## 6. 前端运行用时与客户端观测

### 6.1 显示口径

- 使用同一真实运行的 `started_at/finished_at`，历史重建、运行查询、操作查询及终态刷新共用 UTC 毫秒口径。
- 同 run_id 合并较早 running/unknown 快照或未知账本时保留已知起止时间，其余状态、身份、条目和字段采用传入数据。
- 状态非 running 且起止齐备时显示，取毫秒差除以 1000，保留一位小数，零差值有效；running 隐藏用时但保留测量。
- unknown 且时间齐备时显示用时，同时保留结果未知提示与显式重试入口。
- 确认保存的独立轮次和未受理占位不携带 Agent 用时，测量不完整时保持原界面；启动恢复的中断运行结束时间为空。
- 文本位于回答条目之后、状态及未知操作提示之前，沿用既有样式。
- SSE 终止事件不携带客户端时间，不新增公开协议字段、轮询、定时器或持久化字段。客户端测量与上报走 6.3 的独立只写接口。

### 6.2 客户端发送计时与终态

- 计时起点：一次通过输入校验与提交门禁的发送提交，在 `SessionRunRecord.send` 创建操作身份前读取 `performance.now()`。计时覆盖草稿会话创建、附件准备与网络等待；同一浏览器文档内只使用 `performance.now()` 差值。编辑与重新生成保留业务 Trace 身份规则，不纳入客户端计时；Steering 关联当前运行且不建立客户端尝试。
- 计时终点：前端解析并确认真实 `done` 或 `error` 终态事件、且通过现有业务投影校验的时刻。用户主动停止在 stop 入口捕获中止时刻；流在无终态事件时结束按断连时刻；解析失败按失败时刻。
- 首正文只记录一次：真实解析并通过业务投影校验的安全可见非空正文。thinking、工具事件、心跳、隐藏节点与空正文不计。
- 终态分类固定为五类：`completed`（`done` 且 status 为 completed）、`failed`（`error` 且 status 为 failed）、`cancelled`（`error` 且 status 为 cancelled，或用户主动停止）、`disconnected`（流结束且未收到终态事件，含网络中断）、`unknown`（解析失败、页面卸载，或仅由恢复查询确认终态）。
- 一次传输尝试仅结算一次。停止或断连已测得的分类与耗时冻结，后续查询只确认是否可以上报。
- 页面卸载、管理层释放与恢复查询不补造已丢失的首正文或完成时间。仅由恢复查询确认终态时分类为 `unknown`，客户端时长保持缺失；已有真实首正文可以保留。
- 客户端时长与客户端首正文均为可选测量；缺失保持缺失，零值有效。显式重发与补报复用同一 attempt 身份与测量；实际再次发送业务 HTTP 请求时建立新的传输尝试，业务操作保持原身份；纯恢复查询不建立传输尝试。

### 6.3 客户端指标上报契约

接口固定为 `POST /api/agent/client-attempts`，同源访问，沿用 `check_boundary`，请求 `Content-Type: application/json`。解析 JSON 前逐块累计请求体，超过 4096 字节返回 `413 request_size_exceeded`；缺失或伪造 `Content-Length` 同样按实际字节数判定。附件接口原有大小限制保持不变。

请求字段仅允许：

- `session_id`：标准 UUID。
- `operation_id`：标准 UUID，一次已受理的 send 操作身份。
- `run_id`：标准 UUID 或 `null`；取得响应头后携带真实运行 ID，存在时必须与 `operation_id` 解析出的运行一致。
- `client_attempt_id`：客户端生成的 UUIDv4，一次传输尝试的身份与幂等键。
- `attempt_kind`：固定为 `send`。
- `client_duration_ms`：可选；提供时为有限非负数，范围 `0..86_400_000`，单位毫秒。
- `client_first_text_ms`：可选；提供时为有限非负数，单位毫秒；两项都提供时不得超过 `client_duration_ms`。
- `client_terminal_kind`：6.2 的五个枚举值。

额外字段、布尔值、非数值、非有限数值、越界数值、非 UUIDv4、非标准 UUID 与非枚举值直接拒绝。未知测量直接省略，不补测、不填零、不做缺失字段修复；真实零值有效。

关联规则：

- 取得运行响应头前，`operation_id + session_id` 是唯一关联键；服务端按已受理操作解析运行。
- 取得响应头后，`run_id` 参与校验；与操作解析结果不一致时拒绝。
- 操作未受理、操作已失效、类型不是 send、会话不匹配或运行不属于该会话时返回 `409 attempt_unassociated`。
- 运行尚未进入终态时返回 `409 run_not_terminal`；客户端在既有操作或运行查询确认终态后，用同一 `client_attempt_id` 与已有测量重试。
- 同一尝试只保持一次在途上报；在途期间由终态确认或历史刷新产生的补报触发只记一次，在该请求以可重试结果结束后立即消费并沿用原测量重报，不新增轮询、定时器或循环重试。
- 重复受理 JSON 只返回原运行，不建立新 Trace；同一操作的不同传输尝试各自使用独立 `client_attempt_id`，共用同一业务 Trace 与根节点。

去重、窗口与回执：

- 先校验归属、操作类型、运行一致性与接收窗口，再处理去重。
- 去重键为服务端解析出的真实 `(run_id, client_attempt_id)`。请求中的 `null` 与该真实运行 ID 统一表达；可选测量按接收模型自然规范化；接收时间与服务器处理时长不进入请求指纹。
- 同一规范化请求体在有效期内返回 `200 duplicate`；同一键的不同请求体返回 `409 attempt_conflict`。同进程并发请求只提交一次。
- 去重缓存为进程内有界 TTL 缓存，容量 4096，有效期 600 秒。进程重启后缓存为空，同一身份允许重新导出；冲突体在重启后的诊断值可能被覆盖，该残余风险不影响业务状态。
- 正常持久终态（completed/failed/cancelled）且 `finished_at` 已知时，仅接受 `0 <= now_ms - finished_at <= 600_000` 的上报；超出窗口返回 `410 attempt_expired`。`interrupted` 或缺失 `finished_at` 返回 `410 attempt_unreportable`，不补写业务时间。
- `CloudTraceExporter` 为 `None` 时返回 `200 {"status":"skipped","reason":"cloud_unavailable"}`；首次接受处理返回 `202 {"status":"recorded"}`；重复返回 `200 {"status":"duplicate"}`。三类回执均携带稳定 `observation_id`（16 位小写 hex），客户端据此结束该次补报。
- 观测提交异常只在观测边界隔离，返回 `503 observation_unavailable`，不输出原始异常或伪造成功；该次结果不进入去重缓存，后续可用同一尝试重试。
- `recorded` 只说明接口接受处理；应用队列、SDK 传输与 Cloud 实际入库分别核验。错误响应沿用现有 `detail` 结构与 `no-store` 规则。

该接口只写观测，不改变业务状态、确认、取消、权限或保存规则。客户端输入按不可信诊断数据处理，服务端不得据此补写业务时间、状态或用量。

### 6.4 首正文口径

- 客户端首正文 `client_first_text_ms`：6.2 起点至客户端解析到首个安全可见非空正文事件的时刻。
- 服务端首正文 `agent_run.first_text_ms`：HTTP 受理 `perf_counter` 至安全投影后的首个非空正文进入 SSE 队列的时刻。
- 模型首正文 `model.first_text_ms`：模型请求起点至首个非空 `text_delta` 的时刻。
- 三项独立记录，缺失时保持未知，禁止互相补写。

### 6.5 Cloud 节点与时间包络

- 客户端上报在业务 Trace 根下建立独立 `client_attempt` 观测节点。节点起止表示服务器接收与处理本次上报的时间；客户端持续时间写入 `fit.client_duration_ms`，不作为节点时长。
- 用户端到端时间命名为 `client_duration_ms`；服务端根耗时取 `agent_run.duration_ms`；Cloud Trace 时间包络为同一 trace 内最早 start 与最晚 end 的差，包含后置 `client_attempt` 节点，可能大于前两者。
- 查看位置分别为 `client_attempt` 属性、`agent_run` Span 和 Trace 详情顶部。三个口径不得互相替代。

## 7. Cloud 导出与业务 Trace 映射

### 7.1 身份与 ID

- 一次业务运行对应唯一 Cloud Trace；Trace 身份使用 `Langfuse.create_trace_id(seed=f"fit-agent/business/run/{run_id}")` 生成 32 位小写 hex。
- 确认保存使用独立 Trace，seed 为 `fit-agent/business/confirmation/{operation_id}`；评测回放与网页裁判使用各自 seed，禁止并入业务运行 Trace。
- Trace ID 必须非零。零值或格式不符时追加 `:salt:{n}` 重新生成，最多 8 次；仍失败则整条 Trace 不导出。
- Span ID 使用 `sha256(f"fit-agent/business/{run_id}/{span_id}")[:16]` 生成 16 位小写 hex；根 `span_id=1`。零值或同一 Trace 内碰撞时追加 `:salt:{n}` 重新生成，最多 8 次；仍失败则剔除该 Span 及其子树。确认 Trace 的 `run_id` 为空，身份材料使用 `operation_id`，其余规则相同。
- 客户端节点 ID 使用 `sha256(f"fit-agent/client-attempt/{run_id}/{client_attempt_id}")[:16]`。
- 父身份使用本地 `parent_span_id` 显式构造 OTel `Context`；根无父。禁止依赖进程环境中的当前 Span，线程与并行工具不得串链。

### 7.2 Span 映射与类型

| 本地 name | Langfuse observation type | 远端 name |
| --- | --- | --- |
| `agent_run` | `span` | `agent_run` |
| `turn` | `span` | `turn` |
| `model` | `generation` | `model` |
| `tool` | `tool` | `tool` |
| `compaction` | `span` | `compaction` |
| `confirmation` | `span` | `confirmation` |
| `client_attempt` | `span` | `client_attempt` |

- 普通模型、恢复请求、`history_summary` 与 `turn_prefix_summary` 均导出为 `generation`，按真实调用身份各占一个 Span；父 Span 不重复计量。
- 根 Span 写入 `langfuse.trace.name`、`langfuse.trace.metadata`、`session.id`、`langfuse.environment` 与 `langfuse.release`；`agent_run` 另在实际取得时写入 `fit.first_text_ms`，未知时省略。
- 模型 Span 写入 `langfuse.observation.model.name`、`langfuse.observation.usage_details`、`fit.provider`、`fit.call_kind`、`fit.stop_reason` 与 `fit.first_text_ms`。
- 工具 Span 写入 `fit.tool_call_id`、`fit.tool_name`、`fit.execution_ms` 与 `fit.postprocess_ms`；真实注册名为空时省略。
- 压缩 Span 写入 `fit.reason`；确认 Span 写入 `fit.business_kind`；turn 不写属性。
- 所有 Span 写入 `fit.observation_origin`：服务端快照为 `server`，客户端节点为 `client`。

### 7.3 历史时间与时钟异常

- OTel `start_time = started_at_ms * 1_000_000`；`end_time = finished_at_ms * 1_000_000`。
- 壁钟异常识别：`wall_ms = finished_at - started_at`，`skew_ms = wall_ms - duration_ms`。`wall_ms < 0` 或 `abs(skew_ms) > 1000` 时写入 `fit.clock_anomaly=true` 与 `fit.clock_skew_ms`，并将 `end_time` 回退为 `start_time + round(duration_ms * 1_000_000)`，使用现有 `perf_counter` 单调时长。
- `duration_ms` 非有限或为负时剔除该 Span 及其子树，不补造时间。
- 客户端节点起始为服务器接收上报的 UTC 毫秒转换值，结束按服务器处理时长单调累加。

### 7.4 Token 与费用

四项 Token 来自真实供应商 usage，互斥分桶：

- `input` → `input`
- `output` → `output`
- `cache_read` → `cache_read_input_tokens`
- `cache_write` → `cache_creation_input_tokens`
- `total` 为四项之和，仅作为汇总键。

`cache_write_1h` 与 `reasoning` 分别是 `cache_write` 和 `output` 的子集，禁止重复计入。四项任一缺失时整组省略 `usage_details`；四项齐备时零值有效并原样上传。

不写入 `cost_details`。费用为 Langfuse Cloud 按项目模型定义计算的估算，不等同供应商账单：模型名取真实 `model.id`，与 Cloud 模型定义的 `match_pattern` 正则匹配，用户自定义定义优先。价格来源、单价单位、币种与项目实际配置需按 Cloud 现状核验；本仓库不引入后端费率表或自建价格计算器。

无 usage 或无匹配价格时费用保持 unknown；部分模型调用可定价时费用为 partial。根 `langfuse.trace.metadata` 写入 `fit.usage.coverage`（`full/partial/unknown`）与 `fit.usage.model_calls`，作为覆盖分母；`fit.usage.priced_calls` 在 Cloud 定价结果核验前保持未知并省略，禁止写入未经核验的 0 或完整定价状态。

### 7.5 scope、status 与属性白名单

- 业务进程只导出 instrumentation scope 为 `fit-agent.business` 的 Span；`should_export_span` 精确匹配该 scope，其他库与未知 scope 丢弃。
- 评测回放与网页裁判分别使用 `fit-agent.evaluation` 与 `fit-agent.judge`，在独立进程或独立 public key 下运行，环境为 `evaluation`，不得与业务 Trace 混合。
- completed 使用 OTel `UNSET`，`langfuse.observation.level=DEFAULT`；failed 使用 `ERROR`，level 为 `ERROR`；cancelled 使用 `ERROR`，level 为 `WARNING`。
- `langfuse.observation.status_message` 仅允许受控 `error_code` 或 `cancelled`。创建 Span 时固定 `record_exception=False`、`set_status_on_exception=False`，不导出 exception event、原始异常消息或堆栈。
- Cloud 属性白名单限于第 1.1 节的业务字段、Span 名称、状态、时间、Token、模型/工具名称、受控错误码及 6.3 的客户端数值字段。对话、健康资料、附件、工具参数与结果、凭据、请求头、带查询参数的 URL、原始异常详情禁止进入 Cloud。

### 7.6 生命周期、队列与关闭

- 使用独立 `TracerProvider`，通过公开 `Langfuse(tracer_provider=provider, ...)` 接入导出。历史起止使用公开 OTel `Tracer.start_span(start_time=...)` 与 `Span.end(end_time=...)`；Langfuse 5.0.4 的 `start_observation` 无 Span 历史起点参数，不用于快照导出。
- 进程启动时按“读取配置、创建 TracerProvider、创建 Langfuse 客户端、开放运行受理”的顺序初始化。同一 public key 只允许一个客户端；SDK 会复用首个资源管理器并忽略后续 provider、exporter、filter 与 environment 配置。
- 每次运行根结算后生成一次 `SpanRecord` 快照；本地 SQLite 写入与 Cloud 入队分别隔离。Cloud 入队为同步、非阻塞、有界队列操作，队列容量 1024，热路径不等待网络。
- 队列满时整条 Trace 丢弃并累计 `dropped_cloud_queue_full`；不得拆分 Trace 或阻塞回复。导出线程只消费不可变快照，不访问业务对象。
- 关闭顺序为停止受理、排空运行、关闭 Cloud 导出器、关闭本地 Trace 库、关闭业务库。Cloud 关闭等待 5 秒排空导出与释放 SDK 资源；超时记录 `dropped_cloud_close_timeout` 后继续关闭本地资源，禁止抛出到业务生命周期。关闭开始后的新提交不再入队。
- 本地写入失败允许整条本地 Trace 缺失，已生成快照仍可提交 Cloud；Cloud 失败允许整条 Cloud Trace 缺失，本地记录与业务终态保持完整。丢样状态只用于诊断，不改变报告口径。

### 7.7 environment、source 与归属

- 业务聊天环境取 `FIT_TRACE_ENVIRONMENT`（`production` 或 `development`），`fit.source=chat`。
- 评测回放环境为 `evaluation`，`fit.source=evaluation_replay`；网页裁判环境为 `evaluation`，`fit.source=web_judge`。
- 服务端 Span 的 `fit.observation_origin=server`；`client_attempt` 为 `client`。两者通过同一 `trace_id`、根父 Context 与 `session_id/run_id/operation_id` 关联。
- 评测 dataset、experiment、Score 与真实聊天分别保留身份、环境与费用口径；历史证据和评测成绩原样保留。

### 7.8 客户端节点

- `client_attempt` 是业务 Trace 根下的独立子节点，父 Context 指向根 Span；根已结算后仍可显式使用其 `SpanContext` 建立子节点。
- 节点属性写入 `fit.client_attempt_id`、`fit.attempt_kind`、`fit.client_terminal_kind`，并复用 `session_id/run_id/operation_id`。`fit.client_duration_ms` 与 `fit.client_first_text_ms` 只在实际取得时输出，缺失属性省略，互不补写；首正文与完成时长保持独立。
- 客户端节点不进入本地 SQLite，不改变本地报告分组与统计。后置节点可能扩大 Cloud Trace 包络，用户端到端时间仍只取 `client_duration_ms`。

## 8. 数据保护与验证

验证覆盖真实模型、并行工具、失败、取消、断连、压缩、确认幂等、父子关系、计时、未知用量、统计和 30 天清理。存储边界使用真实隔离 SQLite，覆盖提交锁冲突后的事务恢复及初始化故障后的连接关闭。

Cloud 导出核验使用真实 Langfuse 5.0.4 与 OpenTelemetry 1.45.1，通过本地捕获导出器验证历史起止、纳秒转换、稳定 Trace/Span 身份、显式父 Context、generation/tool 类型、usage 编码、自有 scope 过滤及关闭释放，不触发 Cloud 写入或模型调用。客户端上报实现另在任务 3 验证归属、去重、过期、并发与断连分类。

性能和存储测量仅写隔离库，生产 `backend/data/trace.db` 使用 `mode=ro` 核查，禁止构造样本进入生产统计。核查兼容生产库存在及不存在两种基线，保留已有数据和证据。
