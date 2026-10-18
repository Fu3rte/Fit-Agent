# 全新评测数据准备方案

## 1. 范围与配额

全新编写 150 条检索请求和 80 个端到端业务场景，统一全量评测。用户负责金标、参考答案和行为断言的最终审阅。技术方案由助手确定；技术方案确定不构成数据批准。

检索用途配额：

- `lookup`：60 条，其中预期空结果 10 条。
- `generation`：45 条，其中预期空结果 10 条。
- `replacement`：45 条，其中预期空结果 10 条。

共 120 条有答案请求、30 条无答案请求。标准名、别名、英文、中英混合、描述、歧义、器械与禁用限制作为交叉标签。空结果判断必须符合真实目录与用途条件。

端到端场景设 10 个主要类别，每类 8 个：动作核实、画像更新、计划录入与文件处理、不完整计划与结构调整、新计划生成、动作替换与限制、基于表现调整、实际训练记录与查询、确认取消与状态变化、澄清授权边界与医疗风险。每个场景确定一个主要类别，附加标签允许重叠。

一次完整条件下的搜索计 1 条请求，一个完整业务场景计 1 个场景。消息、工具调用、按钮事件与重复运行单独计数。场景链、同义表达与动作家族保留关联，供去重和分组分析。

历史量化指标、逐例结果、旧数据版本和实验身份完整存档。新查询、qrels、初始状态、参考答案和业务断言全部重新编写。corpus 从真实业务目录与已批准补充数据构建新快照，保持动作 ID 和事实准确。

## 2. 文件与 Schema

采用 UTF-8 JSONL 数据文件，每行一个完整对象；JSON Schema 和版本清单使用 JSON。使用项目已安装的 `jsonschema.Draft202012Validator` 校验 JSON Schema Draft 2020-12，使用成熟的 `jsonlines` 库读取 JSONL，单行 JSON 严格解码复用标准库 `json`。实现时将 jsonlines 纳入依赖与锁文件。业务参数和初始内容复用现有 Pydantic 模型与业务校验。

数据目录为 `backend/dataset/evaluation/<dataset_version>/`。版本内包括：

- `corpus.jsonl`：每行一个真实目录动作文档。
- `queries.jsonl`：每行一条完整检索请求，共 150 条。
- `qrels.jsonl`：每行一个 query_id 与 corpus_id 的相关性判断。
- `scenarios.jsonl`：每行一个完整业务场景，共 80 个。
- `manifest.json`：数据身份、文件与 Schema 哈希、来源版本、数量、配额和发布批准状态。
- `assets/`：业务必需的合成计划附件，逐个绑定哈希。

Schema 位于 `backend/dataset/evaluation/schemas/`，版本内 manifest 绑定实际使用的 Schema 哈希。数据目录中只允许 manifest 声明的数据和附件文件，以及固定名称的 manifest。JSONL 空行、非对象记录和 UTF-8 BOM 均拒绝；解析失败报告文件和行号。

所有对象封闭字段，拒绝未知字段、重复 JSON 键、NaN、Infinity、重复 ID 和非法引用。字段类型、必需性、枚举和空值由 Schema 明确约束。已有业务模型允许的合法缺省继续遵循业务语义；评测输入显式记录影响条件求值的值，防止无意依赖默认值。

### corpus.jsonl

每行字段为 `corpus_id`、`text`、`metadata`、`source`。

- `corpus_id` 为原始字符串动作 ID，保留前导零。
- `text` 与真实索引编码和规范化结果一致。
- `metadata` 包含器械、部位、目标肌肉和肌群等实际筛选字段。
- `source` 记录目录及已批准补充数据的版本、哈希与来源定位。

从实际目录推导动作总数并绑定 manifest；当前目录为 1321 条。加载时校验快照与来源身份。

### queries.jsonl

每行字段为 `query_id`、`scenario_id`、`query`、`purpose`、`conditions`、`tags`、`family`、`expect_empty`、`judgement_scope`、`review`。

- `query` 为自然语言检索文本，使用动作名称、别名或描述，运行时映射到真实工具的 name，不包含目录动作 ID。
- `purpose` 直接保存 `lookup/generation/replacement`。
- `conditions` 保存线上结构化筛选、本次环境与器械条件，以及已保存画像；复用 `CatalogSearchArguments`、`ProfileContent` 和真实条件求值语义。
- `scenario_id` 为关联场景 ID 或 null；关联不强制 Agent 产生固定调用轨迹。
- `judgement_scope` 保存检查范围、候选来源、遗漏复查说明与 `complete`。
- `review` 保存用户审阅状态及记录，查询批准同时覆盖完整相关性判断。
- 口语限制经用户审阅后映射为合法目录条件；尚未确定映射的请求保持草稿。

### qrels.jsonl

每行字段为 `query_id`、`corpus_id`、`grade`、`rationale`、`evidence`、`review`。

grade 为 3 高度相关、2 相关、1 弱相关、0 已审阅且不相关。evidence 使用冻结来源文件及 JSON Pointer 定位，并附简短依据。每个 query_id 与 corpus_id 组合唯一。

无答案请求允许没有 qrels 行，也可保留已审阅负例；必须在 queries 中显式标记 expect_empty 并确认 judgement_scope.complete。有答案请求至少有一个 grade≥1 动作。所有正例必须符合对应用途的有效硬条件。

### scenarios.jsonl

每行字段为 `scenario_id`、`input`、`initial_state`、`events`、`reference`、`rubric`、`assertions`、`metric_applicability`、`tags`、`family`、`review`。

- `input` 保存当前消息和必要前文，附件以冻结文件引用表示。用户消息、前文中的用户发言、message 事件正文及用户提供的计划附件使用动作名称、别名或自然描述，不包含目录动作 ID，也不要求用户指定内部标识。
- 动作 ID 保留在 corpus、qrels、结构化初始状态、工具结果和内部断言中；模型依据检索结果完成自然语言与动作身份的对应。
- `initial_state` 保存合成画像、计划、记录、必要版本、固定日期与时区，业务内容复用现有模型。
- `events` 保存后续有序事件及 event_id，包括消息、按钮确认、取消、会话切换和重启。`message` 事件只承载实际待发送的用户输入；预期助理回答只进入 `reference`，实际回答、工具返回与快照由运行时采集。必要的历史对话放在 `prior_messages`，角色真实、来源明确。
- `reference`、`rubric`、`assertions`、`metric_applicability` 按回答检查点关联 event_id，支持多轮场景。
- `rubric` 包含必须事实及来源、允许变化和禁止断言。
- `assertions` 采用带类型的业务断言，禁止执行任意代码或表达式。
- `metric_applicability` 逐指标声明适用性与理由；适用时 reference 等必需字段非空。
- `tags` 包含唯一主要业务类别及允许重叠的附加标签；`review` 保存用户审阅记录。

每次按钮事件以业务类型和前置步骤产生的符号引用定位快照，由运行器解析为真实 ID，再通过真实 HTTP 确认入口执行。出现多个可匹配目标时就地失败。提示中不能把评测期待的结果作为模型输入。

## 3. 加载器

新增 `backend/evaluation/dataset.py`，集中负责数据加载与验证，复用现有目录、文本编码和条件求值实现。调用方为检索运行、端到端运行和上传入口。

提供两个明确入口：

- `load_for_review(version_dir)`：校验结构、来源、引用和语义，返回完整数据及审阅状态。只用于本地审阅与数据检查。
- `load_for_evaluation(version_dir)`：完成同样检查，再核验全部审批和配额，返回已冻结的完整评测数据。正式运行和上传只使用此入口。

验证顺序：

1. 校验版本目录与 manifest 的合法路径、格式和版本。
2. 校验声明文件、Schema、附件和业务来源的 SHA-256。
3. 使用 jsonlines 逐行读取 JSONL 并严格解码，执行每行的 JSON Schema 校验；manifest 使用 JSON 校验。
4. 使用现有业务模型校验输入和初始状态。
5. 校验 ID 唯一性、引用完整性、数量、配额和事件顺序。
6. 复用真实条件求值，校验 qrels 正例硬条件及空结果一致性。
7. 正式入口核验逐条用户批准、完整覆盖判断及最终发布批准。

出错就地失败，错误包含文件、条目 ID 和字段路径。不跳过条目，不自动改写条件，不自动补批准。任何正式运行都执行全部 150 条或全部 80 个场景。

路径必须位于版本目录内，Schema 引用仅允许本地固定资源。加载器无外部网络和模型调用。上传字段白名单在上传边界单独执行。

## 4. 审批校验

审批以用户明确审阅意见为来源。助手可以记录用户决定，不能自行批准草稿。Schema 合法和哈希一致分别证明结构与内容身份。

每条 query、qrel 和 scenario 的 review 保存 `draft/approved/deferred` 状态、审阅者、时间、意见、批准内容哈希及用户决定引用。query 的批准内容哈希覆盖自身与关联 qrels，计算时排除 review 字段，使用固定的 JSON 序列化及记录排序规则。scenario 的批准内容哈希覆盖场景正文及附件哈希，排除 review 字段。

manifest 的 release 保存最终发布状态、审阅者、时间、用户决定引用及内容清单摘要。内容清单摘要覆盖 manifest 中除 release 外的字段，固定序列化后计算 SHA-256。

query 的批准范围同时覆盖该请求及完整 qrels；scenario 的批准范围覆盖初始状态、事件、全部回答检查点与附件。语料来源和共享 Schema 由发布级批准覆盖。

manifest 记录 corpus、queries、qrels、scenarios、附件及 Schema 的原始字节 SHA-256。逐条审批完成后生成这些文件哈希，再由 release 绑定内容清单摘要，避免循环依赖。

最终发布必须满足：150 条 query、80 个 scenario 及全部 qrel 均有有效 approved 记录；用户确认全部请求的 judgement_scope.complete；没有 draft 或 deferred；release 为 approved；审阅者为用户；所有引用与内容哈希一致。

任意受 manifest 保护的内容变化都生成新数据版本，原批准对新版本失效。采用整个发布版本重新确认的规则，维持审批逻辑可解释。

审批记录是本地可信工作区中的审阅凭据，不能防御有权同时篡改文件和记录的人。哈希不能替代人工判断，也不提供数字签名能力。

## 5. 相关性与生成判定

qrels 只把 grade≥1 计入二元相关集合。未审阅动作保留未知身份。正式结果中出现未审阅候选时，保留实际证据并阻止正式汇总发布；补标、用户批准新版本后，对同一数据版本重算全部对照组。

检索默认计算 Hit@5、Recall@5、Precision@5、MRR@5、nDCG@5。Precision@5 固定分母 5；nDCG 使用 `2^grade−1` 增益。有金标空返回五项记 0；无答案只统计误返回率。评分按最终过滤、门槛及裁剪后的实际顺序执行。

宽泛查询审阅完整相关集合。无答案需要目录范围复核。相关集合与分级不能由实际排名自动生成。

生成事实评分使用 Ragas Faithfulness 与 FactualCorrectness。reference、rubric 与确定性 assertions 分别保存。开放计划允许多个合理方案，逐事实核对并解释方案变化。

生成指标记录分数与证据，本轮不设未经校准的数值通过门槛。器械、禁用动作、预算、JSON、快照、保存授权和状态完整性规则要求全部通过。指标不适用需说明理由，评分失败记为失败并阻止完整有效结果发布，保留已产生证据。

## 6. 真实运行与上传

每个端到端场景在独立业务存储和附件目录中初始化，只使用合成状态，通过真实 Agent、业务服务和 HTTP 确认入口执行。跨会话与重启在同一场景隔离范围内真实执行。临时运行数据写入已忽略的 `backend/temp/`。

以 run_id、scenario_id、event_id 与工具调用 ID 关联实际 contexts、response、快照、排名、耗时、事件和状态变化。contexts 仅收录该回答检查点生成前实际进入模型输入的证据；保留裁剪与来源。response 包含用户可见文本和结构化快照。

用户提供的事实单独保留来源，并明确传入裁判的证据口径。reference 与业务期待仅传给评估器。一次场景有多个检查点时保留逐检查点分数，汇总先按场景聚合，避免长场景占据更大权重；不适用与失败分别计数。

LangSmith 管理样例与实验；MTEB 计算真实排名指标；Ragas 使用已确定的 StepFun `step-5-preview` 裁判。模型服务兼容性及各库 API 在实现时核验。生成、提示、裁判与指标版本绑定实验身份。

上传仅消费经过审批的数据与对应真实运行结果，执行已有授权范围的独立字段白名单，密钥与服务凭据保留本地。生产 Trace 隐私契约保持完整。

## 7. 实现验收

实现时增加真实 Schema、加载器和小型数学及边界测试，覆盖：

- 合法数据加载、150/80 数量与配额、字符串 ID 前导零及空值语义。
- JSONL 空行、BOM、非法单行、非对象记录、未知字段、重复 JSON 键、非有限数、重复 ID、缺失文件和非法路径。
- 目录、Schema、附件、数据内容或 manifest 变化导致批准失效。
- 未批准、暂缓、伪造目标 ID、遗漏 qrels、重复批准和不完整覆盖。
- 用途条件、相关正例合法性、空结果矛盾及未审阅候选门禁。
- 多轮事件与回答检查点引用、唯一快照解析和真实确认授权。
- 消息事件不得承载预写助理回答、消息检查点零提交、保存成功仅绑定确认事件、悬空事件引用拒绝、业务事件检查点不适用生成指标。
- 指标适用性、失败保留、场景级聚合和上传内容与证据绑定。

代码与业务测试按项目要求派发 subagent 运行，超时不超过 300s；主会话依据命令、退出码与输出判定。正式数据批准前不能产出正式业务评分或上传结果。

## 8. 断言语义

场景断言为带类型的封闭集合，运行器按 `checkpoint_id` 与 `event_id` 定位检查点后逐条判定，禁止执行任意代码或表达式。

- `state_unchanged`：字段为 `checkpoint_id`、`event_id`、`kind`（固定 `state_unchanged`）、`business_kind`（`profile`/`plan`/`workout`/`all`）。判定对象是该事件执行前后的完整业务投影，`all` 表示三类业务同时比较。纯查询、明确澄清及重复确认等确需整体不变的检查使用该断言。
- `saved_data_unchanged`：字段为 `checkpoint_id`、`event_id`、`kind`（固定 `saved_data_unchanged`）、`business_kind`（`profile`/`plan`/`workout`/`all`）。判定对象是该事件执行前后的已保存业务内容、记录 ID 与版本号、`is_current` 当前计划标志及保存记录元数据，`all` 表示三类业务同时比较。待确认提案、提案展示与确认绑定不属于该断言的比较范围。生成或修改待确认提案的消息检查点使用该断言验证已保存数据保持完整；快照生成与完整展示继续由 `snapshot_integrity` 与 `save_authorization` 检查。
- 业务投影范围：已保存内容的全部字段与顺序、记录 ID 与版本号、`is_current` 当前计划标志、保存记录（创建与更新时间）、相关提案内容与依据、快照状态（待确认/处理中/已保存/已失效/冲突）以及确认绑定（展示节点与确认入口参数）。
- 排除范围：会话消息、Trace 与仅运行期的观测数据（耗时、tokens、上下文拼接结果）。
- 空值语义：记录不存在与记录存在但内容为空分别判定，前者不算空内容，后者不算缺失。
- `parameter_preserved`：只表达单个具体参数的保留，`field_path` 为该参数在业务投影中的路径，`expected` 为原值；需要证明整体未变时，按比较范围使用 `state_unchanged` 或 `saved_data_unchanged`。
- 其余断言：`equipment_restriction`（允许与禁止的器械）、`forbidden_exercise`（禁止出现的动作 ID）、`snapshot_integrity`（快照仍为准备时的内容）、`save_authorization`（是否保存成功）、`state_transition`（业务状态迁移）、`evidence_present`（实际证据条目数）、`no_saved_state`（截至该检查点不存在对应业务类别的已保存记录；已有保存记录时不得用于表达保存前零提交）、`saved_state`（截至该检查点存在已保存记录）。
- 保存时点规则固定：`save_authorization` 绑定消息事件检查点时 `expected_saved` 只能为 false，用于验证保存前零提交；声明保存成功时必须绑定 `confirm` 事件检查点，且断言的 `proposal_ref` 与该确认事件的目标一致。`state_transition` 的 `saved` 只能出现在确认事件检查点，`invalidated` 只能出现在取消事件检查点。需要表达“截至某时点已经保存”的回归检查使用 `saved_state`。
- 提案引用固定为 `<business_kind>-proposal-<触发提案的事件>`：确认与取消事件的 `source_event_id` 必须指向真正触发提案的用户消息事件，展示与确认入口引用由同一事件派生，取消事件不携带展示与确认入口。消息事件不得承载参考回答正文。
- 纯授权、取消与状态类检查点没有模型生成的事实内容，其 `faithfulness` 与 `factual_correctness` 记为不适用并写明原因，`deterministic_rules` 适用并至少绑定一条断言。
- 加载器按封闭分支校验断言类型与业务字段；新增断言类型时必须同步 Schema、加载器与运行器，不得以未校验字段绕过。

## 9. 当前状态与边界

数据版本 `backend/dataset/evaluation/rebuild-v1/` 已生成完整静态数据：corpus 1321 条、queries 150 条（lookup 60、generation 45、replacement 45，其中无答案各 10 条）、qrels 1079 条、scenarios 80 个（10 类各 8 个）、附件 8 个。manifest 绑定真实目录、补充数据、字段映射、Schema、数据文件与附件哈希。目录已按用户批准删除三对逐字节重复动作（保留 0454、0576、0655），补充语料与字段映射升至 `2026-10-18.1`，审批记录重绑文件哈希与条目计数。

用户已明确批准冻结当前版本。全部 query、qrel、scenario 的 `review` 及 `manifest.release` 为 `approved`。worker 实际执行正式加载与冻结一致性校验，退出码 0，`load_for_evaluation` 通过，`ready=true`、`issues=[]`。冻结内容、哈希和授权依据见 [冻结记录](evaluation-rebuild-v1-freeze.md)。场景断言的比较范围已修订：纯查询、明确澄清与重复确认保留 `state_unchanged`；生成或修改待确认提案的消息检查点改用 `saved_data_unchanged`；快照生成与完整展示继续由 `snapshot_integrity` 与 `save_authorization` 检查。加载器封闭分支已同步新类型，并拒绝与检查点已保存记录矛盾的 `no_saved_state`。本次冻结校验未执行真实检索、Agent 端到端、Ragas 裁判或 LangSmith 上传，也未核验这些运行入口的接入状态；`saved_data_unchanged` 的真实运行比较不属于本次验收。

场景事件已完成角色核查：80 个场景的消息事件只保留实际待发送的用户输入，写好的助理回答、快照正文与工具结论移入 `reference`，要求移入 `rubric` 与 `assertions`。保存类断言在消息检查点验证零提交，保存成功绑定真实 `confirm` 事件检查点，取消、重复确认、重启与跨会话按实际采样时刻绑定断言。重复确认场景对两次点击分别设检查点：首次确认断言保存成功与状态迁移，重复确认断言业务投影未变且仍处于已保存状态。corpus 1321、queries 150、qrels 1079、scenarios 80 与附件 8 保持不变。

数据冻结时间为 `2026-10-18T12:03:51+00:00`，内容摘要为 `634fd90d4a9326efb92129afb416daf061e5cad1466b0c7e049447886e96cef1`。批准基于用户明确授权，正文、条件、金标、参考答案与断言未因冻结而变化。评测结果反映该固定集合上的表现，反复调优存在过拟合风险。历史指标原值和原件保持完整。
