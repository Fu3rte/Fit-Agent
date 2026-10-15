# T6 当前实现与验证边界

## 1. 验收状态

当前配置加载、lookup 条件、资源循环归属与关闭、真实工具取消后停止编码均已复核。独立 worker 的真实 Event/task.cancel 检查退出 0，主会话已读取原始命令及结果。

在途编码取消已接入 HybridRetriever.search；runtime 仅负责资源与委托。经用户授权，独立 worker 完成真实 SDK 在途取消验证，退出 0；本轮 T6 修复项验收通过。尚未运行的业务回归继续保持未验证状态，不能将本次检查解释为全套回归通过。

本次业务源码未由主会话修改，实际测试全部由独立 worker 执行。真实 Embedding POST 请求累计 1 次，provider usage 未返回、计费未知，取消请求可能计费。

## 2. 当前接口与模块

- search_exercises：purpose 必填 lookup/generation/replacement，保留语义输入 name；四个精确字段，以及 task_environment、available_equipment、unavailable_equipment、excluded_ids。
- 搜索结果为 items/has_more/remaining_ids；候选字段为 id/name/equipment/target/matches，最多 10 条，完整 JSON 预算 4000 字符。
- get_exercise_details(ids)：1..5 个，按输入顺序读取原 Catalog 九字段，完整 JSON 7000 字符。
- 裁剪按完整动作，包含元信息开销；采用最终 json.dumps(..., ensure_ascii=False, allow_nan=False) 口径。两工具 max_output_chars=None。
- runtime/conditions/response 独立模块；BusinessService 读取真实画像，短事务结束后执行网络检索；HTTP 注入资源并在运行排空后关闭。

BusinessService 保持可选 retrieval 注入，生产 lifespan 显式持有运行时。调用方需要明确默认运行时的关闭归属。

## 3. 配置与 lookup

根 package.json 的 dev:backend 使用 uv run --env-file .env。三个 Milvus 配置从后端子进程的 backend/.env 加载，不注入前端。

本机 URI 为 http://127.0.0.1:19530，TOKEN 留空，TIMEOUT 为 10 秒连接建立超时。配置缺失只阻断检索，资料浏览及既有确认继续保持可用。

lookup 基础范围为目录器械全量，扣除本次临时不可用器械；本次 excluded_ids 生效。可用声明不收窄范围，equipment 精确筛选继续生效；长期画像限制与环境默认不参与事实核实过滤。

generation/replacement 保持既定环境默认、可用声明放行、长期与临时限制合并及冲突规则。

已读取独立 worker 的 21-config、22-lookup-conditions 原有退出 0 证据。新增真实运行移除继承的三个 Milvus 配置，通过 --env-file .env 完成初始化及已发布清单读取，没有依赖测试单独注入值。

## 4. 循环归属与资源关闭

运行时在状态或锁变更前核验所属循环。异循环 close 明确拒绝，原资源栈、检索实例及正确关闭入口保持。

close 的锁覆盖实际 AsyncExitStack.aclose，成功关闭后清空状态。SDK 上下文始终在所属循环关闭，运行时跨查询复用同一检索实例。

新增独立真实验证记录 foreign_close_rejected_before_state_change、owner_close：Milvus SDK handler 已释放，Embedding SDK 已关闭，运行时锁已释放。隔离 SQLite 与观察连接也已正确关闭。

## 5. 真实取消链已执行

使用真实 threading.Event 与 asyncio 工具 task.cancel，原样透传真正 MilvusConnection.call，不替换 SDK 返回。

实际顺序为：工具任务取消 → Event 设置 → list_aliases RPC 返回 → 主循环搜索阶段检查停止 → 工具线程与工具任务完成收尾。Embedding 编码入口没有执行。

原始结果记录：

- search_error_type=concurrent.futures._base.CancelledError。
- bridge_errors=[asyncio.exceptions.CancelledError]。
- 工具为 _CancelledExecution(None)，对应框架允许的取消结果。
- search_task_done、tool_task_done、tool_thread_done 均为 true。
- requests/prompt_tokens/total_tokens 全为 0。

异常转换符合实际 asyncio futures 状态复制规则。该验证证明校验期间取消会在阶段检查处阻止后续编码。

## 6. 当前在途取消实现与证据边界

位置：backend/app/application/business/retrieval_search.py::_encode_cancellable 与 HybridRetriever.search。

search 保持阶段检查，signal 为可选 Event。signal=None 直接等待真实 encode_query；有信号时，将真实编码创建为 asyncio 任务，以 ENCODE_CANCEL_POLL_SECONDS=0.05 秒间隔等待与检查信号。信号置位或外层任务取消时调用编码 task.cancel，并等待其取消收尾。编码完成后仍检查信号，取消不进入下一次向量查询。

runtime.search 仅传递 conditions/limit/signal，不复制检索管线。T5 融合参数、P2 身份缓存及 Milvus RPC 行为保持。

主会话已读取 41–48 的退出码，均为 0，41 Ruff 输出 All checks passed。43-runtime-lifecycle.evidence.json 记录 inflight_encode_cancelled=true。

真实验证使用 ActionRetrievalRuntime.search → HybridRetriever.search → EmbeddingClient → AsyncOpenAI。通过真实传输 trace 区分既有环境代理的 CONNECT 与实际 POST，不替换 SDK 返回。POST 请求体完整发送并进入响应头等待后，真实 Event 置位，约 50.499 毫秒后返回取消。

实际 search 异常为 concurrent.futures._base.CancelledError，编码子任务为 asyncio.exceptions.CancelledError；两任务均结束，无遗留任务。search/hybrid_search 向量 RPC 均为 0。Embedding、HTTP 与 Milvus 客户端在所属循环完成关闭。

累计 POST attempt=1、request hook=1，SDK 与传输自动重试均为 0。SDK 成功响应计数为 0；该计数不代表未发送请求。provider usage 未知，客户端取消不证明服务端计算停止或退款。50.499 毫秒是本次观测结果，0.05 秒是轮询间隔。

证据为 tmp/action-retrieval-t6/live-cancel-post-evidence.json、live-cancel-post-{command.json,stdout.txt,stderr.txt,exit.txt}。实际命令在 backend 执行 uv run --no-sync --env-file .env python ../tmp/action-retrieval-t6/live-cancel-post-run.py，退出 0。记录包含相关源码 SHA-256，source_unchanged=true；正式集合、alias、schema/index 与 1324 条计数前后相同。SDK 原有环境代理路径保持。

本次在途验收依据真实 SDK 与网络观测。BlockingEncoder 替身的单元检查未用于真实 SDK 验收结论。

## 7. 新增独立运行证据

工作目录 backend。实际命令：

`env -u FIT_AGENT_MILVUS_URI -u FIT_AGENT_MILVUS_TOKEN -u FIT_AGENT_MILVUS_TIMEOUT uv run --no-sync --frozen --env-file .env python ../tmp/action-retrieval-t6/review-fix-zero-api.py --pending-only`

退出码 0，stderr 为空。初始化为本验证的必要资源准备，未重跑既有业务模块或预置取消检查。

证据：tmp/action-retrieval-t6/review-fix-final-20261014T155136Z.{command,stdout,stderr,exit}.txt。

正式集合、alias 绑定、schema/index 哈希与 1324 条计数前后一致。观察连接关闭，测试没有写入正式集合、alias 或业务数据库。

## 8. 其它未运行项

此前已授权工具链记录为 3 次 Embedding 请求、54 total tokens；本轮新增 1 次真实在途取消请求，provider usage 未返回。没有重跑完整收费工具链或 T8 评测，也不会继续使用本次已耗尽的单请求授权。

需 source-database 或真实模型变量的业务回归保持未运行状态。check_business_context_projection 的 execute root 参数调用缺口保持原失败记录，未回退 Trace 修改。

T4 Ruff、T5 限流与恢复边界、T8 质量/性能评测保持各自原状态。T6 本轮修复项和真实 SDK 在途取消检查通过；上述未运行业务回归仍未验证。
