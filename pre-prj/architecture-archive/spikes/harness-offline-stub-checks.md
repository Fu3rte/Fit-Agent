# 历史归档：Harness 四项离线桩验证（2026-09-08，四项通过）

> 本文件为第 12 章「历史归档」正本之二；内容逐字搬运自根 `PLAN.md`「Harness 离线桩验证授权」（L164–184），正文文字保持原样，段落软换行与标题层级按归档体例重排。授权决策与生产边界正本归 `architecture/08-agent-runtime.md` 8.5；本文件仅存历史验证记录，不构成任何实现授权，不以实测通过扩大产品实现范围。验收结果以结论 + 证据指针记录。

## 授权决策（2026-09-08）

| 决策 | 选项 | 选了 | 为什么 |
|---|---|---|---|
| 重试与纠错机制验证 | 仅凭静态源码定参数 / 先做四项离线桩验证 | 四项离线验证（2026-09-08） | 核实事件流纠错、实际发送次数、取消及副作用重复边界，再讨论生产参数；不以源码推断替代运行证据 |

## 授权与范围

- 本次仅授权新增最小离线测试与脱敏结果记录；使用现有 spike 专用隔离环境，其当前实际路径为 `test/spike/.venv`。不安装、升级依赖，不修改 backend 环境或业务实现。
- 所有模型与 HTTP 响应使用本地桩或假传输，不发起真实网络请求、不加载 API Key；只用合成非健康数据。重试次数、Token 和费用数值仅作测试输入，不确定或放宽生产默认值及真实调用护栏。
- 验证范围：① 事件流下可修正校验失败是否触发纠错，每次尝试是否进入预算计数；② SDK 禁重试后一次调用是否只有一次实际发送；③ 退避或纠错期间取消是否禁止新尝试；④ 工具已产生结果后发生错误是否造成重复写入。
- 第④项须分别说明框架默认行为与最小状态核对/幂等桩的效果，不能把测试桩的去重能力说成框架自带能力。费用仅验证合成用量或预算行为，不宣称真实计费验证。
- 验收：四项均有可重复命令、明确断言与实测结果；未满足项如实列为缺口，不为测试通过扩大产品实现。验证成功不等于批准业务模块、完整 Harness、Agent 测评或真实模型调用。

## 实测结果（2026-09-08，四项离线桩验证通过，仅记录测得事实，不含任何新决策）

- 环境与版本：`test/spike/.venv`（CPython 3.13.15，uv 0.12.3）；pydantic-ai-slim 2.40.0、openai 3.8.0、httpx2/httpcore2 2.12.0、pytest 9.1.1。全程 `unshare -rn` 进程级断网 + socket 断网守卫；无真实网络、无 API Key 加载、无业务库写入；副作用仅限内存合成存储。
- 测试与证据：新增 `test/spike/tests/test_harness_offline_stub_checks.py`（13 项测试全绿）；既有 75 项离线测试回归通过，合计 88 passed。复现命令：`cd test/spike && unshare -rn .venv/bin/python -m pytest tests/ -q`。证据：`test/spike/evidence/harness-offline-stub-checks.md`。
- ① 事件流（run_stream_events）纠错与共享预算：坏参数尝试工具零执行（副作用前拦截）；纠错请求发出并计入 `usage.requests`（3 次模型尝试 = requests = 3，终稿完成）；`request_limit=1` 时纠错请求发送前被拒（UsageLimitExceeded，1 次尝试、工具零执行）；`request_limit=2` 时纠错被允许且执行一次写入，随后终稿请求被拒 —— 纠错与正常请求共享请求预算。请求预算≠副作用前成本预留，预留/真实计费不在本次验证范围。
- ② SDK 实际发送：AsyncOpenAI `max_retries=0` 时 5xx 与传输错误各恰好 1 次实际发送（InternalServerError / APIConnectionError）；对照 openai 3.8.0 默认 `max_retries=2` 对同一 5xx 隐式重发共 3 次（SDK 层隐式重试，应用与框架 usage/cost 均不可见）；4xx 永久错误默认亦不重发（1 次发送）。
- ③ 取消：纠错请求执行前取消 → 新模型/工具尝试零发生（RunCancelled，requests=1）；测试自有门控退避等待期间取消 → 同样零新尝试（放行门控的对照可正常发出纠错请求，证明是取消而非结构导致）；取消时进行中的工具任务被取消并排干（finally 执行、取消前已完成副作用保留、取消后无后续尝试）。
- ④ 写入后报错：工具写入后抛可纠错错误、纠错请求中模型重发同一工具 —— 框架原生无幂等/去重层，两次执行均真实写入（writes=2）；同一模型流程套最小状态核对/幂等桩后写入仅 1 次（writes=1，工具仍被调用 2 次）—— 去重来自适配层桩，非框架自带能力。
- 未满足/边界（如实）：模型为本地脚本桩，非真实模型；重试/纠错次数与预算数值均为测试输入，不确定生产默认值、超时与错误映射；SDK 隐式重试的费用不可见、请求前费用预留、真实计费均不在本验证范围；Anthropic 未安装无法静态核对。

## 证据指针

- 测试：`test/spike/tests/test_harness_offline_stub_checks.py`（13 项全绿；与既有 75 项离线测试合计 88 passed）
- 证据报告：`test/spike/evidence/harness-offline-stub-checks.md`
- 复现命令：`cd test/spike && unshare -rn .venv/bin/python -m pytest tests/ -q`
- 来源：根 `PLAN.md`「Harness 离线桩验证授权」L164–184；设计决策与生产边界正本：`architecture/08-agent-runtime.md` 8.5
