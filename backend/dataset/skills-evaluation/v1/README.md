# 训练共享知识库测评集 v1

## 身份与边界

`cases.jsonl` 为独立的30题合成样例集，ID固定为 `skills-v1-001` 至 `skills-v1-030`。24题应按需读取训练知识库，6题为自包含负例。所有参考答案和要求均为待人工复核草案，`review.status=draft`，无用户或专家批准。本次只准备数据，无真实回答、模型调用、检索调用、外网复核、云端上传或评测分数。

适用任务为训练专业回答质量、自然触发、主题按需加载及上下文使用。当前项目MTEB承担动作目录排序评测，使用动作相关性金标计算候选池Recall@20及最终输入@5指标。本集无动作检索query/corpus/qrels，30题无法用于现有动作召回Recall@20。两类指标分别报告。

不改动 `dataset/evaluation/rebuild-v1/v2/v3`、历史实验身份、审批与配额。正式运行继续遵守当前暂停要求；数据准备和离线结构核验均不构成真实调用或解除暂停的授权。

## 题目分布

- 001—003：RIR/RPE与速度、力竭、酸痛和长期效果。
- 004—006：可执行频率、计划分析、目标未知。
- 007—009：训练者与单动作分层、传统硬拉起点、职责一致的替代。
- 010—012：杠铃与哑铃口径、辅助与负重引体、技术幅度可比性。
- 013—016：明确改为55kg、记录不足、复训终末结果与首日负荷、重建基线。
- 017—019：掉次数归因、效率分母、组结构与密度。
- 020—024：频率与总量前提、休息比较口径、耐力证据范围、来源与个体差异、疼痛边界。
- 025—030：日期提取、字段重排、提供次数求和、语言改写、标题排序、缺失值原样提取。

15主题覆盖位置（编号可有交叉）：

- `methodology.md`：003、018、023。
- `evidence-grading.md`：020、021、023。
- `sources.md`：015、021、022、023。
- `performance-factors.md`：017、022。
- `effort-and-fatigue.md`：001、002。
- `training-variables.md`：019、020。
- `progression.md`：010—016中的相关题。
- `goal-content.md`：004、006。
- `trainee-classification.md`：007。
- `program-design.md`：004、005。
- `exercise-technique.md`：009—012、024。
- `exercise-selection.md`：008、009。
- `adjustment.md`：013、015、016、024。
- `diagnosis-and-experiment.md`：016—018。
- `myths.md`：002、003、022。

013检验用户明确给定55kg时可采用该数值，并保留验证与保存边界。015检验复训终末结果无法推出首日重量或确定恢复时间。022检验“数据不足”的综述范围，保留具体耐力研究结果与测法差异。以上要求均按语义判定。

## 字段与输入隔离

每行是独立case结构，字段含义如下：

- `id`：稳定样例身份；`type`、`tags`：题型和分组标签。
- `prompt`：自然用户请求，无读取skill或路径的指令。
- `context.synthetic=true`、`context.content`：完整必要上下文，所有人物、记录和情形均为合成；无额外上下文也显式声明。不得当作真实用户事实。
- `should_use_skill`：任务级期望是否使用 `training-theory`。正例需在正常工具权限下有自然发现入口的机会；该标记不注入模型输入。
- `topic_paths`：仓库根相对路径，作为相关主题和可接受知识路径候选。目录内跨主题的等价论据、能解决问题的关联读取可经人工接受；无须逐项读取这些文件。只读入口索引不能计为已加载主题正文。负例为空。
- `expected`：每题3—5条语义要求；`critical_errors`：严重错误清单。样例覆盖不足时，人工仍需标记真实危害与事实错误。
- `allowed_variation`：成立的替代表达或安排；不得用于豁免必要要求。
- `reference_answer_points`：参考回答要点草案，无固定段落要求、无逐字匹配要求。
- `reference_sources`：可核查的本地文件、章节定位和已有研究标识。章节名用于定位；研究标识照录当前来源登记。负例以本行提供文本及基础运算为封闭依据。
- `review`：草案状态、本次未外网复核声明；`external_verified=false`无法证明任何独立科学核验已完成。

只有 `prompt` 与 `context.content` 进入待测模型的初始输入。`topic_paths`、要求、参考要点、来源与review属于评测侧材料。读取主题时生产 `read` 工具使用后端相对路径，例如 `skills/training-theory/references/progression.md`；本集路径有 `backend/` 前缀，转工具路径时以实际工具工作目录为准。

负例的 `should_use_skill=false` 仅要求无需读取该训练知识库。业务工具可正常使用；含健身关键词不构成必须加载知识的依据。无关读取、错误回答和业务工具行为分别记录。

## 参考答案审核

知识库当前正文属于候选依据，不能充当唯一独立真值。来源登记中已有的“已核对”状态属于仓库历史记载，本次未重新核对外部记录或论文全文。研究人群、测法、比较组和结局范围均需在正式评估前人工独立核实，安全题需合适的专业复核。发现充分支持的其他有效安排时应接受相应语义答案，不能将未充分支持的科学结论设成唯一满分要求。

审核可直接打开JSONL逐题检查：输入是否自包含、是否合成、是否自然触发、要求能否由可核查依据支持、严重错误边界是否合理、替代答案是否成立。无需启动模型。参考来源、审核者与审核决定需要真实记录，禁止补造批准。该独立集尚无接入现有批准摘要机制的实现；任何发布或现有门禁变更均需另行决定。

## 当前项目接口与缺口

核对入口：`backend/evaluation/dataset.py`、`backend/evaluation/langsmith_scenarios.py`、`backend/dataset/evaluation/schemas/scenarios.schema.json`、`optimization/retrieval-evaluation-guide.md` 第1、9、11节、`optimization/evaluation-contracts.md` 13.7。

现有 `load_for_review()` / `load_for_evaluation()` 面向带manifest、corpus、queries、qrels、scenarios、Schema身份与来源的完整业务数据版本。`langsmith_scenarios sync/run --dataset` 会走正式加载门禁；run的validation用途同样先加载正式数据。正式门禁要求逐条用户批准、内容摘要、来源引用、发布批准和全量配额。未知版本采用80个scenario、150个query的默认基线；`rebuild-v3`固定40场景、150个query。

本集没有现有scenario所需的input/initial_state/events/checkpoint/reference/rubric/assertions/metric_applicability结构，也没有完整业务版本的其他文件。现有 `evaluation.langsmith_scenarios --dataset` 无法直接消费本集。不得用复制空文件、补造审批或调整默认配额使其通过。

项目已有业务场景真实生成、检索、评分及外部裁判投影能力。本30题尚未接入现成loader、执行runner、记录投影、LangSmith上传适配或训练知识专业裁判；当前入口未提供本集所需的Skill开/关A/B配置开关。本次交付不实现这些缺口，无本集模型运行CLI。正式运行需获授权并完成相应实现与审核。

现有 `external-review`、`external-project` 等离线入口只消费其协议下的真实场景执行证据，本集JSONL不能直接替代这些证据。

## 记录与裁判字段映射

以下为后续适配契约说明，当前未实现转换器：

1. `prompt` → `inputs.user_request.message`；`context.content` → 模型实际获得的合成背景。保留case ID与背景注入位置，避免把同一背景重复注入。不存在的历史消息不补造。
2. 真实可见回答 → `outputs.answer.visible_text`；当次真实 `read` 返回正文及其他模型实际可见的必要事实 → `outputs.context.items`。需保留工具调用身份、路径、读取范围与成功/错误状态供审计。实际上下文只能包含模型确实获得的正文；未读取的参考文件正文不得补入。参考文件登记本身不算读取证据。
3. `expected` → `reference_outputs.reference.must_include`；`critical_errors` → `.forbidden_claims`；`allowed_variation` → `.allowed_variation`；`reference_answer_points` 作为参考回答要点；`reference_sources`作为评测侧溯源材料。这些字段需适配现有目标Schema，逐项语义判定，无须复述参考原句。
4. 裁判输出与人工决定 → 逐项结果，保留case ID、要求项、通过/失败/无法判定、理由和证据定位。参考批准状态、指标适用性和裁判配置单独真实记录，不伪造分数。

13.7的 `evidence_supported_v1` 只检查动作名称、器械、目标肌群、重量口径、步骤与用户已有事实，忽略一般训练建议与计划安排。它不能原样衡量本集全部训练理论专业性；例如一个回答的动作事实全部有依据，同时仍可能作出错误耐力推论。该指标适用性须逐题审核，纯概念题不能因缺少受评事实而充当专业性通过证据。

`reference_requirements_met_v1` 的must_include/forbidden_claims/allowed_variation可映射本集要求，但需语义判断。危害性与专业正确性还依赖独立核实参考、适用条件与人工复核；两项现成Question均无法取代这些工作。后续外部裁判仍须满足实际执行证据与适用性批准门槛。

## 可执行的纯离线校验

已有后端环境需包含项目已声明的 `jsonlines`、`jsonschema` 和pytest。本命令不安装依赖，禁止联网；环境缺包时应如实报告阻塞：

```powershell
uv run --locked --offline --no-sync --directory backend python -m pytest dataset/skills-evaluation/v1/test_cases.py -q --basetemp=temp/skills-evaluation-v1
```

从仓库根执行，测试路径相对backend。超时上限300秒；测试实际运行由父会话派发专用subagent。本次编写者未运行该测试，执行结果由独立测试回报记录。

同目录 `test_cases.py` 使用jsonlines读取JSONL、Draft 2020-12 jsonschema检查每行类型与必要字段，同时检查精确30题、24/6分布、稳定ID、草案状态、明确合成背景、prompt无路径指令、15主题覆盖、本地来源文件存在及三个回归标签。Schema内嵌于测试，免增独立Schema文件。它不验证论文真实性、语义正确性、模型行为、实际按需读取、得分或审批。

## 建议记录和报告口径（尚未执行）

- 每配置每轮训练任务通过率分母24；负例误触发率分母6。另报负例文本任务通过率与训练任务严重错误数。无法判定、失败和未完成均单独列明，不缩小完整运行分母，不将30题混成总调用率。
- 令TP为24个正例中实际读取训练知识库的题数，FN=24−TP，FP为6个负例中的读取数。触发precision=TP/(TP+FP)，recall=TP/24；precision无分母时记不可计算。按案例是否发生过成功读取计数，同时记录尝试与失败读取；同一题多次调用不重复计TP。入口索引读取计知识库触发，主题正文成功读取另算主题加载。
- 逐题记录任务要求满足情况、严重错误、实际读取的入口与主题、相关性、是否存在无关主题加载、工具次数、输入输出token、费用和端到端耗时。主题命中结合 `topic_paths` 和人工认可的等价依据判定，无关内容、漏读与未使用事实分别审计；不要求每题全读references。费用缺账单或费率依据时保持未知。
- 对照设计可用同模型、系统提示、合成背景、工具集和运行参数，记录知识库入口、可发现性和权限差异。Skill关闭基线不得通过初始输入、缓存或工具预填暗中取得知识库全文。当前缺少此A/B开关，不能宣称已经运行对照。
- 建议每配置重复3次：30×3=90次；两配置共180次。该数量属于设计，无已执行成绩。逐配置按轮报告24/6分母；三轮合计分别为72个正例和18个负例观察，并保留同题重复的相关性。
- 记录数据文件摘要、代码版本、模型与提示身份、配置、运行时间及真实工具轨迹。报告配对逐题差异、类别和主题覆盖，必要时给出适当不确定性区间。30题及主题内的小样本无法证明微小提升，不设置无依据的发布阈值。
- 本v1内容已公开用于开发；需要独立保留题防止针对题目过拟合。保留题应隔离访问与调参，单独版本和审核，本次未创建或冒充独立holdout。
