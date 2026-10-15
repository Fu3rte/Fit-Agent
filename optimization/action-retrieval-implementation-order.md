# 动作检索实现顺序

## 1. 依据与状态

实现遵循 [设计方案](action-retrieval-plan.md) 与 [检索契约](action-retrieval-contract.md)。当前架构与业务范围已确认。

目标为提高别名、描述及中英文查询的覆盖与命中，增强召回和排序，保证器械约束正确及结果上下文受控。质量提升不设置数值门槛，保留量化指标与逐例证据。

本次只实现方案 A 的 BM25 + dense 两路加权排序。精确匹配优先保留为可选设计，本次不实现。

T1 已完成调用链核查与契约对齐。T2 语料、编码、查询与名称基线验证完成。T3 运行环境与探针安全验收通过；环境、能力、重启持久化、非法配置拒绝与精确清理均有真实证据。T4 已交付客户端及离线、超时和取消检查；真实编码、21 条分批、鉴权失败、有限超时及事件循环归属验证通过，Ruff import 排序检查未通过。按用户要求继续 T5，T4 完整验收状态保留。T5 正式索引、混合检索、发布保护及重启复用已有真实验证，零重试修复后的独立复验通过已执行检查。当前验收采用用户裁决“连接建立约 10 秒 + RPC 单次尝试直接传播”，接受无 RPC deadline 的阻塞权衡，并保留限流与 UNAVAILABLE 恢复重连两项未实测边界，详见 §7。本次 T5 零配额复核的 inputs 原校验函数、Ruff 和三路只读 smoke 均通过，Embedding 请求为 0；可以开展 T6 接线开发。查询热路径 P2 修复经当前源码与独立 worker 的 validation/config/search、专项 Ruff 证据复核通过；稳定集合校验仅 4 次 RPC、get/query/load_collection 均为 0，Embedding 请求为 0。实例内锁合并并发完整验证，成功身份用于稳定查询；完整校验中途非取消数据故障未直接覆盖，见 [T5 复核](action-retrieval-index-review.md)。T6–T8 的业务接线、联合回归与质量/性能验收继续按各自交付证据确认。

## 2. 八个任务与交付

### T1：调用链与契约对齐

依赖：无。当前交付已完成。

工作：核查 Catalog、工具声明与绑定、BusinessService、画像持久化、计划保存、确认入口、前端复用组件和 Trace 接线。

交付文件：

- `optimization/action-retrieval-contract.md`。
- `optimization/action-retrieval-implementation-order.md`。
- `optimization/action-retrieval-plan.md` 的链接与范围同步。

源码核查发现的重点：

- 当前搜索是中文名称子串匹配，返回全部完整动作，无搜索专用体积上限。
- 画像列表字段、校验和确认持久化已经存在；搜索尚未应用画像限制。
- 计划准备与保存已经核验目录 ID 和已保存画像限制，继续复用。
- ProfileFields 尚未展示不可用器械；页面与待确认快照共享该组件。
- 环境器械默认规则已存在于当前 SYSTEM_PROMPT，需进入候选硬过滤。
- 工具 Trace 已覆盖准备、执行和后处理，检索保留已有边界。

验收：文档引用真实路径和符号；现状与目标明确区分；只修改检索文档。数据统计确认目录数量与唯一 ID。文档检查不代表业务验证。

### T2：语料与评测数据

依赖：T1。

工作：补充中文俗称、英文名称和字段中文映射，记录原始 ID、来源、审阅状态与版本；冻结文本组合及规范化规则；统计简要字段、完整步骤和序列化长度。

建立开发集与留出集，按意图和别名家族分组。覆盖标准名称、别名、中英混合、口语描述、居家有哑铃练背、临时限制、多动作别名与无答案。

文件归属：`backend/dataset/` 的补充数据及 Schema、检索评测语料与标注文件。原始动作身份与事实不擅自改写。文件具体命名在任务启动时记录。

交付：人工审阅记录、金标及拆分、文本编码版本、字符预算依据、当前名称检索的专项基线证据。

验收：引用 ID 全部存在；补充数据经过成熟 Schema 校验；歧义关系保留；数据与标注未经伪造；留出集不参与别名维护和调参。

### T3：Milvus Standalone 运行环境

依赖：T1。可与 T2、T4、T7 的开发并行。

工作：准备 Docker Compose、固定 Milvus/PyMilvus 兼容版本、持久化存储和本机端口访问；验证 BM25 Function、analyzer、COSINE 和 hybrid_search 所需能力。

文件归属：Milvus 部署配置、`backend/pyproject.toml`、`backend/uv.lock` 及必要 `.gitignore` 规则。依赖清单与 lock 文件由本任务统一修改，T4 不并行修改这些文件。

交付：可运行服务、启动与停止说明、连接配置、版本记录及数据持久化证据。Fit-Agent 整体容器化保持现有安排。

验收：真实服务健康、重启数据保留；中文与中英文 analyzer 样例；必要检索能力可用；持久化数据不进入提交；服务不暴露于非受控网络。

### T4：Embedding API 客户端

依赖：T1。复用已经安装的 OpenAI SDK，可与 T2、T3、T7 的开发并行。

工作：异步 API 客户端、必需环境配置、目录分批编码与查询编码、返回数量和 index 对齐、1024 维、有限值及非零范数校验；明确鉴权、限流、超时和取消行为。

文件归属：新增 Embedding 客户端、检索配置及专用测试文件。延续当前分层；模块路径在任务启动时固定。通用 HTTP、Agent、AI stream 和 Trace 代码不在此任务修改范围。

交付：客户端接口、后端配置说明、真实调用证据及失败行为证据。

验收：真实空间和模型可用；密钥不进入日志、前端、索引与证据；配置缺失在检索相关入口明确失败；浏览与有效快照确认保持独立。异步客户端生命周期与调用循环明确，禁止跨线程复用不兼容的异步资源。

真实验证依赖用户已配置业务空间 ID 与 API Key，缺失时如实记录阻塞。

### T5：索引构建与混合检索

依赖：T2、T3、T4。

工作：统一文本与 Milvus schema；目录 ID 主键、筛选字段、dense/sparse 索引；构建和查询版本检查、构建校验与发布；两路相同硬过滤、COSINE、内置归一化与 WeightedRanker。

实际源码归属：`backend/app/application/business/retrieval_index.py`、`retrieval_search.py` 和 `backend/test/check_retrieval_index.py`。`Catalog.load/get/all/search` 保持原状。BusinessService 的业务及保存改动统一由 T6 完成。

交付：可构建并重启复用的索引、混合检索接口、参数记录、明确失败行为；将物理 schema、索引参数及发布方式同步至契约。

验收：构建数量与唯一 ID 正确；未完成构建不进入查询；不匹配版本报错；真实两路检索、过滤、发布与重启复用通过。在线只编码查询，不重新编码整个目录。连接建立保留约 10 秒超时；RPC 单次尝试直接传播，传入 `retry_times=0, retry_on_rate_limit=False`，每次 RPC 的 `timeout=None`。用户接受服务端暂停且连接存活时 RPC、取消收尾及串行关闭可能持续等待。

独立验证：目录 1324 条、67 次编码请求、305035 prompt/total tokens；编码约 23.243 秒、构建约 28.404 秒。正式 alias、完整元信息、失败发布保护与重启复用已有真实证据。零重试修复后 T5 三文件 Ruff 与 inputs/metadata/check/boundaries 均退出 0；可重试 INTERNAL 故障实测为单次 HEADERS，约 0.017 秒直接失败。限流路径仅代码检视；UNAVAILABLE 恢复重连残留未完成隔离实测，必须随交付保留。证据索引与 T6 接口见 §7。

### T6：业务与工具集成

依赖：T5；返回预算依据 T2。

当前配置加载、lookup 条件、循环归属关闭及阶段间真实取消已复核。独立 Event/task.cancel 检查退出 0：RPC 返回后阻止编码，搜索/工具/线程收尾，真实 SDK 正确关闭，正式集合与 alias 保持；新增 Embedding 请求及 tokens 为 0。当前编码取消已位于 HybridRetriever.search，可选 signal 以 0.05 秒轮询触发编码任务取消，runtime 仅委托。41–48 专项退出码均为 0；新增用户授权的真实 SDK 在途取消检查退出 0，累计 1 次 POST 请求，实际发送并等待响应后约 50.499 毫秒完成取消，子任务、SDK 及连接全部收尾，正式数据不变。provider usage 与计费未知，单请求授权已用尽。本轮修复项验收通过，未运行业务回归仍保持未验证。先前已授权工具链记录为 3 请求、54 total tokens，详见 [T6 复核](action-retrieval-business-review.md)。

工作：明确动作核实与生成/替换的用途传递；读取已保存画像与任务临时条件；实现环境默认、显式器械放行、不可用器械及禁用动作过滤和输出复核。

实现简要搜索、按 ID 批量详情、结构化体积裁剪和后续查询信息；固定输入输出 Schema、详情工具名、批量和字符预算并同步契约。

同步提示词中的查询表达、工具返回解释和搜索后读取详情流程，覆盖原数组结构的调用方。复用已有画像确认持久化与计划保存校验，按契约补齐必要约束。

主要文件归属：

- `backend/app/application/business/service.py`。
- `backend/app/agent/tools/exercises.py`、`business.py`、`prompts.py`。
- 必要的业务模型与工具专用测试。
- 必要的 HTTP 生命周期注入，须先协调 Trace 的共享文件归属。

交付：端到端可调用的搜索与详情工具、画像约束读取、计划生成/替换集成、调用方及回归断言同步。

验收：既有授权、版本、确认幂等及业务保存保持正确；未确认画像零长期提交；工具 JSON 完整且预算合规；取消收尾、凭据检查与 tool Trace 生命周期保持原语义。

### T7：不可用器械画像展示

依赖：T1 的既有画像 wire；完整联调等待 T6。

T7 展示与本轮验证已复核通过。共享 ProfileFields 展示 unavailable_equipment，取值按真实组件节点读取：以 Field 定位字段、FieldTitle 整段标题匹配、Badge 核对列表条目；null、[]、非空列表保持各自语义。联合验证走真实 HTTP 与按钮确认入口，覆盖未确认零提交、保存/解除后查询刷新、历史快照独立值、跨会话及重开隔离 SQLite。证据位于 tmp/action-retrieval-t7/，10-verify-profile-node-read、11-t7-joint-node-read、12-t7-joint-ready-order 均退出 0；主会话已读取源码与保存输出，本轮未重跑测试、未调用模型。浏览器视觉及鼠标点击自动化未执行，详见 [T7 复核](action-retrieval-profile-review.md)。

工作：在 `frontend/src/features/profile/ProfileFields.tsx` 复用现有列表展示结构，补齐 `unavailable_equipment`；核查 ProfilePage 和 ChatTranscript 的共享展示。

文件归属：ProfileFields 及画像展示专项检查。只在确有需要时修改页面或 ChatTranscript；后者与 Trace 前端展示存在共享边界，修改前协调。

交付：已保存画像、对话画像和待确认快照一致展示不可用器械；沿用已有确认按钮、查询刷新和状态恢复。

验收：null、空列表、实际限制展示正确；快照修改后重新确认；保存和解除后刷新、跨会话读取正确；不添加装饰性或引导性文案。

### T8：联合回归、参数冻结与效果证据

依赖：T6、T7。评测脚本可按 T1 契约提前准备。

真实查询向量、18 单元开发集网格、参数冻结和固定留出检索已有历史证据。生产配置为 alpha=0.5、两路深度100、threshold=0.0；实际覆盖125/128条查询，三条用途语义暂缓。开发集及留出无答案误返回率均为1.0。生产审计修复已由独立agent完成10条指定检查，退出码均为0，覆盖环境判定、错误与最终JSON预算、互斥条件零编码、相关调度/取消/Trace及SDK生命周期；新增计费请求为0。主会话已复核核心接线与保存证据。当前源码已完成用户授权的固定参数重复留出复验：20261015T081832Z-9a806e7e，66条四组、唯一一次正式运行、身份不漂移、额外模型请求0，结果与历史相同，原冻结及账本保留。本次结果不用于选参。用户最新确定本轮以基本功能收尾为目标，执行tmp/action-retrieval-functional-final-prompt.md：修复已知夹具缺陷，独立真实验证搜索/详情、生成、替换及确认读取，保持量化入口可用。本轮不设用量或费用预算，不跑18样本、基线或指标矩阵。功能通过后可标记运动检索模块功能完成；量化评测统一延期至全部优化方案实现后执行。功能收尾尚未标记通过。默认Trace库未知历史记录保持，不清理；共享停服不在授权内。详见 [T8复核](action-retrieval-evaluation-review.md)。

以下T8量化工作与交付统一延期，不作为本轮功能完成门禁。

工作：同一提交运行当前名称检索、稀疏、dense、混合对照；用开发集选择 α、召回深度、analyzer 和无答案门槛，冻结后评估留出集。

交付：分类质量指标、逐例新增命中与退化证据、过滤及预算检查、跨会话和重启证据、API 与索引失败行为、端到端 tokens/耗时/调用成本及固定配置。

业务场景包含动作核实、计划生成和反馈替换，每例按总体方案执行三次。统计整轮搜索与详情的全部上下文，检查详情请求是否抵消搜索缩减收益。

验收：别名与描述查询的覆盖及命中改善有实际证据；器械与禁用动作硬约束违反数为零；结果数量及体积全部合规；指名查询与无答案退化明确记录。精确匹配优先依据证据作为独立后续决策，本任务不自行追加。

## 3. 串行、并行与文件归属

执行关系：

- T1 完成后，T2、T3、T4、T7 的前端开发可并行。
- T5 等待 T2、T3、T4 完成。
- T6 等待 T5 完成。
- T7 完整业务联调等待 T6。
- T8 等待 T6、T7 完成。

并行任务只修改各自归属文件。依赖清单及 lock 由 T3 统一维护；注册、提示词、BusinessService 及公共工具 Schema 由 T6 统一维护。各任务记录专用测试文件归属，避免覆盖同一文件。

三份检索文档由协调任务统一同步；并行任务提供参数和证据，不同时修改公共契约。

## 4. Trace 共存要求

依据 [Trace 实施清单](trace-observability-implementation-order.md) 与当前工作区，Trace 正在接线根运行、工具、模型、压缩及确认相关边界。

| 共享位置 | 检索可能触达的工作 | 协调要求 |
| --- | --- | --- |
| `interfaces/http.py` | 检索依赖生命周期、服务与工具绑定 | T6 基于 Trace 最新状态接入，只修改必要注入点，保留根结算及通知顺序 |
| `agent/tool.py` | 检索异步执行、结果裁剪及取消收尾 | 复用现有能力；结构化裁剪在检索业务内完成，通用 Trace 接线保持原样 |
| 确认服务与 BusinessService | 画像、计划授权和保存校验 | 保持按钮确认、幂等、版本和事务边界；确需改同文件时协调单一负责人 |
| `ChatTranscript.tsx` | 画像快照展示与 Trace 运行用时 | 优先只改共享 ProfileFields，确需改消息组件时协调 |
| 回归支持与全局工具断言 | 新详情工具及形状变化 | 不覆盖 Trace 支持改动；读取最新测试状态并同步受影响断言 |

检索不修改 Trace 数据 Schema，不把 Embedding 当作现有聊天/摘要 model Span，不将检索文本和结果写入 Trace。专项 API 和质量证据独立收集，现有 tool Span 仍反映检索工具总耗时。

Trace 代码或测试的已知问题只记录，不作为检索任务的附带修复。源码正在并行变化时，实施与回归前重新读取相关文件，禁止回退其他任务的修改。

## 5. 验证执行归属与真实阻塞

每个实现任务必须交付测试及实际证据；业务和实现测试的实际运行派发 subagent。依据命令、退出码和输出判定，不将未运行检查标为通过。

使用真实 Milvus、Embedding、SQLite 与业务接口；遵守无 Mock、无伪造、无错误降级要求。证据存于 `tmp/` 或 `backend/data/` 等已忽略目录，隔离业务与观测生产数据。

需要外部输入的事项：T2 别名与相关性人工审阅、T4 真实业务空间和密钥、T3 Docker 环境及可用资源。缺失时阻塞对应真实验证，不擅自宣称完成。

质量验收无新增数值拍板项。技术参数通过既定统计和真实验证固定，更新契约即可；出现新的业务歧义或与 PRODUCT.md 冲突时停止该部分并请用户确认。

## 6. T1 文档验证记录

- 目录统计命令：PowerShell `ConvertFrom-Json` 读取 `backend/dataset/actions.agent.json`，退出码 0；1324 条记录、1324 个唯一 ID，存在背部哑铃候选。
- 文档检查命令：`& ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 'tmp/check-action-retrieval-docs.ps1'))) | Tee-Object -FilePath 'tmp/action-retrieval-doc-check.txt'`，退出码 0。
- 检查结果：三份检索文档、25 个引用路径、关键源码符号与 T1–T8 任务存在性通过；未设置质量提升数值门槛，精确匹配优先保持本次不实现。
- 检查脚本和输出位于已忽略的 `tmp/`。本任务只修改检索文档，未修改业务或 Trace 源码。
- 业务测试、Milvus 部署、真实 Embedding API 和质量/性能评测未执行。后续实现仍按 subagent 测试执行归属完成验证。

## 7. T5 当前验收、接口与证据

### 7.1 T6 消费接口及资源边界

- `RetrievalCorpus.load()` 读取原始 Catalog、published 语料、字段映射及批准哈希，校验当前输入版本。
- 在同一事件循环的 `async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db` 中创建和使用检索依赖。
- `await HybridRetriever.open(db, corpus, embedding)` 校验当前已发布集合。
- `await retriever.search(query, conditions=SearchConditions(...), alpha=0.5, bm25_depth=50, dense_depth=50, limit=10)` 返回字符串 ID、融合分数、名称和四个真实结构化字段。默认值属于接口运行参数，质量选择归 T8。
- 四字段精确筛选、有效器械集合与排除 ID 由 T5 接收已求值条件；用途、画像合并、工具及 HTTP 生命周期归 T6。详情继续按 ID 读取原始 Catalog。
- RPC 单次尝试，错误直接传播。连接建立受 `FIT_AGENT_MILVUS_TIMEOUT=10` 约束；RPC `timeout=None`。取消等待正在执行的同步调用，关闭与调用串行。用户接受暂停服务端时的持续阻塞。

### 7.2 独立验证证据

证据均位于已忽略 `tmp/action-retrieval-t5/`；主会话读取输出并同步文档，本轮未重新执行业务测试。

- `18-build-first-charge.command.json`、`build-result.json`：首次完整目录编码及发布，1324 条，67 请求，305035 prompt/total tokens；目录未重复编码。
- `19-failures.*`、`failure-results.json`：版本不符、空文本失败构建与不完整候选拒绝，已发布 alias 和查询结果保持。
- `21-switch.*`、`alias-switch-results.json`：复用真实向量，完整校验后切换原生 alias；目录编码 0，旧正式集合保留。
- `35-project-stop.*`、`37-project-start.*`、`39-persistence.*`：本项目保留卷重启，manifest、集合和查询保持，目录编码 0。
- `51-verify-ruff.result.txt`、`51-verify-ruff.stdout.txt`：从 `backend` 执行 `uv run --frozen --env-file .env ruff check app/application/business/retrieval_index.py app/application/business/retrieval_search.py test/check_retrieval_index.py`，退出 0。T4 I001 保持原状态，本次未检查全 backend。
- `52-verify-modes.results.txt`：从 `backend` 执行 `uv run --frozen --env-file .env python -m test.check_retrieval_index <mode>`，mode 分别为 inputs、metadata、check、boundaries，四项均退出 0；各自原始输出为 `52-verify-<mode>.stdout.txt` 与 `.stderr.txt`。
- `53-verify-preservation.json`：正式 alias、两份集合、1324 条记录、manifest 和编码版本保持。
- `54-verify-retry-policy.json`：SDK 3.0.2 及生产调用链代码检视，`retry_times=0`、`retry_on_rate_limit=False`、无数值 RPC timeout。
- `55-verify-zero-retry-rpc.json`：真实服务下线后会话调用约 2.055 秒以 CONNECT_FAILED 失败，finally 恢复服务；RPC 未上线，传输层后台重连有观测记录。
- `56-verify-connect-bounds.json`：blackhole 连接约 10.02 秒、accept-then-RST 约 0.004 秒失败，各观测 1 次 accept。
- `59-verify-zero-retry-internal-stream.json`：真实透传代理注入 RST_STREAM(INTERNAL_ERROR)，可重试 INTERNAL 单次 HEADERS，约 0.017 秒直接失败，解除故障后查询成功。
- `60-verify-rate-limit-inspection.json`：限流仅代码检视；UNAVAILABLE 连接管理器恢复重连残留仍为未实测边界，不声明已验证所有传输恢复行为。
- `61-verify-final-state.txt`：三个本项目容器 healthy，暂存区为空，HEAD `ebb1635`，`.env` 内容哈希保持。

测试进程按用户授权注入固定 `FIT_AGENT_MILVUS_URI=http://127.0.0.1:19530`、TOKEN 空、TIMEOUT 10，保持 `.env` 不变。`41–45` 的 DEADLINE_EXCEEDED 超时证据作废，归档保留，不计入当前策略验收。`verify-backup-20261017/` 保存复验前证据。

### 7.3 保留对象与发布配置

- 正式入口 alias：`fit_agent_actions_current`。
- 当前集合：`fit_agent_actions_build_df2e2f6042934c79b626c29fe8562bb3`。
- 保留旧正式集合：`fit_agent_actions_build_84ae20ff94d74d089f6f2a41070436f8`。
- 已核验归属并精确清理：`fit_agent_t5_test_metadata`、`fit_agent_t5_test_incompatible`、失败构建 `fit_agent_actions_build_834f460c339e4f06b4f9697a729e475d`。本任务不清理旧正式版本、其他任务对象或卷。
- 发布者 `fit-agent-t5-single-publisher` 串行构建及发布；Schema、容量、analyzer、索引参数及不可变清单格式见检索契约 §3.2、§6。模型 `qwen3.7-text-embedding-flash`，1024 维，编码规则 `retrieval-text-2026-10-14.2`。
- 当前改动保持未提交状态，并行任务改动保留；业务集成和质量验收继续归 T6–T8。

