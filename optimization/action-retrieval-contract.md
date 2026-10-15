# 动作检索数据与业务契约

## 1. 范围与依据

本契约约束动作检索增强的实现与验收，实施顺序见 [任务清单](action-retrieval-implementation-order.md)，总体方案见 [设计方案](action-retrieval-plan.md)。

已确认采用统一文本 BM25 与 dense 两路检索，Milvus Standalone 通过 Docker Compose 部署，使用内置归一化与 WeightedRanker。精确匹配优先保留为可选设计，本次不实现。

业务依据为 `PRODUCT.md` §2.1、§2.2、§3.2、§3.4、§4；Trace 边界遵循 [Trace 契约](trace-observability-contract.md)。下文“现状”来自当前源码的只读核查，“目标”是待实现契约。符号引用用于定位，后续修改前重新读取当前源码。

优化目标：别名与描述查询命中改善、器械约束正确、返回上下文受控。覆盖标准名称、别名、中英文表达、口语描述及带条件需求。质量评测保留指标与逐例证据，不设置提升百分比或分数门槛。

## 2. 当前调用链与待补齐项

| 边界 | 当前源码依据 | 现状与待补齐项 |
| --- | --- | --- |
| 目录载入 | `backend/app/application/business/catalog.py`：`Catalog.load/get/all/search` | JSON Schema 校验后加载原始目录；按 ID 读取、完整遍历与名称子串匹配已经存在；需新增索引检索能力 |
| 数据模型 | `backend/app/domain/business/models.py`：`CatalogExercise/CatalogSteps` | 九个事实字段及中英文步骤；现有模型没有别名或英文名称字段 |
| 业务搜索 | `backend/app/application/business/service.py`：`search_exercises` | 同步调用 `Catalog.search`，不读取画像，不执行画像限制过滤 |
| 工具参数 | `backend/app/agent/tools/exercises.py`：`SearchExercisesArguments` | 必填非空 `name`，四个可选精确筛选字段；需支持语义输入和任务约束 |
| 工具返回 | 同文件：`bind_exercise_tools/_tool` | 返回全部匹配动作的九字段数组，`max_output_chars=None`；需简要搜索与独立详情读取 |
| 工具注册 | `backend/app/agent/tools/business.py`：`business_tool_declarations/bind_business_tools` | 声明与执行绑定分别汇总；新增详情工具及参数变更需同步两处 |
| 业务生命周期 | `backend/app/interfaces/http.py`：`lifespan/execution_registry` | 启动载入 Catalog，注入 BusinessService，并同步只读目录到 SQLite；工具执行通过现有运行上下文绑定 |
| 器械默认 | `backend/app/agent/prompts.py`：`SYSTEM_PROMPT` | 已写入居家、户外默认徒手及显式器械放行规则；需落实为检索候选选择的确定性条件 |
| 画像模型与校验 | `models.py`：`ProfileContent`；`service.py`：`_validate_profile` | `unavailable_equipment/forbidden_exercise_ids` 已存在，拒绝重复、空白及目录外引用 |
| 画像保存 | `service.py`：`prepare_profile_update/_commit_save`；`business_repository.py`：`get_profile/save_profile` | 完整快照、版本与 JSON 持久化链路已有；复用既有链路并补跨会话验收 |
| 计划约束 | `service.py`：`_validate_plan/_plan_profile_conflict/_commit_plan_save` | 已核验有目录 ID 的动作、已保存画像限制及准备依据；当前环境默认和任务临时限制不在搜索层执行 |
| 确认入口 | `backend/app/application/business/confirmation.py`：`confirm/_accept/_execute` | 后端独立受理，重复请求复用操作结果，调用真实业务保存；保持授权、幂等和事务边界 |
| 前端画像 | `frontend/src/features/profile/ProfileFields.tsx`：`LIST_FIELDS` | 当前只展示禁用动作列表，需补不可用器械展示 |
| 页面与快照复用 | `ProfilePage.tsx`；`frontend/src/features/chat/components/ChatTranscript.tsx`：`entry.profile` | 已保存画像和待确认画像共用 ProfileFields，优先在共享组件补齐 |
| 前端解析 | `frontend/src/lib/contract.ts`：`ProfileContentWire`；`frontend/src/lib/business.ts`：`parseProfileContent` | 不可用器械字段及 null/空数组校验已经存在，保持 wire 结构 |
| 依赖与启动 | `backend/pyproject.toml`；`README.md` 后端启动命令 | 已安装 OpenAI SDK；依赖清单尚无 PyMilvus；当前启动命令未显式加载检索 `.env` |

数据核查：`backend/dataset/actions.agent.json` 有 1324 条记录和 1324 个唯一 ID；字段与 CatalogExercise 对应。数据内存在 `body_part=back` 且 `equipment=dumbbell` 的候选。数据统计不等同于检索质量验证。

当前线上规则入口是 `agent/prompts.py` 及已注册工具。`backend/skills/` 中出现的历史工具名、画像状态和候选结构须核对实际接线，不能直接视为当前业务契约。

## 3. 目录、别名与统一文本

### 3.1 动作身份与事实

- 原始 ID 保持字符串形式，保留前导零；Milvus 主键复用该 ID。
- 原始目录继续提供名称、器械、肌群、重量口径及完整步骤。业务保存和按 ID 详情读取复用 Catalog 事实。
- 别名与英文名称作为补充检索数据，关联原始 ID，记录来源、审阅状态与版本。只有已审阅内容进入发布索引。
- 别名可关联多个动作；结果保留不同 ID 的具体变式，模型在歧义影响选择时澄清。
- 中文器械及肌群映射输出目录中的合法标识，避免把显示文案作为持久化标识。
- 本次不改变目录事实字段与既有 SQLite 业务身份。

### 3.2 编码与 Milvus 记录

每条记录包含原始 ID、统一文本、dense 向量、BM25 sparse 向量及 `equipment/body_part/target/muscle_group` 筛选字段。构建版本关联目录哈希与补充数据版本。

当前编码规则版本为 `retrieval-text-2026-10-14.2`，固定字段顺序为 `name, aliases, english_name, equipment, body_part, target, muscle_group, steps_zh, steps_en`。段间使用 `\n`，空字段跳过；英文名称与别名均只纳入 `review_status=approved` 的内容。文档与查询共用 NFKC、空白折叠与 casefold 规范化。字段映射输出 canonical 与中文显示；`query_canonical()` 返回完整命中集合，消费方保留同义及泛称的多值语义。

物理字段为 VARCHAR 主键 `id`（64 字节）、`text`（4096 字节）、`equipment/body_part/target/muscle_group`（各 128 字节）、1024 维 FLOAT_VECTOR `dense` 和 SPARSE_FLOAT_VECTOR `sparse`。`text` 启用 `analyzer_params={"type":"chinese"}`，BM25 Function `text_bm25` 从 `text` 生成 `sparse`。dense 使用 `AUTOINDEX/COSINE`；sparse 使用 `SPARSE_INVERTED_INDEX/BM25`，参数为 `inverted_index_algo=DAAT_MAXSCORE`、`bm25_k1=1.2`、`bm25_b=0.75`。字符串容量按 UTF-8 字节校验，当前统一文本最大 1604 字节。工具响应预算按字符计量，T6 用最终响应 Schema 复核。

器械与动作限制进入结构化过滤。原始查询文本不能直接拼接为 Milvus 过滤表达式；标识必须经过目录验证和安全参数编码。

## 4. 查询语义与约束

### 4.1 查询与用途

现有 `search_exercises.name` 由中文名称包含匹配扩展为语义查询输入；现有四个筛选字段的精确筛选含义保留。模型可提交别名、英文或描述性文本。

业务层必须区分动作核实与生成/替换建议：

- 动作核实允许返回画像受限动作，回答说明真实器械要求和限制。lookup 的基础范围为目录器械全量，扣除本次临时不可用项，应用本次 excluded_ids 和显式精确筛选；可用声明不收窄范围，不叠加长期画像禁用或环境默认。
- 生成及替换候选必须遵守有效器械条件与禁用动作。
- T6 search_exercises 的 purpose 必填，枚举为 lookup/generation/replacement；name 为语义输入，保留四个精确字段，新增 task_environment、available_equipment、unavailable_equipment、excluded_ids 任务参数。BusinessService.search_exercises 为异步方法，工具通过 MainLoopCall 投递主循环；可信会话、运行和消息身份继续由后端绑定。lookup 条件、校验期间取消及循环归属关闭已复核；HybridRetriever.search 已接入编码任务取消及 0.05 秒信号轮询，真实 SDK 在途取消已由独立 worker 验证通过，累计 1 次 POST 请求，provider usage 与计费未知，见 [T6 复核](action-retrieval-business-review.md)。

### 4.2 条件求值

- 训练环境为居家或户外时，默认可用器械为 `body weight`。
- 其余环境或环境未知时，默认目录器械可用。
- 用户明确声明可用器械时，按声明放行对应器械；可用声明不自动等价于“只使用该器械”的筛选条件。
- 用户明确提出“只用哑铃”时，将该条件作为本次候选的器械筛选。
- 画像 `unavailable_equipment` 和 `forbidden_exercise_ids` 从已保存结构化状态读取；临时缺少器械或排除动作只用于对应任务。
- 可用声明与已保存不可用限制发生冲突时，遵循现有澄清、解除限制及画像确认流程；禁止静默删除长期限制。
- `null` 保持未知语义，空数组表示明确没有对应清单项，不能借未知值绕过 PRODUCT.md 的环境默认。
- 两路召回采用同一有效过滤条件，结果输出再次核验。相关性分数不能覆盖硬约束。

“家里有哑铃，想练背”验收：识别居家环境和明确可用哑铃，目标为背部；允许 `body weight` 与 `dumbbell` 中符合限制的候选，相关性排序围绕背部训练。明确筛选背部时使用目录 `body_part=back`。已有禁用项继续生效，未知或歧义条件按任务需要澄清。

实际设施依赖补标不在本次范围。`body weight` 的设施可执行性受现有标签覆盖限制。

## 5. 搜索与详情返回

### 5.1 搜索

- 默认最终候选不超过 10 条，按归一化后的线性融合分数排序。
- 每个简要候选包含 ID、名称、器械、目标肌群及简短字段匹配依据；依据只使用可核实字段。
- 字段匹配说明不能虚构某一路精确词项命中。无法取得词项证据时，只描述实际目录字段和用户条件。
- 返回合法 JSON，包含是否仍有候选及后续查询所需信息。其含义限于本次召回及裁剪状态；未计算全目录匹配总量时不能宣称全量总数。
- 按完整候选裁剪，字符预算包含 JSON 序列化与元信息开销。不能在 JSON 中间截断。
- 无可用候选或无答案门槛未通过时允许合法空结果。
- 本次不增加精确名称查找、优先分组、额外词项打分或对应开关。

### 5.2 详情

- 独立只读工具按原始动作 ID 批量读取九个目录事实字段，包含完整中英文步骤和重量口径。
- 详情取自原始 Catalog，允许核实受限动作。详情读取不构成推荐可用性或保存授权。
- 输入校验未知 ID、空 ID、重复 ID 和批量上限；未知 ID 明确报错，不能伪造详情。
- 输出同样保证合法 JSON、完整动作和体积上限；裁剪时明确哪些 ID 已返回及哪些仍需后续请求。
- 单条完整详情无法容纳于预算时明确报错，禁止静默缺失步骤。

T6 当前工具输出固定为 `{items, has_more, remaining_ids}`。搜索每项为 `{id, name, equipment, target, matches}`，最多 10 条，完整 JSON 4000 字符；读取 11 个召回候选，remaining_ids 仅为本次已召回未返回 ID。详情工具名为 get_exercise_details，ids 为 1..5 个，按输入顺序返回原目录九字段，完整 JSON 7000 字符。预算包含元信息及剩余 ID，序列化口径为 json.dumps(..., ensure_ascii=False, allow_nan=False)。接口已接线，配置、lookup、循环归属关闭及阶段间真实取消已经复核；本轮修复项及真实 SDK 在途取消验证通过，未运行的业务回归继续保持未验证，详见 [T6 复核](action-retrieval-business-review.md)。

`agent/tool.py::_truncate` 当前是通用字符串切片。检索工具在业务序列化前完成结构化裁剪，并沿用 `max_output_chars=None`，避免二次切片破坏 JSON；工具自身预算校验必须覆盖完整序列化结果。

## 6. 融合、索引与发布

- 请求路径固定 `[BM25, dense]`，使用 `WeightedRanker(α, 1−α)` 并启用内置分数归一化（`norm_score=True`）。T3 实测 BM25 映射为 `arctan(x)/(π/2)`，COSINE 映射为 `(1+cos)/2`；正交探针的 COSINE 为 0，归一化得分为 0.5。真实动作的非相关候选得分由实际向量决定，T8 用真实查询校准门槛。
- 某动作未进入某一路召回时，该路贡献为零；每路召回深度独立于最终候选上限。
- α、召回深度、分词、无答案门槛通过开发集选择；冻结后评估留出集。融合分数表示排序依据。
- 目录向量预计算；在线只编码查询，BM25 路径提交文本，由 Milvus 处理 sparse 表示。
- T5 查询校验按 HybridRetriever 实例记住已验证的物理集合名和完整清单指纹。构建发布、实例启动及集合/清单身份变化时执行全量校验；稳定查询核验 alias、实际源哈希、服务及配置兼容和清单完整性，仅 4 次 RPC，不回读全目录向量。实例内锁合并并发首次验证，成功后才记录身份，实例重建重新验证；详见 [T5 复核](action-retrieval-index-review.md)。
- 构建时记录目录哈希、别名版本、API 服务地址、模型、维度、编码规则、analyzer、索引参数、服务及 SDK 版本。
- 构建和查询均校验有效版本。目录、模型、编码或索引配置变化后重建；禁止混用向量空间。
- 新索引完成数量、唯一 ID、维度、字段和版本校验后发布。未完成构建不得进入查询路径。
- 重启复用兼容索引，失败构建保持已发布索引的完整性；索引缺失或不匹配时明确失败。
- 原生 alias `fit_agent_actions_current` 为正式入口，构建候选名为 `fit_agent_actions_build_<32位UUID hex>`。发布者为 `fit-agent-t5-single-publisher`，由运营方串行执行；完整校验、flush/load 和真实三路查询通过后才原生切换 alias，保留既有正式集合。
- 不可变清单格式为 `action-index-schema-metadata-1`：collection `description` 保存格式、字段名、UTF-8 字节数和 SHA-256，完整 JSON 存于 Schema 的 `text.description`，经 `describe_collection` 回读验证。collection description 上限 1024 字节，清单应用容量上限 4096 字节。清单记录实际源版本与哈希、编码及索引配置；缺失、损坏、数量或版本不符直接失败。本次不增加自动模型替换或检索降级。

## 7. Embedding 与运行配置

复用仓库 OpenAI Python SDK 的异步客户端调用 `embeddings.create`。T4 配置模块为 `app/application/business/retrieval_config.py`，客户端为 `retrieval_embedding.py`；提供 `load_embedding_config()`、`EmbeddingClient.from_env()`、上下文内 `encode_query/encode_batch` 与纯返回校验器 `validate_embedding_data()`。配置加载仅发生于检索客户端创建入口。T4 真实编码、鉴权失败及源码边界验证通过，Ruff import 排序检查未通过，完整验收待通过；详见 [Embedding 交付报告](action-retrieval-embedding-report.md)。

- `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID` 为必需后端配置。
- Base URL 为 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- `FIT_AGENT_EMBEDDING_MODEL=qwen3.7-text-embedding-flash`，`FIT_AGENT_EMBEDDING_DIMENSIONS=1024`。
- 请求采用 `dimensions=1024`、`encoding_format="float"`；批次不超过设计约定的 20 条。
- 校验返回数量、index 对齐、维度、有限值及非零范数。
- 客户端使用异步上下文明确释放资源，并保证创建、请求与关闭属于同一事件循环。显式 timeout 必须是有限正数；当前 SDK 默认 connect 5 秒、read/write 600 秒。T4 的显式超时有限性及事件循环归属边界检查已由独立 subagent 运行通过。
- 密钥保存在已忽略配置中，由启动命令显式加载；根 package.json 的 dev:backend 使用 uv run --env-file .env，仅由后端子进程加载 backend/.env，三个本机 Milvus 配置已通过该方式真实读取。禁止密钥进入前端、日志、索引、评测证据或 Trace。
- T3 固定 Milvus `v3.0.2`、PyMilvus `3.0.2`、etcd `v3.5.25`、MinIO `RELEASE.2024-12-18T13-15-44Z`，消息队列使用内置 Woodpecker。
- `FIT_AGENT_MILVUS_URI=http://127.0.0.1:19530`、`FIT_AGENT_MILVUS_TOKEN` 留空、`FIT_AGENT_MILVUS_TIMEOUT=10`（秒）。T5 的 URI 与 TIMEOUT 为必需配置，缺失时停止检索入口。TIMEOUT 约束连接建立，实测 blackhole 连接约 10.02 秒失败。当前服务无鉴权，仅发布本机回环端口；内部网络及本机访问主体仍需受控。
- 用户裁决采用“连接建立约 10 秒 + RPC 单次尝试直接传播”。T5 `MilvusConnection.call()` 下传 `retry_times=0, retry_on_rate_limit=False`，每次 RPC 的 `timeout=None`。该策略关闭 PyMilvus 装饰器的 RPC 与限流自动重试；连接建立继续使用 `MilvusClient(..., timeout=self.timeout)`。
- 用户接受 `timeout=None` 的 RPC 在服务端暂停且连接存活时持续阻塞。同步 Milvus I/O 在线程内执行，取消需等待当前调用返回，关闭与调用串行；阻塞 RPC 的取消及关闭同样可能持续等待。T6 必须保留该资源收尾语义。
- 零重试实测覆盖真实透传代理注入的可重试 `INTERNAL`：上行 HEADERS 为 1 次、约 0.017 秒失败。限流路径仅代码检视；`UNAVAILABLE` 触发的连接管理器恢复重连残留尚未完成独立故障隔离实测。gRPC 传输层后台重连属于保留的观测边界。
- Compose 项目为 `fit-agent-milvus`，数据保存在受控 named volume。常规 `stop` 或 `down` 保留数据；`down -v` 删除数据，常规流程禁止执行。
- 容器后端仅在加入 Milvus 所在 Docker 网络后，才能通过 `http://standalone:19530` 连接；当前 T3 未验证 Fit-Agent 整体容器化。
- T3 已验证中文 analyzer、BM25 Function、`SPARSE_INVERTED_INDEX` + `BM25`、1024 维 `AUTOINDEX` + `COSINE` 和两路同表达式过滤。T5 固定物理配置见 §3.2；α、两路深度及无答案门槛的质量选择归 T8。
- 集成验证脚本只能建删明确归属该任务的探针集合，必须在连接和删除前校验目标归属。

配置校验在索引构建或需要检索能力的入口明确执行。无需检索的画像与历史浏览、有效快照确认保存保持可用；这些路径仅依赖已有原始目录及业务存储。

API 鉴权、限流、连接超时、Milvus 不可用和版本不匹配必须保留明确失败，不能把基础设施失败转换为空候选。Milvus RPC 在单次调用返回错误时直接传播，收尾遵循本节接受的阻塞边界。Embedding 超时与同循环生命周期保持 T4 约定。现有工具框架对异常的协议转换保持原样。

## 8. 画像、计划及保存边界

- 复用 ProfileContent 的列表字段、目录引用校验、完整画像快照、版本和 SQLite JSON 持久化。
- 临时限制不直接写画像；长期限制与解除必须经完整快照展示、按钮确认、授权校验后保存。
- 共享 ProfileFields 已展示 unavailable_equipment，使画像页及对话快照一致：null 显示未知，[] 显示无，非空数组按原顺序完整呈现。当前专项和免费联合检查已有退出 0 证据；测试中的手写 HTML 提取待整改，详见 [T7 复核](action-retrieval-profile-review.md)。不新增装饰性提示文案。
- `prepare_plan`、`prepare_plan_adjustment` 与保存继续核验原始目录 ID、画像版本、当前计划依据及已保存禁用限制。
- 本次检索加强候选过滤，保留既有计划保存校验。未知目录 ID 和 `exercise_id=null` 的现状语义保持 PRODUCT.md 及现有模型约束。
- 未确认画像变更不进入长期状态。画像变化使既有提案依据不匹配时，复用冲突与重新准备流程。
- 检索和详情调用均只读，不产生确认事件，不写训练事实，不代替原确认入口。

## 9. Trace 与并行开发边界

当前 `agent/tool.py::run_tool_batch` 已建立 tool Span，包含准备、执行及后处理；`_execute_with_cancel` 已区分异步与线程池执行并等待取消收尾。后续检索接入保留该完整生命周期。

- 检索耗时自然进入已有 tool Span 和根运行耗时，不要求 Trace 报告功能完成。
- Embedding 不加入当前聊天/摘要的 model Span 或用量统计。检索评测独立记录 API 次数、耗时与实际计费用量；新增 Embedding Span 类型或 call_kind 需要独立契约确认。
- Trace 的 tool 属性保持白名单，禁止加入查询原文、画像、候选结果和凭据。
- 保留根终态提交、观测收尾、通知以及确认授权与幂等顺序。
- `interfaces/http.py`、确认服务及通用 tool 层属于共享边界，检索实现前重新读取 Trace 最新源码并协调单一修改归属。
- 本任务只新增或修改三份检索文档，Trace 代码、文档及测试不作修改。

Pi 依据：`D:/repository/pi/packages/telemetry/src/noop.ts` 的 `startNoopSpan`、`src/memory.ts` 的 `startInMemorySpan` 及 README Adapter Contract。显式父上下文、callback 恰好执行一次和被动观测语义继续沿用当前 Trace 设计。

## 10. 回归与验收证据

| 场景 | 检查项 |
| --- | --- |
| 标准名称、别名、英文、中英混合、口语描述 | 相关动作命中、具体变式、排序改善和退化样本 |
| 居家有哑铃练背 | 环境默认、显式放行、背部相关性及有效限制 |
| 无哑铃替换 | 临时硬过滤、画像快照、确认后跨会话与重启读取 |
| 器械恢复与歧义 | 解除确认、含糊名称澄清、重复确认幂等 |
| 禁用动作、多动作别名、无答案 | 限制有效、歧义不误选、合法空结果 |
| 搜索与详情 | 候选数、批量、字符预算、合法 JSON、完整条目及后续信息 |
| 计划保存 | 画像/计划依据变化、未确认零提交、目录 ID 与限制校验 |
| API 与索引 | 真实连通、返回对齐、构建校验、重启复用及明确失败 |
| Trace 共存 | 工具父子关系、执行和后处理、取消、凭据白名单与业务结局 |

已有回归入口：`backend/test/check_business_profile.py`、`check_profile_confirmation.py`、`check_plan_core.py`、`check_plan_tools.py`、`check_plan_integration.py`、`check_plan_import_tools.py`、`check_agent_save_boundary.py`、`check_http.py`；前端现有 `verify:profile/verify:plan/verify:confirm/verify:runs` 脚本。按本次影响范围同步断言，测试存在不代表已执行通过。

同一提交报告当前名称检索、稀疏、dense、混合对照。每组保持语料、别名与预算可比，单独记录别名补充及裁剪贡献。记录 Recall@10、MRR@10、nDCG@10、无答案误返回、逐例证据、真实 tokens 和耗时。显式硬约束违反数为零，返回预算全部合规。

业务与实现测试由 subagent 实际运行并回报命令、退出码与输出。人工别名审阅及金标相关性标注必须真实完成。证据写入已忽略目录。

## 11. 本任务验证边界

T7 共享展示已实现，主会话读取 profile/confirm/build/HTTP/快照与联合检查的退出 0 记录，以及真实确认、刷新、跨会话和重开隔离 SQLite 的证据。本轮未重跑测试、未调用模型；测试使用正则剥标签和可见文本切分提取 HTML 字段，完整验收待合规整改。真实浏览器视觉/鼠标自动化未验证，详见 [T7 复核](action-retrieval-profile-review.md)。

T6 已实现 runtime/conditions/response 与业务、工具、HTTP 接线。配置加载、lookup 条件、循环归属关闭及阶段间取消已复核。新增独立真实 Event/task.cancel 验证退出 0：校验 RPC 返回后不编码，主循环搜索/桥接线程/工具均收尾，两个 SDK 在所属循环关闭，正式集合/alias/1324 条计数不变；新增 Embedding 请求及 tokens 为 0。此前已授权工具链证据为 3 请求、54 total tokens。当前 HybridRetriever.search 的可选 signal 与 _encode_cancellable 已接线，编码任务每 0.05 秒检查 Event，取消时 task.cancel 并等待收尾；runtime 仅委托。41–48 专项退出码均为 0。新增经用户授权的真实 SDK 检查退出 0：POST 完整发送并进入响应等待后设置 Event，约 50.499 毫秒返回准确取消异常，编码子任务结束、SDK 同循环关闭、无向量查询 RPC，正式数据及 1324 行计数保持；累计 1 次 POST 请求，provider usage 未返回、可能计费。本轮修复项验收通过，未运行业务回归仍保持未验证，详见 [T6 复核](action-retrieval-business-review.md)。

T1 完成源码核查；T2 完成语料、编码、查询与名称基线验证。T3 环境与探针安全验收通过：真实服务健康、检索能力和重启持久化均有证据；探针固定名称、连接前拒绝非法配置、精确删除及同前缀集合记录保留均已验证。正常流程退出 0，拒绝场景按预期退出 1。主会话已读取当前源码与运行证据，T3 本轮未重跑集成测试。T4 真实 Embedding 验证已由 worker 执行：21 条动作文本分 20+1 两批，模型返回 1024 维，live 与 auth 均退出 0；有限超时和事件循环归属边界验证通过，Ruff import 排序检查未通过，完整验收待通过。T5 已构建完整 1324 条正式索引，目录仅编码一次：67 请求、305035 prompt/total tokens，编码约 23.243 秒，构建约 28.404 秒。真实 BM25/dense/hybrid、过滤、参数边界、版本与失败发布保护、完整集合切换及保留卷重启均有证据；重启目录编码为 0。零重试修复后的独立复验中，T5 三文件 Ruff、inputs/metadata/check/boundaries 均退出 0，正式 alias、manifest、1324 条记录及两份保留集合保持。主会话已读取源码和 `tmp/action-retrieval-t5/51–61` 对应历史证据，这些完整验证本轮未重跑。本次独立 worker 的 inputs 原校验函数、T5 三文件 Ruff 及三路只读 smoke 均退出 0；复用已存向量，Embedding 请求为 0，集合及 alias 保持不变。T5 查询热路径 P2 修复已由独立 worker 执行 validation/config/search 与专项 Ruff，均退出 0。主会话已读取当前源码与 p2-* 证据；稳定校验 get/query/load_collection 均为 0，并发及集合变化按身份完整校验一次，失败/取消不记录成功身份，正式集合及 alias 保持。该轮 Embedding 请求为 0，可以开展 T6 接线开发；完整校验中途非取消失败的真实数据故障尚未直接覆盖。

当前 T5 验收遵循 §7 用户裁决及已列明的两项未实测边界。`tmp/action-retrieval-t5/41–45` 的 `DEADLINE_EXCEEDED` 超时验证作废，仅归档保留，不计入当前 RPC 策略验收。业务接线、业务回归和质量/性能评测继续归 T6–T8；T4 Ruff I001 保持原验收状态。

T2/T3/T4/T5/T6 负责将本文标注的技术参数和接口物理细节固定并同步文档；业务范围、两路融合、确认规则和可选精确匹配边界已确认。
