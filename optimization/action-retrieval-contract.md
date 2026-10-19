# 动作检索契约

设计见 [动作检索设计方案](action-retrieval-plan.md)。业务依据为 `PRODUCT.md` §2.1、§2.2、§3.2、§3.4、§4；观测遵循 [Trace 契约](trace-observability-contract.md)。

## 1. 动作身份与检索数据

- 原始目录为 `backend/dataset/actions.agent.json`，1321 条动作。ID 保持字符串形式及前导零，作为 Milvus 主键和业务引用。
- Catalog 提供名称、器械、肌群、重量口径及完整步骤；详情和业务保存复用原始事实，不改变 SQLite 业务身份。
- 别名和英文名称关联原始 ID，保留来源、审阅状态与版本；仅 `review_status=approved` 的内容进入索引。
- 别名允许一对多，保留不同 ID 的具体变式；歧义影响选择时澄清。
- 器械和肌群映射输出目录合法标识。`query_canonical()` 返回完整命中集合，消费方保留同义及泛称的多值语义。
- 英文名称来源为 MIT 许可数据集 `hasaneyldrm/exercises-dataset` 的 `data/exercises.json`，commit `7455efae41b330c265e7cd4b78dfa848e7ce5ebd`，仅使用数据层 `name`；媒体归 © Gym visual。

## 2. 编码与物理 Schema

编码版本为 `retrieval-text-2026-10-14.2`，固定顺序为：

```text
name, aliases, english_name, equipment, body_part, target, muscle_group, steps_zh, steps_en
```

段间使用 `\n`，空字段跳过；器械和肌群包含 canonical 标识及中文映射。文档与查询共用 NFKC、空白折叠和 casefold 规范化。

Milvus 字段：

- `id`：VARCHAR 主键，64 字节。
- `text`：VARCHAR，4096 字节，`analyzer_params={"type":"chinese"}`。
- `equipment/body_part/target/muscle_group`：各为 VARCHAR，128 字节。
- `dense`：1024 维 FLOAT_VECTOR，`AUTOINDEX/COSINE`。
- `sparse`：SPARSE_FLOAT_VECTOR，由 BM25 Function `text_bm25` 从 `text` 生成。
- sparse 索引：`SPARSE_INVERTED_INDEX/BM25`，`inverted_index_algo=DAAT_MAXSCORE`、`bm25_k1=1.2`、`bm25_b=0.75`。

字符串容量按 UTF-8 字节校验，工具响应预算按字符计量。器械与动作限制进入结构化过滤；原始查询禁止直接拼接到过滤表达式，标识须经目录验证和安全参数编码。

## 3. 搜索输入与执行

`search_exercises` 参数：

- `name`：必填非空语义输入，支持名称、别名、英文及描述。
- `purpose`：必填，枚举 `lookup/generation/replacement`。
- `equipment/body_part/target/muscle_group`：可选精确筛选，保留既有含义。
- `task_environment`、`available_equipment`、`unavailable_equipment`、`excluded_ids`：本次请求的环境、器械声明与排除条件。

`BusinessService.search_exercises` 为异步方法，工具通过 `MainLoopCall` 投递主循环。会话、运行和消息身份由后端可信上下文绑定。

### 3.1 用途

- `lookup`：基础范围为全目录器械，应用本次临时不可用器械、`excluded_ids` 和显式精确筛选；不叠加长期画像禁用或环境默认。可用声明不收窄范围。允许核实受限动作，回答须说明真实要求。
- `generation/replacement`：遵守环境默认、器械声明、已保存画像限制、本次临时限制和显式筛选。

### 3.2 条件求值

- 居家或户外默认可用 `body weight`；其余或未知环境默认目录器械可用。
- 明确可用声明放行对应器械；“只用某器械”形成精确筛选。
- 已保存 `unavailable_equipment` 和 `forbidden_exercise_ids` 从结构化画像读取。本次临时限制不写入长期状态。
- 可用声明与已保存不可用限制冲突时，经过澄清、解除限制及画像确认流程，禁止静默删除。
- `null` 表示未知，空数组表示明确没有清单项；未知值仍遵守环境默认。
- 两路召回使用相同硬过滤条件，输出再次复核；相关性分数不能覆盖硬约束。

“家里有哑铃，想练背”允许符合限制的 `body weight` 与 `dumbbell` 候选；明确筛选背部时使用 `body_part=back`。

`body weight` 的实际设施可执行性受现有标签覆盖限制，单杠、双杠和训练凳等依赖补标不在范围内。

## 4. 搜索与详情输出

序列化口径为 `json.dumps(..., ensure_ascii=False, allow_nan=False)`，预算包含全部公开字段。

### 4.1 搜索

```text
{items, has_more}
```

- `items` 每项为 `{id, name, equipment, target, matches}`，最多 5 条，完整 JSON 上限 4000 字符。
- 单次检索读取 20 个候选形成候选池；门槛及响应预算裁剪前的前 20 条为真实候选池，用于候选阶段评测。
- 最终公开条目从同一候选池产生，按 RRF 顺序应用门槛与完整 JSON 预算，最多 5 条；预算不足时允许少于 5 条，禁止截断 JSON 或单条动作结构。
- `has_more` 仅表示同一候选池中仍有通过门槛但被数量或预算裁剪的候选；不暴露内部剩余候选的 ID、名称或排名。
- `matches` 仅描述可核实目录字段和用户条件；缺少词项证据时不得宣称某路精确命中。
- 无可用候选或未通过返回门槛时返回合法空结果。
- 不增加精确名称优先、分组、额外词项打分、翻页或对应开关。

### 4.2 详情

- 工具名 `get_exercise_details`，`ids` 为 1..5 个原始动作 ID。
- 按输入顺序返回原目录九个事实字段，包括完整中英文步骤及重量口径；完整 JSON 上限 7000 字符。
- 校验空 ID、未知 ID、重复 ID 和批量上限；未知 ID 明确报错。
- 按完整动作裁剪，明确已返回和剩余 ID；单条详情超出预算时明确报错，禁止静默缺失步骤。
- 允许读取受限动作；详情读取不构成推荐可用性或保存授权。

工具在业务序列化前结构化裁剪，保持 `max_output_chars=None`，避免通用字符串切片破坏 JSON。`AgentTool.format_error` 和 `validate_output` 为末尾可选字段，默认 `None`，仅检索工具启用。

## 5. 召回与融合

- 路径顺序固定 `[BM25, dense]`，使用 Milvus 内置 `RRFRanker(k=60)`。
- 融合分数为 `Σ 1 / (60 + rank_i(d))`，排名从 1 开始；某动作未进入某路召回时该路贡献为零。
- 每路召回深度独立于最终返回上限。正式基线为 `fusion=rrf`、`rrf_k=60`、深度 `100/100`、`threshold=0.0`。
- 候选池上限 20、最终模型输入上限 5 与 RRF 平滑参数 60 分别为独立配置；生产入口与评测入口共用同一阶段构造，不重复检索。
- 实际参数随代码、数据和索引身份记录。门槛作用于 RRF 分数；融合分数仅表示排序依据，零门槛不保证无答案拦截。
- 目录向量预计算，在线仅编码查询；BM25 直接提交文本，由 Milvus 生成 sparse 表示。

## 6. 索引校验与发布

- 清单记录目录哈希、补充数据版本、API 服务地址、模型、维度、编码、analyzer、索引参数、服务与 SDK 版本。
- 构建、实例启动及集合或清单身份变化时执行全量校验，覆盖数量、唯一 ID、字段、维度、源数据及配置兼容性。
- `HybridRetriever` 实例缓存已验证物理集合名和完整清单指纹；实例内锁合并并发首次校验，仅成功后记录身份，实例重建重新校验。
- 稳定查询核验 alias、实际源哈希、服务和配置兼容及清单完整性，仅调用 `list_aliases/describe_alias/get_server_version/describe_collection`，不回读全目录向量。
- 正式 alias 为 `fit_agent_actions_current`，候选集合名为 `fit_agent_actions_build_<32位UUID hex>`，发布串行执行。
- 完整校验、flush/load 及真实 BM25、dense、hybrid 查询通过后原生切换 alias，保留既有正式集合。失败构建不破坏已发布索引。
- 清单格式为 `action-index-schema-metadata-1`：collection `description` 保存格式、字段名、UTF-8 字节数和 SHA-256，完整 JSON 存于 Schema 的 `text.description`，通过 `describe_collection` 回读验证。
- collection description 上限 1024 字节，清单应用容量上限 4096 字节。
- 目录、模型、编码或索引配置变化后重建；禁止混用向量空间。索引缺失、清单损坏或版本不兼容直接失败，禁止自动模型替换或检索降级。
- 重启复用兼容索引。探针仅操作明确归属的集合，在连接和删除前校验目标归属；正式、保留集合及卷受保护。

## 7. Embedding 配置与生命周期

配置模块为 `app/application/business/retrieval_config.py`，客户端为 `retrieval_embedding.py`，接口包括 `load_embedding_config()`、`EmbeddingClient.from_env()`、上下文内 `encode_query/encode_batch` 及 `validate_embedding_data()`。

- `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID` 为必需后端配置，缺失或空值立即报错。
- Base URL：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- `FIT_AGENT_EMBEDDING_MODEL=qwen3.7-text-embedding-flash`。
- `FIT_AGENT_EMBEDDING_DIMENSIONS=1024`。
- 使用 OpenAI Python SDK 异步 `embeddings.create`，请求 `dimensions=1024`、`encoding_format="float"`，每批最多 20 条。
- 校验返回数量、index 对齐、1024 维、有限值及非零范数。
- 创建、请求和关闭必须属于同一事件循环，通过异步上下文释放资源。显式 timeout 为有限正数；SDK 默认 connect 5 秒、read/write 600 秒。
- `HybridRetriever.search` 接收取消 signal，编码期间每 0.05 秒检查 Event，取消时 `task.cancel` 并等待编码协程收尾。
- `usage.requests` 仅统计成功响应，失败尝试单独记录。
- 根 `package.json` 的 `dev:backend` 使用 `uv run --env-file .env`，仅后端子进程加载已忽略的 `backend/.env`。Embedding 与聊天模型配置独立。
- 禁止密钥进入前端、日志、索引、评测证据或 Trace；只发送检索所需文本，画像限制在后端转换为结构化过滤。

## 8. Milvus 部署与失败语义

- 固定 Milvus `v3.0.2`、PyMilvus `3.0.2`、etcd `v3.5.25`、MinIO `RELEASE.2024-12-18T13-15-44Z`，使用内置 Woodpecker。
- Compose 项目为 `fit-agent-milvus`，数据保存于受控 named volume。`stop/down` 保留数据，常规流程禁止 `down -v`。
- `FIT_AGENT_MILVUS_URI=http://127.0.0.1:19530`，`FIT_AGENT_MILVUS_TOKEN` 可空，`FIT_AGENT_MILVUS_TIMEOUT=10` 秒。URI 与 TIMEOUT 为必需配置。
- 无鉴权服务仅发布本机回环端口，内部网络、本机访问主体及 MinIO 凭据均须受控。
- 容器后端加入 Milvus 所在 Docker 网络后使用 `http://standalone:19530`。
- 连接建立使用 `MilvusClient(..., timeout=self.timeout)`；`MilvusConnection.call()` 传入 `retry_times=0, retry_on_rate_limit=False`，每次 RPC 使用 `timeout=None`。
- RPC 单次错误直接传播，关闭 SDK 装饰器的 RPC 和限流自动重试。gRPC 传输层后台重连仍可能发生。
- 同步 Milvus I/O 在线程内执行，调用和关闭串行。连接存活且服务端暂停时 RPC 可持续阻塞，取消和关闭需等待当前调用返回。
- 配置校验发生于构建或检索入口，不影响仅依赖原始目录和存储的画像、历史浏览及有效快照确认保存。
- 鉴权、限流、连接超时、Milvus 不可用及版本不匹配必须明确失败，禁止转换为空候选；现有工具框架的异常协议转换保持原样。

## 9. 画像与计划保存

- 复用 `ProfileContent`、目录引用校验、完整画像快照、版本及 SQLite JSON 持久化。
- 长期限制及解除须经快照展示、按钮确认和授权校验；未确认零提交。
- `ProfileFields` 统一展示画像页和对话快照的 `unavailable_equipment`：`null` 显示未知，`[]` 显示无，非空数组按原顺序完整呈现，不增加装饰性文案。
- `prepare_plan`、`prepare_plan_adjustment` 和保存继续核验原始目录 ID、画像版本、当前计划依据及已保存禁用限制。
- 画像变化导致提案依据失效时，复用冲突及重新准备流程；未知 ID 与 `exercise_id=null` 遵守 PRODUCT.md 和既有模型约束。
- 检索和详情均只读，不产生确认事件，不写训练事实，不代替确认入口；确认保留授权、幂等和事务边界。

## 10. Trace

- 沿用 `run_tool_batch` 的准备、执行、后处理 Span 及 `_execute_with_cancel` 的取消收尾生命周期。
- 检索耗时进入 tool Span 和根运行耗时；Embedding 不加入聊天或摘要 model Span 及其用量统计。
- tool 属性遵循白名单，禁止查询原文、画像、候选及凭据。
- 保留根终态提交、观测收尾、通知及确认授权和幂等顺序。
- 沿用显式父上下文、callback 恰好执行一次和被动观测语义。

## 11. 质量与一致性要求

- 评测覆盖标准名称、别名、英文、中英混合、描述、条件、多动作别名、禁用及无答案，人工审阅别名和相关性标注，按意图与别名家族拆分数据。
- 名称、BM25、dense、hybrid 对照使用同一提交及可比语料、别名与预算，单独记录别名补充及裁剪贡献。
- 候选阶段使用 Recall@20：前 20 条候选命中相关 ID 数除以金标全部正相关 ID 数；候选池为空时为零。
- 最终模型输入阶段使用 Hit@5、MRR@5、nDCG@5、Precision@5：只按实际公开的前 5 条及其顺序评分；Precision@5 固定分母 5；nDCG 使用增益 `2^grade−1`、折扣 `log2(rank+1)`；MRR 无命中为零。
- 无答案误返回率只按最终模型输入是否非空统计；有金标空返回的四项 @5 为零，Recall@20 保持候选阶段真实值。
- 保存 query、约束、金标 ID、候选 ID、分数、模型和版本及逐例退化证据；动作身份及硬约束由程序核验。
- 显式硬约束违反数为零；候选数、批量、字符预算、完整 JSON 和完整条目全部合规。
- 覆盖真实接口及索引构建、对齐、重启复用、失败发布保护、取消和资源释放。
- 业务覆盖动作核实、生成、替换、限制保存与解除、查询刷新、跨会话、重启、重复确认、未确认零提交、授权及提案依据有效性。
- 端到端统计包含搜索、详情及全部模型输入，记录真实 tokens、重复调用与耗时；质量提升不设置百分比门槛，量化评测独立于基本功能完成判定。
