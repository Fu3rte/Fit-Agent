# Harness 四项离线桩验证证据（2026-09-08 授权）

本文件为 PLAN.md「Harness 离线桩验证授权（2026-09-08）」小节对应证据。全部基于实装隔离环境
`test/spike/.venv` 离线运行；无真实网络、无真实模型调用、无 API Key 加载、无业务库写入。

## 1. 环境与版本（实测）

| 项 | 值 |
|---|---|
| 解释器 | `test/spike/.venv`，CPython 3.13.15（uv 0.12.3，include-system-site-packages=false） |
| pydantic-ai-slim | 2.40.0（pydantic-graph 2.40.0、pydantic 2.13.5、genai-prices 0.1.6、anyio 4.15.1） |
| openai | 3.8.0（httpx2 传输） |
| httpx2 / httpcore2 | 2.12.0 |
| pytest | 9.1.1（无 pytest-asyncio，采用 tests/ 既有 `asyncio.run` 风格） |
| 静态检查 | ruff 0.16.6（~/.local/bin）、pyright（~/.local/bin，pyrightconfig.json venvPath=.venv） |

网络隔离：每条命令以 `unshare -rn` 进程级断网运行；测试模块内另有 autouse socket 断网守卫
（DNS/connect 直接抛 OSError）。未安装/升级任何依赖，未修改 backend 环境或任何业务实现。

## 2. 复现命令与结果

```bash
cd /home/finnian/code/agent/Fit-Agent/test/spike

# 新增四项检查（13 项测试）
unshare -rn .venv/bin/python -m pytest tests/test_harness_offline_stub_checks.py -v
# -> 13 passed in ~1.3s

# 既有离线回归（不含新文件 = 75 项；含新文件全量 = 88 项）
unshare -rn .venv/bin/python -m pytest tests/ -q --ignore=tests/test_harness_offline_stub_checks.py
# -> 75 passed
unshare -rn .venv/bin/python -m pytest tests/ -q
# -> 88 passed in 2.40s

# 静态检查
ruff check tests/test_harness_offline_stub_checks.py        # All checks passed
pyright tests/test_harness_offline_stub_checks.py           # 0 errors, 0 warnings
.venv/bin/python -m py_compile tests/test_harness_offline_stub_checks.py   # OK
```

退出码全部为 0；新文件 13 项测试 + 既有 75 项回归全绿，无失败/跳过/xfail。

独立审查指出两处取消场景在启动事件前的等待缺少超时保护；已将启动等待和 driver 执行纳入同一个 `asyncio.timeout`，并在 `finally` 取消及排干 driver。修正后父代理复跑上述全量断网命令：`88 passed in 2.00s`；ruff 通过，pyright 为 0 errors、0 warnings。审查意见不改变四项行为结论。

## 3. 四项实测结论

### ① 事件流（run_stream_events）下可修正校验失败 → 纠错发生，且每次尝试进入共享请求预算

场景：模型第一次请求发出 `record(topic='bad')`（非法参数，schema 为 `Literal['good']`），
工具参数副作用前校验失败 → 工具零执行；框架以 RetryPromptPart 反馈模型；第二次（纠错）请求
发 `record(topic='good')` 成功执行；第三次请求输出终稿。

实测（断言全过）：

| 变体 | 结果 |
|---|---|
| 不限（默认 UsageLimits） | 模型尝试 3 次 = `usage.requests` 3；坏参数尝试写入 0 次；纠错后工具执行 1 次（writes=`['good']`）；纠错请求带 RetryPromptPart；Run 完成 |
| `UsageLimits(request_limit=1)` | 纠错请求在发送前被拒 → `UsageLimitExceeded`；模型尝试 1 次；`usage.requests` 1；工具 0 执行 |
| `UsageLimits(request_limit=2)` | 初始 + 纠错请求被允许（纠错执行 1 次写入），随后的终稿请求被拒 → `UsageLimitExceeded`；模型尝试 2 次；`usage.requests` 2 |

结论：run_stream_events 后台 run() 语义确实支持纠错（未假设 run_stream 与 run_stream_events
同构）；每次纠错请求计入 `RunUsage.requests`，与正常请求共享 `UsageLimits.request_limit`；
预算耗尽在请求发送前检查并拒绝。注意：该拒绝发生在副作用之后是可能的（request_limit=2 变体），
即请求预算不是“副作用前成本预留”——真实预留/计费语义不在本验证范围。

### ② AsyncOpenAI 禁重试 → 一次逻辑调用恰好一次实际发送

假传输（httpx2.AsyncBaseTransport 注入 `http_client`）计数每次实际发送并返回脚本化响应；
base_url 为本地不可路由占位地址 `http://127.0.0.1:9/v1`；API key 为合成占位串。整个测试进程
处于断网环境（见 §1），真实端点不可能被调用。

| 配置 | 假响应/错误 | 实际发送数 | 抛错类型 |
|---|---|---|---|
| `max_retries=0` | HTTP 500 | 1 | `InternalServerError` |
| `max_retries=0` | 传输层连接失败 | 1 | `APIConnectionError` |
| `max_retries=2`（SDK 默认） | HTTP 500（`Retry-After: 0.001` 仅压缩内建退避） | 3 | `InternalServerError` |
| `max_retries=2`（SDK 默认） | HTTP 400（永久错误） | 1 | `BadRequestError` |

结论：`max_retries=0` 时 SDK 层只发送一次，不再静默重发（5xx 与传输错误均如此）；对照证明
openai 3.8.0 的默认隐式重试（`DEFAULT_MAX_RETRIES=2`）会对外不可见地重发同一请求——若首次
请求服务端已处理而响应丢失，可能产生服务端侧重复处理或额外费用，框架 usage/cost 未必完整覆盖；本次未验证服务端幂等或计费行为。这是 SDK
原生重发行为，须由适配层显式关闭或纳入预算（spike 已用 max_retries=0）。本次 HTTP 400 永久错误不重发属
SDK 既有分类，与“永久错误不重试”的生产原则一致。

### ③ 纠错/门控退避期间取消 → 禁止新尝试；进行中任务被取消并排干

- 3A（框架级取消，纠错请求边界）：工具执行后抛可纠错错误；在带 RetryPromptPart 的纠错
  ModelRequestNode 执行前调用 `AgentRun.cancel()` → 上下文以 `RunCancelled` 退出；模型尝试
  1 次（纠错请求从未发出）、`usage.requests`=1、工具写入 1 次。
- 3B（测试自有“重试门控延迟”）：在纠错节点前挂起于测试门控（模拟未来适配器的有界退避窗口，
  框架自身没有内建退避延迟）。取消时门控等待被打断 → `RunCancelled`，模型尝试 1 次、写入 1 次；
  对照（放行门控）纠错请求正常发出（模型尝试 2 次、Run 完成），证明是“取消”而非结构导致。
- 3C（取消时工具任务在飞行）：同一响应并行 `blocked`（挂起于永不触发的 Event）与 `fast_write`
  （写入一次）。取消 → `RunCancelled`；`blocked` 被取消并排干（finally 已执行）；`fast_write`
  已完成副作用保留；取消后模型/工具尝试数为 1（无后续尝试）。

区分：3A/3C 证明的是框架级取消语义（AgentRun.cancel / iter 上下文）；3B 证明的是适配层若自建
“门控退避”必须把取消纳入等待策略——该门控是测试自有代码，不构成框架能力或生产重试实现。

### ④ 工具“写入后报错”模型重发同一工具 → 原生重复写入风险，状态核对桩可收敛

场景：工具 `apply_payment(K1, 10)` 先写入合成内存存储，随后抛 `ModelRetry`（模拟“已写入但提交
确认丢失”）；纠错请求中模型（脚本）再次发出同一工具调用。

| 变体 | 模型尝试 | 工具执行次数 | 合成写入 |
|---|---|---|---|
| 框架原生（无幂等） | 3 次（初始+纠错重发+终稿），requests=3 | 2 | `[('K1',10), ('K1',10)]` |
| 同流程 + 最小状态核对桩 | 3 次，requests=3（行为相同） | 2 | `[('K1',10)]` |

结论：框架原生不保证“写入结果不确定”时无重复——框架只在工具失败后生成纠错请求，由模型决定
是否重发同一工具；模型重发时框架不拦截、不去重、无状态核对钩子，第二次执行同样真实写入。
最小状态核对/幂等桩（写入前查已存在则跳过）把写入收敛为一次。该去重属适配层（工具实现）
责任，不能宣称框架自带幂等；生产“写入结果不确定先核对状态”必须由工具/适配层实现。

## 4. 覆盖范围与如实缺口

- 模型为本地 `FunctionModel` 脚本桩 + openai SDK 假传输，非真实模型；结论不证明真实模型行为。
- 重试/纠错次数、request_limit 数值、Retry-After 0.001、门控均为测试输入，不设定/放宽生产参数。
- 费用只体现合成 usage/请求预算行为；SDK 隐式重试费用不可见、请求前费用预留、真实计费、真实
  服务端幂等键支持均未验证。
- run_stream（流式消费直连）与 run_stream_events 的行为差异未在此对比（本次只实测事件流路径，
  未假设 run_stream 与之同构）。
- Anthropic 未安装，无法静态核对；DeepSeek 服务端对 SDK 幂等键的支持未验证。
- 取消测试均为同进程确定性驱动（asyncio Event/门控，无 sleep 竞态）；真实分布式/服务端取消
  计费语义未验证。
