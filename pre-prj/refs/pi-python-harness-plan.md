# pi 0.85.0 → Fit-Agent Python Harness 可执行移植方案
> 状态：已否决（2026-09-09）：Agent 基座遵循 PLAN.md 采用 PydanticAI，本路线 A 不采用；对应 Phase 1 移植代码归档于 `backend/archive/agent_core_pi_port/`，仅作历史参考。以下原文保留不再更新。
> 目标：保持 pi 的核心数据合同、wire 行为、Agent/loop 控制语义与压缩算法，而不是另造框架。
> 本文只规划 Python 后端；不实现源码、不批准新依赖、不替代后续业务表设计。
## 0. 结论与推荐边界
1. 推荐只实施一条路线：**Fit-Agent AgentCore 兼容 profile**（`pi-ai` + `pi-agent-core` 的选定能力）。
2. 该路线移植两种首版 wire、旧 `Agent`/`agent-loop` 控制语义、原压缩算法和 Fit-Agent SQLite Run 适配。
3. 该路线不是、也不应宣传为“完整 durable `AgentHarness` 的 Python 复刻”。
4. 新 durable `AgentHarness` 与旧 `Agent`/`agent-loop` 是两套独立实现。
5. `docs/harness.md` §5.7 明确：新 Harness execution blocks 不重建也不修改 `agent-loop.ts`。
6. Fit-Agent 已拍板“重启遗留 Run 直接 failed”，与 durable Harness 的恢复目标冲突。
7. 因此推荐路线保留 pi 的核心行为，不引入 lane/branch/fork/持久恢复状态机。
8. 健身规则、正式事实校验、草稿确认和长期记忆继续放在内核外。
9. 本文所有重试、超时、预算、压缩参数和新增依赖均为候选，须另行确认。
10. 未确认前不得把候选值写成产品承诺或默认配置。
## 1. 来源基线与可信度
### 1.1 本地与上游版本
- 项目 cwd：`/home/finnian/code/agent/Fit-Agent`。
- 项目主仓 HEAD：`830d3e45527ff19cb43a02c32bf2121d9e01a89c`。
- 本地 `pi-package/agent/package.json`：`@earendil-works/pi-agent-core`，版本 `0.85.0`。
- 本地 `pi-package/ai/package.json`：`@earendil-works/pi-ai`，版本 `0.85.0`。
- 上游 `v0.85.0` tag commit：`107d79f11072bbc8a3a757ed7fd69596bee7d68c`。
- 当前新快照实际与上游 main commit `9841914c71a74d81abe07f751aefd271fd924e63` 全量一致。
- 比对范围为 agent 218/218、ai 341/341，共 559/559 个真实文件。
- 相对严格 v0.85.0，551/559 文件一致，8 个文件是 tag 后 main 版本。
- 与本方案核心相关的差异仅需警惕 `ai/src/types.ts` 与 `api/openai-responses.ts`。
- 首版不移植 OpenAI Responses wire，所以后者不进入实现范围。
- 核心 `agent-loop.ts`、Harness compaction、两种目标 wire 均与 v0.85.0 一致。
- `*:Zone.Identifier` 共 559 个，是 Windows 下载元数据，必须从源码清单排除。
- 不能用这些元数据文件推断真实源码缺失。
- 外部 SQLite session backend 包未随 `pi-package/{agent,ai}` 提供。
- Fit-Agent SQLite 层因此依据已拍板的 `aiosqlite + 手写 SQL` 自建适配。
### 1.2 版本演进的准确表述
- durable `AgentHarness` 不是 0.85.0 才突然新增的全部设计。
- 0.84.0 已将 v2 `AgentHarness` 从 experimental 提升为默认导出。
- 0.84.0 同时切换到 v4 lane-based Session/SessionStorage/SessionRepo。
- 0.85.0 在 ai 包新增 compact、可持久化的 assistant-message frames。
- 0.85.0 修复 frame 对 provider thinking level 的保留。
- 0.85.0 增加 Anthropic per-turn effort 持久化、历史 effort 标记和签名失配恢复。
- agent 0.85.0 还修复代理响应丢失 provider-native thinking level。
- 所以本文把上述能力按其真实层次描述，不用“新版 Harness 全部刚新增”的说法。
### 1.3 实际回读范围
- `pre-prj/architecture-decisions.md`、`pre-prj/PRD.md`。
- `agent/src/agent-loop.ts`、`agent/src/agent.ts`、`agent/src/types.ts`。
- `agent/docs/harness.md`，特别是 §3、§4、§5.7。
- `agent/src/harness/runtime/drive.ts`。
- `runtime/drive/{generation,response,tools,structural,boundary,terminal}.ts` 的目标路径与关键过程。
- `harness/execution/{assistant,tools,effect-gate}.ts`。
- `harness/compaction/compaction.ts` 的阈值、cut、prepare、generate、commit 输入输出。
- `ai/src/{types,models}.ts` 的数据合同与成本计算。
- `ai/src/utils/{validation,event-stream,assistant-message-frame,retry,provider-retry,overflow,estimate}.ts`。
- `ai/src/api/{openai-completions,anthropic-messages,transform-messages}.ts`。
- `agent/CHANGELOG.md`、`ai/CHANGELOG.md`、两个 package manifest。
- 源码优先于旧 README；发现冲突时以当前 TS 实现和目标版本 changelog 为准。
### 1.4 核心行为源码索引
下列行号绑定本次快照 `9841914c71a74d81abe07f751aefd271fd924e63`；路径相对 `pi-package/`。实施时由 manifest 维护，不把行号当跨版本稳定标识。

| 行为 | 源码锚点 |
|---|---|
| 低级流非阻塞 push、独立 result | `ai/src/utils/event-stream.ts:21-65` |
| Usage 可选字段、聚合省略规则 | `ai/src/types.ts:383-404`；`agent/src/harness/utils/usage.ts:14-34` |
| transformContext → convertToLlm → stream | `agent/src/agent-loop.ts:279-370` |
| listener 顺序、agent_end 与 idle | `agent/src/agent.ts:486-535,544-591` |
| length 全批拒绝、并行/顺序执行、terminate | `agent/src/agent-loop.ts:379-424,431-561,589-591` |
| 工具校验与 hook 的先后 | `agent/src/agent-loop.ts:593-765`；`ai/src/utils/validation.ts:318-350` |
| 跨模型 thinking、错误过滤、孤儿结果 | `ai/src/api/transform-messages.ts:93-116,158-223` |
| OpenAI-compatible 缓存归一化 | `ai/src/api/openai-completions.ts:1507-1548` |
| 压缩阈值、有效 cut、split-turn | `agent/src/harness/compaction/compaction.ts:147-249,311-418,634-707,753-865` |
| 新 Harness 分类与原子结算 | `agent/src/harness/runtime/drive/response.ts:180-484` |
| 请求重试默认关闭、可取消等待 | `ai/src/utils/provider-retry.ts:69-125` |

### 1.5 已知未知项
- 未拿到上游 SQLite backend 的源码与发布产物，不能声称存储格式兼容。
- 未下载 npm tarball/dist，方案以本地 src 为唯一移植输入。
- 未移植 OpenAI Responses、Google、Bedrock、Mistral、Codex 等 wire。
- 未批准 Python SDK、`httpx` 或 JSON Schema validator 新依赖。
- 未批准具体 timeout、retry、token budget、压缩 reserve 数值。
- `watchSession` 在上游仍是明确的未实现切片，不作为 Fit-Agent 目标。
## 2. 两条路线、成本与保真范围
### 2.1 路线 A：推荐的 Fit-Agent AgentCore 兼容 profile
- 保真对象一：`pi-ai` 的消息、事件、usage、tool schema、跨模型转换合同。
- 保真对象二：旧 `Agent`/`agent-loop` 的单 Agent ReAct 时序。
- 保真对象三：`compaction.ts` 的阈值、cut、split-turn 与摘要组合算法。
- 保真对象四：OpenAI-compatible 和 Anthropic Messages 两种 wire。
- 应用适配：把内存 run 接到已拍板的 SQLite `runs/messages/run_events`。
- 有意裁剪：steer、followUp、Queue、多 lane、branch、fork、navigation。
- 有意裁剪：durable intent/effect/settlement 恢复、tool replay、operation resume。
- 有意偏离：重启时不恢复，统一 `failed/interrupted_by_restart`。
- 有意偏离：取消后不提交 Assistant 消息，遵守 Fit-Agent 既定决策。
- 预计复杂度：中等；行为金测试可围绕两个 wire 与 loop 聚焦。
- 风险：SQLite 适配必须显式解决中间 tool-call assistant 的提交策略。
- 收益：足够覆盖 PRD 的模型调用、ReAct、流式、取消、压缩和指标。
### 2.2 路线 B：完整 durable AgentHarness 端到端移植
- 若“核心设计一致”特指新版 durable Harness，只能选择这条完整路线。
- 必须移植 13 个持久化叶子状态及其合法转换。
- 叶子包括 starting、checkpoint、assistant ready/retry/effect pending、tools。
- 还包括 deferred 两态、summary 四态、navigation ready-to-commit。
- 必须移植 accept、drive、requestAbort、reconcile 和 operation result。
- 必须保留 intent → effect → settlement 原子边界。
- 必须保留 synchronous effect gate 准入。
- 必须保留 assistant frames、tool progress、outcome staging 和 source-prefix materialization。
- 必须保留 invocation fencing、safe/never replay、恢复合成结果。
- 必须移植 Session/Repo/branch/lane/value/list/usage ledger 的数据合同。
- 必须有恢复测试、崩溃点故障注入与存储 conformance。
- 预计复杂度：高，且会与当前 Run 状态与重启决策重叠。
- 开工前必须重开“重启 failed 还是恢复 operation”的产品决策。
- 还必须重开“取消后 assistant 可否以 aborted 轨迹提交”的冲突决策。
- 不允许只裁剩一个 loop，却继续宣称“完整新版 AgentHarness 一致”。
- 不建议 A、B 同时实现；那会形成两套状态机、两套事件语义和双倍测试面。
### 2.3 推荐选择
- 推荐 A，原因是它与已拍板的单进程、单 Run、重启失败语义吻合。
- durable Harness 仅作不变量与故障测试的参考，不作为隐性第二运行时。
- 若用户改选 B，应停止 A 的实现规划并先修订冲突决策，不可混搭推进。
## 3. 建议 Python 模块与 TS 映射
### 3.1 包层次
```text
backend/fit_agent/
  agent_core/                 # pi 兼容内核，不含健身规则
    ai/                       # 消息、事件、wire、usage
    agent/                    # loop、工具、压缩
  runtime/                    # Fit-Agent Run、取消、SSE、预算
  storage/                    # aiosqlite 手写 SQL
  domain/                     # 健身规则、草稿、正式事实
  api/                        # FastAPI 路由与 DTO
```
- 名称可随项目最终目录调整；职责边界比具体目录名更重要。
- `agent_core` 不 import `domain`、FastAPI 或 SQLite。
- `domain` 工具通过窄接口注册给 core。
- `runtime` 组合 core、storage 和 domain，但不复制 provider 逻辑。
### 3.2 `ai` 层逐模块映射
| TS 来源 | Python 建议 | 保留合同 | 裁剪/偏离 |
|---|---|---|---|
| `ai/src/types.ts` | `agent_core/ai/types.py` | Message、Content、Usage、Model、StopReason | 非目标 API 类型裁剪 |
| `ai/src/models.ts` | `agent_core/ai/models.py` | provider/api/model 分离、cost | 动态模型目录裁剪 |
| `utils/event-stream.ts` | `agent_core/ai/event_stream.py` | Queue + 独立终值 Future | 无扩展生态 |
| `utils/assistant-message-frame.ts` | `agent_core/ai/frames.py` | encoder/reducer、terminal 不入 frame | 只做目标 blocks |
| `utils/json-parse.ts` | `agent_core/ai/partial_json.py` | UI partial best-effort | 不允许用于执行 |
| `utils/validation.ts` | `agent_core/ai/tool_validation.py` | clone→normalize→convert→coerce→check | 不以 Pydantic 默认替代 |
| `utils/estimate.ts` | `agent_core/ai/estimate.py` | 最近有效 usage + trailing estimate | 参数可配置 |
| `utils/overflow.ts` | `agent_core/ai/overflow.py` | overflow/length 分类 | pattern 随 golden 固定 |
| `utils/provider-retry.ts` | `agent_core/ai/provider_retry.py` | 请求级分类、Retry-After、可取消 sleep | 默认 retries=0 |
| `utils/retry.ts` | `agent_core/ai/assistant_retry.py` | 整轮错误分类 | 是否启用待确认 |
| `api/transform-messages.ts` | `agent_core/ai/transform.py` | 跨模型降级、孤儿 tool result | 图片能力按首版裁剪 |
| `api/openai-completions.ts` | `agent_core/ai/openai_compat.py` | request/SSE/usage/compat 子集 | 不做全部 provider quirks |
| `api/anthropic-messages.ts` | `agent_core/ai/anthropic.py` | request/SSE/cache/signature/effort | 不做 OAuth/Claude Code |
### 3.3 `agent` 层逐模块映射
| TS 来源 | Python 建议 | 保留合同 | 裁剪/偏离 |
|---|---|---|---|
| `agent/src/types.ts` | `agent_core/agent/types.py` | AgentEvent、Tool、hooks | QueueMode 删除 |
| `agent/src/agent-loop.ts` | `agent_core/agent/loop.py` | turn、tool batch、terminate | steer/followUp 删除 |
| `agent/src/agent.ts` | `agent_core/agent/agent.py` | facade、listener 顺序、idle | transcript 持久化由 runtime |
| `harness/compaction/compaction.ts` | `agent_core/agent/compaction.py` | 原 prepare/generate/result 算法 | fileOps 可裁剪 |
| `agent/src/agent-loop.ts` 的工具函数 | `agent_core/agent/tools.py` | prepare/execute/finalize、beforeToolCall/afterToolCall | 不套用新 Harness execution/tools.ts 的合同 |
| `execution/effect-gate.ts` | runtime 的轻量 effect admission | cancel 后禁止新 effect | 不宣称 durable Gate |
### 3.4 应用 runtime/storage 映射
| Fit-Agent 模块 | 责任 | 来源约束 |
|---|---|---|
| `runtime/run_service.py` | 幂等 accept、全局槽、启动 task | 已拍板 Run 规则 |
| `runtime/run_task.py` | loop 驱动、终态仲裁、draining | asyncio 取消边界 |
| `runtime/event_sink.py` | 白名单事件、分块、落库 | run_events 是补读源 |
| `runtime/cancellation.py` | invocation/run/SSE 三种取消分离 | 用户点击才取消 |
| `runtime/budget.py` | 安全 turn 边界与 effect 准入 | 数值待确认 |
| `storage/db.py` | 单连接、全局 Lock、短事务 | 已拍板 SQLite 规则 |
| `storage/run_repo.py` | 条件状态更新与幂等查重 | client_request_id 全局唯一 |
| `storage/message_repo.py` | internal/display message 分离 | final 提交策略待确认 |
| `storage/event_repo.py` | SSE id、白名单 payload | 不作为业务事实源 |
### 3.5 领域层明确外置
- 健身安全红旗与动作限制不进入 `agent_core`。
- 计划、档案、训练记录的正式事实不依赖模型摘要。
- 长期记忆从已确认业务表读取，不从 `run_events` 反推。
- Agent 工具只生成 Pending 草稿，不直接改正式事实。
- 草稿确认继续走已拍板的 context_version 与原子事务。
- 工具 schema 校验只证明形状合法，不证明健身业务事实合法。
- 领域服务必须在草稿生成和最终确认时执行确定性规则。
## 4. 核心数据合同
### 4.1 最小类型
以下是字段映射示意，省略了固定 role、部分元数据与 ContentBlock 定义；实际序列化合同仍以源类型和 golden fixtures 为准。
```python
@dataclass
class Usage:
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    reasoning: int | None = None
    cache_write_1h: int | None = None
    cost: "UsageCost" = field(default_factory=UsageCost)


@dataclass
class AssistantMessage:
    content: list[ContentBlock]
    provider: str
    api: Literal["openai-completions", "anthropic-messages"]
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp_ms: int
    error_message: str | None = None
    provider_thinking_level: str | None = None
```
- Python 字段序列化时映射回 pi camelCase，golden trace 比较统一 JSON。
- `reasoning` 是 `output` 子集，不重复加入 `total_tokens`。
- `cache_write_1h` 是 `cache_write` 子集，不重复加入总量；其成本按 `cost1h = 2 * inputRate`。
- 必填计数按各 adapter 的原逻辑归一；`reasoning`、`cacheWrite1h` 必须保留可选性。
- 上述两个可选字段用 `None` 表示 TS `undefined`，序列化时省略，不输出 JSON null。
- 聚合时两边均缺失则仍省略；否则将缺失一侧按 0 求和，显式 0 必须保留。
- Adapter 若明确产生 0，则照原值保存；不能对所有 Provider 一律补 0。
- 对必填字段的“未上报却归一为 0”，可在应用指标层另加 availability；不改变 pi wire 合同。
### 4.2 EventStream
```python
class EventStream(Generic[E, R]):
    def __aiter__(self) -> AsyncIterator[E]: ...
    async def result(self) -> R: ...
    def push(self, event: E) -> None: ...
    def end(self, result: R | None = None) -> None: ...
```
- 内部使用 `asyncio.Queue` 与一个独立 `Future[R]`。
- 即使调用者从不消费事件，也必须能等待 `result()` 得到终值。
- done/error 都完成 Future；provider 错误编码在 AssistantMessage 中。
- 低级队列只供观察，绝不能要求 SSE 消费者驱动执行。
- Core `push()` 必须同步、非阻塞，不能等待事件消费者；终态到达立即完成独立 Future。
- 首版保留原版非阻塞内存队列语义，不能用会等待容量的有界 Queue 偷换；零消费者仍可完成。
- 这是有明确内存上限风险的保真取舍：候选 Run/输出预算限制累计量，不声称原版自带有界背压。
- 持久 sink、文本分块和慢 SSE 客户端位于独立应用通道，不能反向卡住 core 的终值完成。
- 若以后给观察通道设容量，须另行定义从 SQLite 补读/通知合并策略，不丢核心语义事件。
### 4.3 live partial、事件快照与 frame
- provider 内部 `partial` 是同一个会持续 mutate 的共享活对象。
- 旧 Agent loop 的 `{ ...partialMessage }` 只是顶层浅拷贝，嵌套 content 仍可能共享，不能称事件时刻的深快照。
- 内核保留该合同；应用在入库/跨 SSE 边界时及时序列化或用 frame 编码，不把可变对象引用当历史记录。
- `AssistantMessageFrame` 是紧凑、可重放的增量记录，不等于事件快照。
- frame 不包含 done/error；终态消息必须独立结算。
- `toolcall_checkpoint` 是 partial JSON 文本进度，不是已验证工具参数。
- Web 不展示 thinking 内容、签名或 provider-native thinking level。
- 内部重放保留 `thinkingSignature`、redacted 标记和 provider thinking level。
- 重放必需的签名/内容保存在 SQLite 的内部模型上下文载荷，不可脱敏改写签名本身；它们不进入日志或 Web 轨迹。
- 内部记录的 API 投影必须按白名单重建，不能直接返回数据库 payload。
### 4.4 工具 schema 与权限
- 工具声明包含 name、description、JSON Schema、execution mode。
- 名称查找失败必须生成明确 error tool result，不尝试猜测。
- 验证顺序严格保持：clone → normalize optional nulls → Value.Convert 等价层。
- 对普通 JSON Schema 再做 pi 的 primitive/union coercion，最后 Check。
- 不自动应用 JSON Schema `default`，除非 pi 源行为明确如此。
- 不把 Pydantic 的默认填充、strictness 或 union 选择当作天然等价。
- 路线 A 使用 `beforeToolCall`/`afterToolCall`；权限检查发生在真实 effect 前。`before_tool`/`after_tool` 是路线 B 的另一组合同。
- 权限策略可以 block；block 结果仍形成 tool result 供模型理解。
- 正式业务写入工具不得存在；只暴露“创建/修正 Pending 草稿”工具。
- partial JSON 无论看似完整与否都不可执行。
- 只有完整终态 tool call 通过 schema、权限和领域校验后才执行。
## 5. Agent loop、事件和工具时序
### 5.1 推荐端到端序列
```text
HTTP accept
→ 短事务：查 client_request_id、检查全局活跃 Run、写 user message + pending Run
→ 创建 run task，占有全局 live slot
→ 条件更新 pending → running，追加 run_started
→ agent_start / turn_start
→ provider message_start / update* / message_end
→ 若 toolUse：完整参数校验、顺序 preflight、工具执行、结果源序物化
→ 下一 turn，或 final/error/abort
→ 条件终态事务
→ task 与无法终止的底层 effect 全部退出
→ 释放全局 live slot
```
### 5.2 旧 Agent/loop 必须保持的细节
- `Agent` facade 的监听器按注册顺序逐个 await。
- `message_end` 先经 Agent 状态 reducer 和监听器，再进入工具 preflight。
- `agent_end` 监听器全部完成后，Agent 才真正 idle。
- `prepareNextTurn` 只在确实还会启动下一 assistant turn 时执行。
- final、terminate 或 stop 后不能为了“清理”再调用它。
- parallel 模式先按源码顺序完成整批 preflight。
- 随后已准备的工具并发执行。
- `tool_execution_end` 按完成顺序出现。
- `toolResult` 消息按原 tool-call source order 加入 context。
- 任一工具声明 sequential 时，整批走 sequential。
- stopReason=`length` 且带工具调用时，全批一律不执行。
- 截断批为每个调用生成错误 tool result，提示模型重新发完整参数。
- `terminate` 只有整批所有最终结果均为 true 才提前停止。
- 某一个工具 terminate=true 不得提前终止同批或后续 turn。
### 5.3 新 Harness 事件不能误套到路线 A
- 新 Harness 的 hook 名称、持久阶段和旧 loop hooks 语义不同。
- 新 Harness 工具先独立 stage outcome，再按 source-prefix 物化。
- 它不是旧 parallel `Promise.all` 行为的简单重命名。
- 新 Harness 的 `message_end` 可早于持久 commit。
- 只有 `entry_added` 才证明该消息已进入 durable session。
- 新 Harness 事件总线隔离 handler 失败；旧 Agent 则按序 await listener。
- 路线 A 的 SSE “persisted” 含义必须由 SQLite sink 自己定义。
- 建议只在事务提交后发 `message_committed` 应用事件，避免借用 `entry_added` 名称混淆。
### 5.4 中间 assistant 轨迹的提交策略
- 已拍板“Assistant 消息与 completed 同事务”对工具中间 assistant 有歧义。
- 推荐待确认方案：运行中所有中间 assistant/tool 结构写内部 `run_events`。
- 这些事件必须足够重建本 Run 的完整临时 model context。
- Run 成功时，在一个终态事务提交该 Run 尚未入 `messages` 的 assistant/tool 序列与 completed；accept 已写的 user message 不重复插入。
- 失败或取消时，不把孤立 tool result 或半截 assistant 带入下次模型 context。
- Web 仍可从脱敏事件展示工具执行轨迹。
- 此策略不修改原决策；须由用户确认后才能成为实现合同。
## 6. Provider 适配
### 6.1 OpenAI-compatible wire
- endpoint 候选：`POST {base_url}/chat/completions`，使用 provider 配置的精确路径规则。
- 请求核心：model、messages、stream=true、tools、tool_choice。
- max token 字段按 compat 选择 `max_tokens` 或 `max_completion_tokens`。
- 支持时发送 `stream_options.include_usage=true`。
- Qwen thinking 可映射 `enable_thinking`、`reasoning_effort` 或 chat template kwargs。
- 只实现 DeepSeek 与目标本地 Qwen 验证所需 compat，不复制十二类全部方言。
- 工具 schema 的 `strict` 仅在 endpoint 支持时发送。
- 响应按 choice delta 合并 text、reasoning 与 tool_calls。
- 工具 arguments 增量只用于 UI/frame；完成前不校验执行。
- finish_reason 映射 stop/length/toolUse/error；未知值显式 error。
- usage 解析以 `parseChunkUsage` 为金标准。
- `cacheRead` 优先级：嵌套 `prompt_tokens_details.cached_tokens`。
- 其次 DeepSeek `prompt_cache_hit_tokens`，最后 top-level `cached_tokens`。
- compat endpoint 可从 `prompt_tokens_details.cache_write_tokens` 报 cacheWrite。
- `input=max(0,prompt-cacheRead-cacheWrite)`。
- completion_tokens 已含 reasoning；reasoning 不再加到 output。
### 6.2 Anthropic Messages wire
- endpoint 候选：`POST {base_url}/v1/messages`。
- header 包含 `x-api-key`、`anthropic-version` 和经确认的 beta features。
- 请求核心：model、messages、system、max_tokens、stream=true、tools。
- thinking/adaptive、output_config.effort 只按模型能力发送。
- system、工具和稳定历史的 cache_control 位置保持确定性。
- SSE 映射 message_start、content_block_start/delta/stop、message_delta。
- text、thinking、redacted_thinking、tool_use 均按 content index 聚合。
- `signature_delta` 与 block signature 保留供同模型历史重放。
- cacheRead=`cache_read_input_tokens`。
- cacheWrite=`cache_creation_input_tokens`。
- cacheWrite1h=`cache_creation.ephemeral_1h_input_tokens`，是 cacheWrite 子集。
- thinking tokens 是 output 子集。
- 消息级 stop_reason 不存在时不得伪造成功。
### 6.3 历史转换的当前源码语义
- “同模型”比较 provider + api + model 三者，缺一不可。
- 同模型有签名 thinking 原样保留，包括空文本签名块。
- 普通 thinking 跨模型降级为纯文本，不添加 `<thinking>` 标签。
- redacted thinking 跨模型直接丢弃。
- thoughtSignature 跨模型从 tool call 删除。
- error/aborted assistant 整条过滤，不参与下一请求。
- 孤儿 tool call 自动补 error toolResult，文本固定为 `No result provided`。
- tool-call ID 按目标 provider 约束规范化，toolResult ID 同步映射。
- 旧 README 的相反描述不得进入 Python 实现或测试预期。
### 6.4 缓存与长期记忆
- Prompt Cache 是 provider 对稳定请求前缀的缓存，不是应用长期记忆。
- 长期记忆来自 SQLite 已确认业务事实，并每次确定性投影到 context。
- system prompt、工具定义和历史前缀排序保持稳定以提高 cache hit。
- 当前用户消息、瞬态时间戳和 run-specific 文本放在稳定前缀之后。
- summary 请求使用 fresh sessionId 且 `cacheRetention="none"`。
- 不要用压缩摘要替代业务事实，也不要用 cache hit 推断记忆正确。
### 6.5 SDK 与直接 HTTP 的最小建议
- 候选一：官方 OpenAI/Anthropic async SDK，适配代码更少。
- 但必须将 SDK 内建 `max_retries=0`，防止与 pi 请求级重试叠加。
- 候选二：直接 `httpx.AsyncClient.stream()`，wire 与 SSE 控制更清楚。
- 最小推荐待确认：单一 `httpx` async client + 自写两种最小 SSE parser。
- 不引入 `httpx-sse` 框架，不引入 LangChain/LangGraph。
- 不手写所有 SDK 兼容层，只覆盖两个已拍板 wire 和验证目标。
- `httpx` 或 SDK 都是新依赖，必须在实现前由用户拍板。
## 7. 压缩：完整 prepare / generate / commit
### 7.1 触发与估算
- `shouldCompact` 必须严格使用 `contextTokens > contextWindow - reserveTokens`。
- 等号不触发，这是 deterministic golden 的边界条件。
- 默认 `reserveTokens=16384`、`keepRecentTokens=20000` 来自上游 Harness。
- 这些默认对小上下文 Qwen 可能不可行，不能盲抄到产品默认。
- 候选参数至少满足 reserve < contextWindow；还需校验摘要 + retainedTail + system/tools/领域事实 + 本轮输入 + 输出预留不超过窗口，不能只检查 reserve。
- 每次构造真实请求后重新估算；估算只是启发式，仍保留 provider overflow 处理和明确失败出口。
- 上线前对每个 model profile 做可行性检查；失败则拒绝保存配置。
- token estimate 以最近有效 assistant usage + 后续消息估算为主。
- 没有 usage 时使用上游约 4 chars/token 启发式。
- Python `len` 是 Unicode code points，TS `string.length` 是 UTF-16 code units。
- 为差分一致，估算长度应实现 UTF-16 code-unit 计数。
### 7.2 prepare
- 只处理当前有效 context，不删除历史 messages/run_events。
- 最近 compaction entry 存在时读取 `previousSummary` 和 `retainedTail`。
- 下一次压缩把旧 retainedTail 虚拟接到新条目之前参与 cut。
- 合法 cut 可位于 user、assistant 和若干自定义消息前。
- 合法 cut 不允许位于 toolResult 前，避免断开 tool-call/result 关系。
- 从尾部累计估算 token，寻找约 keepRecentTokens 的 cut。
- 若 cut 在一个 turn 中间，向前找到 user/turn start。
- 输出 `messagesToSummarize`、`turnPrefixMessages`、`retainedTail`。
- 输出 `isSplitTurn`、`tokensBefore`、`previousSummary`、settings。
- 新 compaction entry 自包含 summary + retainedTail。
- 它不依赖 `firstKeptEntryId` 才能重建有效 context。
### 7.3 generate
- 普通摘要上限为 `floor(0.8*reserve)`，split-turn prefix 为 `floor(0.5*reserve)`；`model.maxTokens > 0` 时再取两者较小值，否则按原算法不施加该模型上限。
- split-turn 时先串行生成历史摘要，再生成 turn-prefix 摘要。
- 不得并发，因为第二阶段的语义和可观测顺序应确定。
- previousSummary 仅进入历史摘要更新 prompt。
- prefix 摘要说明 retained suffix 所需上下文。
- 两次请求分别产生 usage，最终相加为一次 compaction 的 summary usage。
- 每个 provider request 在 usage ledger 中只计一次，不能因事件重放重复计费。
- 摘要失败或取消时返回错误，不提交新 compaction。
### 7.4 commit 与失败安全
- generate 成功后才执行短 SQLite commit。
- 建议先在现有 SQLite 内部事件中保存自包含 compaction 载荷，按会话查询最新成功记录；具体 SQL/索引待持久化合同确认，不默增通用 Session 表体系。
- 原 `CompactResult` 包含 summary、retainedTail、tokensBefore、usage 等；tokensAfter 是应用另行估算的展示指标，不伪称上游字段。
- commit 不删除原历史；context projector 从最新成功 compaction 开始构造有效上下文。
- commit 必须再次检查 Run 尚未取消；取消先赢则丢弃待提交摘要，usage 可记录但不得发布新上下文边界。
- 事务失败只保证旧 context/旧 compaction 完好，不允许在持久化不可用时继续新 effect；终止执行并走存储故障路径。
- 用户界面在 commit 后显示“已发生上下文压缩”。
- summary 永远不是档案、限制、计划版本或待确认草稿的事实源。
- 这些事实每轮从领域表重新注入，确保摘要遗漏不破坏安全边界。
### 7.5 overflow 单次恢复
- 正常轮间阈值压缩可接到下一 turn 的 `prepareNextTurn`；旧 loop 自身不实现自动压缩。
- 该 hook 不覆盖新 Run 的第一轮：应用在首次模型调用前另做同一有效上下文检查，避免续聊已满窗口时漏检；这是公开的应用补充策略。
- provider 明确 context overflow 时允许一次应急压缩后重试该 assistant turn。
- `length + output=0` 且窗口满等上游判据可进入同一恢复路径。
- 每个 assistant turn 只允许一次 overflow recovery，防止无限压缩循环。
- 若没有安全 cut、摘要失败或压后仍溢出，终止为明确错误。
- 不能把 overflow 恢复算作普通 transient retry。
## 8. 重试、超时、预算与错误分级
### 8.1 重试层级候选
- provider request retry 默认 `maxRetries=0`，与 pi 当前默认一致。
- 若启用，请求级仅覆盖连接错误、408、409、429 和 5xx 等瞬态错误。
- 尊重 `x-should-retry`，并解析 `retry-after-ms` / `retry-after`。
- 服务端要求的 delay 超过上限时直接失败；上限具体数值待确认。
- assistant 整轮 retry 是另一层，只对分类为 transient 的 error 生效。
- aborted、quota/billing/budget、schema/permission/domain 错误不得重试。
- overflow 走专用单次恢复，不走普通 retry。
- 不增加“整个 Run retry”第三层；手动重试创建新 Run。
- SDK 自动重试必须关闭，否则会形成隐藏第三层。
### 8.2 timeout 候选
- 分开配置 connect、first-byte、idle-read、overall request 和 tool timeout。
- 数值均待真实 DeepSeek/Qwen/Anthropic 测量后确认。
- timeout 是 `provider_timeout` 或 `tool_timeout`，不能伪装成用户 aborted。
- 用户取消才产生 Run `cancelled`。
- retry exhaustion、协议错误、验证错误分别保留机器错误码。
- Web 只显示可理解且脱敏的错误摘要。
### 8.3 预算候选
- 候选维度：max turns、provider requests、tool effects、tokens、elapsed time、cost。
- 具体数值不在本文批准。
- 预算在安全 turn 边界检查，并在每次真实 effect admission 前再检查。
- 已 admit effect 可收尾；预算耗尽后禁止新 provider/tool effect。
- summary 请求也计入 provider request、token、cost 预算。
- 工具批应在 preflight 阶段为每个 effect 做准入，防止超预算新启动。
### 8.4 Python 取消与资源收尾
- invocation 取消、Run 取消、SSE 断开是三个不同信号。
- SSE 断开只停止该订阅生成器，不触发 Run task cancel。
- Run 取消设置共享 cancellation event，并 cancel 可中断的 provider task。
- `asyncio.CancelledError` 继承 `BaseException`，cleanup 后必须重新抛出。
- 禁止 `except BaseException` 吞掉取消。
- 只有协程明确 suppress cancellation 时才涉及 `uncancel()`，不是普遍步骤。
- async generator 由拥有者在 finally 中 `aclose()`。
- HTTP response stream 和 client response 在 finally 中关闭。
- `asyncio.to_thread()` 的 awaiter 被取消，不代表底层线程已停止。
- 这是由取消注入点与 executor join 语义推导出的约束，不冒充官方原句。
- 底层同步 effect 实际退出前，不释放全局 live slot。
- cancelled 但 draining 的 Run 对新不同请求仍返回 409。
- 不能仅靠 SQL pending/running 的 partial index 判断实时槽位。
- 同 `client_request_id` 始终先返回原 Run，包括任何终态。
- 取消后不得新增 Assistant message，也不得写 completed/failed 等其他终态。
- 迟到 trace/usage 只可脱敏结算到内部事件，不得“复活”Run。
## 9. SQLite、SSE 与安全
### 9.1 单连接事务纪律
- FastAPI lifespan 只持有一个 `aiosqlite` connection。
- 所有读写都经过同一个 `asyncio.Lock`。
- 多语句事务从 BEGIN 到 COMMIT/ROLLBACK 全程持锁。
- 模型请求、工具执行、hook、SSE yield 期间绝不持锁。
- pending accept：先按 client_request_id 查重，再检查 live slot/活跃 Run。
- user message 与 pending Run 在同一事务创建。
- pending→running、running→terminal 都用条件 UPDATE。
- 条件更新失败说明取消或竞态已赢，当前 task 不得覆盖终态。
### 9.2 final/cancel/restart
- final 成功事务按待确认的中间轨迹策略写 Assistant 序列与 completed。
- cancel 事务把 pending/running 条件更新为 cancelled，并追加取消事件。
- cancel 返回后 task 可能仍 draining；slot 到真实 effect 退出后才释放。
- 启动迁移后在单事务将遗留 pending/running 改 failed。
- 机器码固定 `interrupted_by_restart`，并追加对应持久事件。
- 不恢复旧 task，不自动重试，不重放外部副作用。
### 9.3 SSE 持久事件
- `run_events.id INTEGER PRIMARY KEY AUTOINCREMENT` 作为 SSE event id。
- SSE 始终按最后已发送 id 查询 SQLite；先注册通知，再补读，发送后推进 cursor。
- 通知是可合并的唤醒提示，不承载唯一事件；等待前再次检查数据库，并保留超时补查，防止补读/订阅窗口丢事件。
- 仅当前 Run 的终态已发出且无待补读事件时结束订阅；SSE 断开不改变执行句柄。
- 事件 payload 采用明确白名单，不直接 dump Python 对象或 provider body。
- 文本按块持久化，不按 token 每行写库。
- thinking、签名、API key、Authorization、原始 headers 不进入 Web payload。
- 工具结果的 Web 投影只发送展示摘要；§5.4 的内部事件仍保存重建临时 context 所需的结构化载荷，二者不能混为一份脱敏摘要。
- 内部事件同样位于既定 SQLite，禁止 SSE 按 event_type 无过滤透传；具体内部载荷分类随 §5.4 一并确认。
- `run_events` 是执行审计与 SSE 补读源，不是正式健身事实源，也不是上游原始 HTTP/SSE 字节副本。
### 9.4 internal context 与 Web 展示
- 内部 context 保留模型重放必要的 tool call、tool result、签名和 effort。
- Web message 只包含用户可见文本、草稿卡和脱敏工具摘要。
- 隐藏思维链永不通过 API 返回。
- Provider API key 只在调用时从配置读取。
- Key 不进入日志、异常详情、trace、run_events 或响应。
- Provider 查询默认只返回 `has_api_key`。
## 10. 分阶段实施计划
### Phase 0：冻结 profile 与 source manifest
- 用户确认路线 A、依赖选择和中间 assistant 提交策略。
- 建立 `source-manifest.json`，记录 tag SHA、snapshot SHA、目标文件和许可证。
- 为每个移植函数记录原 TS 路径与源 commit。
- 复制/改写代码时保留 MIT copyright 与许可告知。
- 注意本地包没有随附 LICENSE 文件，发布前须从权威上游补齐 MIT 文本。
- 验收：manifest 可定位每个核心 Python 模块的 TS 来源。
### Phase 1：纯数据合同与 deterministic utilities
- 实现 messages/content/model/usage/cost。
- 实现 EventStream、frame encoder/reducer、partial JSON。
- 实现 transform、validation/coercion、estimate、overflow。
- 不连接网络、不连接 SQLite。
- 验收：TS↔Python 固定 JSON fixtures 全量对齐。
### Phase 2：两种 provider wire
- 建立 byte/line fixture 驱动的 SSE parser。
- 实现 OpenAI-compatible request/response/usage。
- 实现 Anthropic request/response/cache/signature/effort。
- 实现 provider-level cancellation 与资源关闭。
- 验收：provider payload、事件序列、final message、usage 均对齐。
### Phase 3：Agent/loop 与工具
- 实现 Agent facade、listener 串行等待和 idle。
- 实现 sequential/parallel tool batch。
- 实现 schema、权限、领域工具 adapter。
- 实现 length 全批拒绝和 terminate 全批规则。
- 验收：脚本 provider 的 deterministic golden traces 对齐。
### Phase 4：压缩
- 先移植 prepare/cut/split-turn 的纯函数。
- 再接 summary request 和 usage 汇总。
- 最后接有效 context projector 与 commit。
- 验收：阈值、cut、previous summary、retained tail、失败回退全部对齐。
### Phase 5：Run/SQLite/SSE 集成
- 实现 migration、repo、幂等 accept、live slot、cancel/draining。
- 实现内部事件与 Web 白名单事件投影。
- 实现 final/cancel/restart 条件事务。
- 验收：状态机、终态竞态、SSE 断线补读和重启失败行为。
### Phase 6：真实 provider 与端到端验收
- DeepSeek OpenAI-compatible 真实调用，不以 mock 替代。
- 至少一个本地 Qwen 小模型 HTTP 端点真实调用，不以 mock 替代。
- Anthropic 至少做真实 wire 验证；是否要求付费完整行为测试待确认。
- 记录 endpoint 版本、模型 ID、compat 配置与脱敏结果。
- 验收：真实 text、tool call、usage/cache、cancel 和一次长上下文场景。
## 11. 差分测试与故障注入矩阵
### 11.1 比较原则
- 不比较 LLM 自然语言“看起来差不多”。
- 比较 provider request payload、事件类型序列和最终结构化消息。
- 比较工具调用参数、结果 source order 和完成事件 completion order。
- 比较 Run 状态序列、错误码、usage/cache/cost 和 compaction 元数据。
- ID 与时间戳可通过固定 clock/ID generator 归一化。
- 归一化不得删除事件顺序、usage 字段或真实错误分类。
- 浮点 cost 使用明确容差；token 整数必须精确一致。
### 11.2 必测用例
- EventStream 不消费事件直接等待 result。
- start 前错误、流中 error、aborted、done 四种终态。
- shared partial 的嵌套引用语义与 TS 一致；应用已序列化的持久事件不被后续 mutation 改写。
- 零事件消费者、大量事件时仍能 `result()`；未来观察通道超容量不能阻塞核心终态。
- Usage 可选字段的双缺失、单侧缺失、显式 0 与混合聚合均保持序列化差异。
- frames round-trip；terminal 不在 frame；thinking level/signature 保留。
- transform 同模型三元组比较与跨模型 thinking 降级。
- redacted thinking 丢弃、error/aborted assistant 过滤。
- 孤儿工具结果补 `No result provided`。
- null vs absent、optional 非 nullable 字段的 null 删除；required null 依原 schema/coercion 结果判定，不能一律拒绝或一律转 0。
- anyOf/oneOf coercion 的成员顺序与失败回退。
- bool/int/string 转换边界，不应用 schema defaults。
- 中文、emoji、代理对的 UTF-16 长度估算。
- JSON 序列化 key/order/escaping 与工具参数尺寸。
- 价格浮点、`cost1h = 2 * inputRate` 成本。
- OpenAI cacheRead 三种字段优先级与负 input clamp。
- reasoning/output、cacheWrite1h/cacheWrite 子集不重复计数。
- length + 多工具：全批不执行。
- parallel 乱序完成：end completion order、message source order。
- sequential 工具存在时整批串行。
- 路线 A 的 beforeToolCall block、afterToolCall patch、全批 terminate；不混用路线 B 的 hook 名称。
- abort-first 与 effect-first 两种 gate 竞态。
- cancel 与 completed/failed 终态竞态只能有一个赢家。
- cancelled draining 时新 Run 仍 409。
- 同 client_request_id 在 pending/running/终态都返回原 Run。
- SSE 断开不取消；重新连接按 event id 补读。
- 工具 effect 成功后进程退出：按路线 A 标 failed，不自动重放。
- 失败/取消的孤立工具轨迹不进入下一模型 context。
- compaction 等号不触发、超过一 token 触发。
- cut 可在 assistant 前、不可在 toolResult 前。
- split-turn 两次摘要严格串行，max token 分别 0.8/0.5 reserve。
- previousSummary + retainedTail 参与下一次压缩。
- 摘要失败保留旧有效 context。
- overflow 只恢复一次，普通 retry 不吞 overflow。
- 稳定 prompt 前缀与 cache key/session id。
- summary fresh sessionId + cacheRetention none。
- timeout 不映射 cancelled，用户 cancel 不映射 failed。
- API key、thinking、signature 不出现在日志/SSE/HTTP response。
### 11.3 故障注入点
- accept 事务提交前后。
- pending→running 条件更新时并发 cancel。
- provider headers 后、首事件前、半个 tool JSON 时断流。
- assistant final 已获得但终态事务前 cancel。
- parallel 工具一个完成、一个阻塞、一个抛错。
- `to_thread` awaiter cancel 而线程继续运行。
- summary 第一次请求成功、prefix 请求失败。
- compaction generate 成功、commit 失败。
- SQLite busy/rollback、SSE listener 慢或断开。
- completed 更新行数为 0，验证不追加 Assistant message。
## 12. 未来测试命令示例（本次未执行）
```bash
# Python 单元与差分 fixture；路径以未来工程为准
python -m pytest tests/agent_core -q
python -m pytest tests/golden/test_ts_python_traces.py -q
python -m pytest tests/providers/test_openai_compat_wire.py -q
python -m pytest tests/providers/test_anthropic_wire.py -q
python -m pytest tests/runtime/test_cancel_races.py -q
python -m pytest tests/runtime/test_compaction.py -q
# 真实调用，必须显式 opt-in，密钥不得打印
FIT_AGENT_E2E=1 python -m pytest tests/e2e/test_deepseek_live.py -q
FIT_AGENT_E2E=1 python -m pytest tests/e2e/test_qwen_local_live.py -q
FIT_AGENT_E2E=1 python -m pytest tests/e2e/test_anthropic_wire_live.py -q
```
- 上述是未来建议命令，不代表本次已安装依赖或执行测试。
- 本次不运行缺依赖的 TS build，也不安装 npm/Python 包。
- golden fixtures 应从目标 TS 函数生成并随 source SHA 固定。
## 13. 集中待确认决策
1. 是否确认路线 A：Fit-Agent AgentCore 兼容 profile，而非完整 durable Harness；该选择仍未批准。
2. 是否确认成功时统一提交完整本 Run 对话序列；失败/取消只保留内部脱敏轨迹。
3. Python provider 层选官方 async SDK，还是单一 `httpx` 直连两个 wire。
4. JSON Schema validator 与 partial JSON 是否允许新增依赖；若允许，具体选择什么。
5. 是否启用请求级 retry、assistant retry；各层次数、delay 上限与 timeout 数值。
6. 每种模型 profile 的 contextWindow、reserveTokens、keepRecentTokens、max output 候选值。
7. 执行预算维度与具体上限，以及预算耗尽的机器错误码。
8. SSE 事件全集、chunk 大小、慢订阅者策略和心跳间隔。
9. Anthropic 首版是仅 wire conformance，还是必须纳入真实付费端到端验收。
## 14. 明确非目标
- 不实现 durable AgentHarness 的 13 叶子状态机。
- 不实现恢复、safe replay、deferred、navigation、fork、branch、multi-lane。
- 不实现 steer/followUp/nextRun queue。
- 不移植 coding-agent 工具、TUI、扩展生态、子 Agent 或 JSONL 会话副本。
- 不引入 LangChain、LangGraph、Redis、Celery、ORM 或额外 worker。
- 不把 `run_events`、模型摘要或 Prompt Cache 当业务事实源。
- 不让自然语言确认绕过正式草稿确认接口。
- 不在本文批准依赖、参数或尚未拍板的业务表结构。
## 15. 实施完成定义
- source manifest 能追溯到 v0.85.0 tag 与当前 snapshot 差异。
- 两种 wire 的固定输入可产生 deterministic payload/event/final-message golden。
- Agent loop 的工具、length、terminate、listener 和 prepareNextTurn 时序对齐。
- 压缩 prepare/generate/commit 与上游纯算法边界对齐。
- SQLite 幂等、条件终态、cancel draining 和 restart failed 通过故障注入。
- DeepSeek 与本地 Qwen 真实调用通过，Anthropic wire 测试通过。
- Web 永不暴露 API key、隐藏 thinking 或签名。
- 正式健身事实只由领域事务产生，不依赖摘要。
- 所有有意偏离都在 manifest/ADR 引用中明示，不能伪称全量 Harness 等价。

## 16. 可复核的一手来源
以下网络来源由研究子代理核实；版本结论采用提交 SHA 而不是会移动的 main 名称。
- [pi v0.85.0 固定提交](https://github.com/earendil-works/pi/tree/107d79f11072bbc8a3a757ed7fd69596bee7d68c)：目标发行版。
- [本次源码快照固定提交](https://github.com/earendil-works/pi/tree/9841914c71a74d81abe07f751aefd271fd924e63)：实际读取版本；§1 披露与 tag 的差异。
- [Python asyncio exceptions](https://docs.python.org/3/library/asyncio-exceptions.html)、[Tasks](https://docs.python.org/3/library/asyncio-task.html)：取消与 to_thread 的语义；目标 Python 版本仍须在实现时锁定。
- [Python 异步生成器语言参考](https://docs.python.org/3/reference/expressions.html#asynchronous-generator-functions)：aclose 与清理责任。
- [OpenAI Python SDK](https://github.com/openai/openai-python)、[Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)：本次读取时默认重试 2 次；这些链接未固定 SDK 版本，安装时必须重新核对并锁定。
- [DeepSeek Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion)：prompt cache hit/miss 字段；真实流式上报时机仍由 adapter fixture 与实测验收。

## 17. 本次交付的验证边界
- 已完成源码快照核对、独立方案审查，并修正 EventStream 可完成性、Usage 可选字段及源码定位问题。
- 完成 Markdown 结构、引用路径/行号范围和关键合同断言检查；本次只交付文档。
- 尚无 Python 实现，也未执行 TS↔Python 差分或真实 Provider 调用；不能据方案审查宣称运行时行为已等价。
