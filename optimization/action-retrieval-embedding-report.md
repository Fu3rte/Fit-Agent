# 动作检索 T4：Embedding 客户端交付复核

依据 [检索契约](action-retrieval-contract.md) §7 与 [实现顺序](action-retrieval-implementation-order.md) 的 T4。

## 1. 状态

T4 实现已交付，真实编码与鉴权失败验证通过。中文、英文、中英混合查询、21 条动作文本分批及成功 usage 均有实测证据。有限超时与事件循环归属边界验证通过；当前 Ruff import 排序检查退出 1，完整验收仍待该项通过。

本轮由 worker 实际执行 live 与 auth 各一次，主会话已读取当前源码、命令、输出、退出码、耗时及 JSON 结果。文档由主会话维护。

## 2. 文件与接口

- `backend/app/application/business/retrieval_config.py`：配置加载与独立错误类型。
- `backend/app/application/business/retrieval_embedding.py`：异步客户端、批次编码、返回校验与成功响应 usage 累计。
- `backend/test/check_retrieval_embedding.py`：offline/probe/live/auth 专用检查。

接口：

- `load_embedding_config(environ=None) -> EmbeddingConfig`。
- `EmbeddingClient.from_env(timeout=None)` 或 `EmbeddingClient(config, timeout=...)`。
- 在 `async with` 上下文中调用 `encode_query(text)`、`encode_batch(texts)`。
- `validate_embedding_data(items, expected_count, dimensions)` 校验返回数量、index、维度、有限值与非零范数。
- `config`、`effective_timeout`、`usage` 提供运行信息。

错误类型：`MissingRetrievalConfig`、`RetrievalConfigError`、`EmbeddingResponseError`，SDK 请求失败直接传播。

客户端 `usage.requests` 当前在成功收到响应后递增，不计失败请求；真实验证报告需单独记录失败尝试次数。

## 3. 配置与生命周期

必需变量为 `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID`。模型默认 `qwen3.7-text-embedding-flash`，维度为 1024。Base URL 由工作空间 ID 与契约 §7 域名构造。

调用命令显式加载 `backend/.env`，模块导入不读取必需凭据、不发请求。密钥不进入本报告。

请求使用 `dimensions=1024`、`encoding_format="float"`、`max_retries=0`，单批不超过 20 条，按顺序分批。

SDK 默认超时证据为 connect 5 秒、read/write 600 秒；显式 7.5 秒配置已核对。客户端在异步上下文中创建并关闭，上下文外使用与重复进入会失败。

## 4. 验证证据

实际测试由 subagent 执行，命令工作目录为 `backend`，使用 `uv run --frozen`。真实配置命令显式带 `--env-file .env`。

| 检查 | subagent 回报退出码 | 可读取证据与结果 |
| --- | --- | --- |
| Ruff 三个文件 | 1 | `boundary-validation-ruff-20261014T145747-12027.*`：`test/check_retrieval_embedding.py:1:1` 的 I001 import 排序错误 |
| offline | 0 | `tmp/action-retrieval-t4/02-offline.utf8.txt`、`offline.json`：纯校验器、配置、输入及基本生命周期检查 |
| probe | 0 | `03-probe.utf8.txt`、`probe.json`：超时与取消；JSON 记录 APITimeoutError、1.704 秒、CancelledError |
| live | 0 | `retry-live-20261014T144413.*`、`live.json`：1024 维、21 条分批、7 次请求，prompt/total 均为 4682 tokens |
| auth | 0 | `retry-auth-20261014T144459.*`、`auth.json`：AuthenticationError，SDK 对 HTTP 401 的异常映射 |

真实编码命令为 `uv run --frozen --env-file .env python -m test.check_retrieval_embedding live`。覆盖 3 条语言查询、21 条动作文本（20+1 两批）、2 条首尾对齐查询，共 7 次请求；语言查询耗时 1.383 秒，批量耗时 0.453 秒，命令约 5 秒。首尾对齐 cosine 为 0.997735 和 1.0。

鉴权命令为同一模块的 `auth` 子命令，使用明确非法凭据发送一次探针请求，命令约 3 秒。两次命令 stderr 均为空；鉴权探针 usage 未验证。原始证据按 command/stdout/stderr/exit-code/elapsed-seconds 分文件保存，中文内容以 UTF-8 JSON 核对。

纯校验器输入测试不属于真实模型返回证据。限流 429 未实际观察，未实施高频收费调用。

## 5. 源码边界验证

共享 `validate_timeout()` 在配置和客户端显式覆盖入口拒绝 bool、非法类型、非有限值及非正值；允许有限正数和 None。

客户端进入上下文时记录所属事件循环，请求与关闭前检查归属；非法跨循环操作保留所属循环的客户端引用，成功关闭后清空引用与循环。

独立测试 subagent 实际运行 offline、probe、boundary，退出码均为 0。`boundary-check.json` 核对配置及显式超时、同线程跨循环、跨线程请求与关闭、资源保留及关闭、重复进入、上下文外使用、取消收尾及重新进入均通过；boundary 检查 SDK 网络请求数为 0。

证据前缀为 `boundary-validation-offline-20261014T145703-12005`、`boundary-validation-probe-20261014T145719-12012`、`boundary-validation-boundary-20261014T145733-12019`，位于 `tmp/action-retrieval-t4/`。

## 6. 后续验收边界

当前配置的真实模型编码与鉴权失败验证通过，本轮未变更端点、配置或源码，也未执行全目录编码。

T4 功能及边界验证通过，当前 Ruff import 排序检查未通过。源码状态保持不变，按用户要求继续 T5，T5 不负责修改 T4 文件。

T5 接入使用已验证的模型、维度与客户端签名；正式索引与完整业务检索仍归后续任务。T4 完整验收仍待 Ruff 检查通过。
