# 动作检索 T2：语料与评测数据报告

依据 [实现顺序](action-retrieval-implementation-order.md) 的 T2、[检索契约](action-retrieval-contract.md) 与 [设计方案](action-retrieval-plan.md)。本任务只补充检索语料、评测数据、编码规则、长度统计与名称检索基线，未实现 Milvus、Embedding API、业务工具或前端。

## 1. 新增文件与职责

| 文件 | 职责 |
| --- | --- |
| `backend/dataset/action-supplements.candidates.json` | 补充语料候选：1324 条动作的英文名称与中文俗称，含来源、审阅状态、别名家族 |
| `backend/dataset/action-supplements.published.json` | 发布语料：候选经人工审阅通过后的 approved 内容，供 T5 建索引 |
| `backend/dataset/action-supplements.schema.json` | 补充语料结构约束 |
| `backend/dataset/action-field-mappings.json` | canonical 字段到中文显示/查询映射，含同义词组 |
| `backend/dataset/action-field-mappings.schema.json` | 字段映射结构约束 |
| `backend/dataset/action-supplement-reviews.json` | 人工审阅账本：候选、通过、拒绝分开记录 |
| `backend/dataset/action-retrieval-queries.json` | 128 条评测查询与相关性标注、开发/留出拆分 |
| `backend/dataset/action-retrieval-queries.schema.json` | 评测查询结构约束 |
| `backend/app/application/business/retrieval_text.py` | 统一检索文本编码辅助模块（字段顺序、规范化、字段映射、来源记录） |
| `backend/test/check_retrieval_supplements.py` | 补充数据、映射、查询、审阅账本与统一文本的自动校验 |
| `backend/test/check_retrieval_text.py` | 规范化与编码逻辑的真实小测试 |
| `backend/test/check_retrieval_baseline.py` | 当前名称检索基线、分类指标与长度统计 |

证据与构建脚本位于已忽略的 `tmp/action-retrieval-t2/`（含 `evidence/` 下四条命令的原始输出）。本任务未修改 `backend/dataset/actions.agent.json`、其事实 Schema、业务服务、工具注册、HTTP、Trace、前端、依赖清单或 lock 文件。

## 2. 数据来源与对齐核验

英文名称取自上游 MIT 许可数据集 `hasaneyldrm/exercises-dataset`（本地副本 `D:/repository/exercises-dataset`，`data/exercises.json`，commit `7455efae41b330c265e7cd4b78dfa848e7ce5ebd`）。该仓库的数据层（名称、器械、肌群、说明文本）为 MIT，媒体（images/videos）为 © Gym visual，本任务只取数据层字段 `name`。

程序化对齐核验结果：上游与本目录 ID 集合一致、顺序一致，1324 条；`equipment`、`target`、`muscle_group`、`body_part`、`secondary_muscles`、`steps.zh`、`steps.en` 七项逐条比较零不一致。目录 SHA-256：`4860919c855e343130a1b13a53e4abcc32ca9f8cecf4c0ffbc55412fb51870ee`。

## 3. 补充语料与审阅状态

按 body_part 分 8 个板块派发 subagent 生成中文俗称候选（每个 subagent 写入独立片段文件，再统一整合），覆盖全部 1324 条动作。下表按条目内别名来源标记统计（无补充条目无来源标记，单列）：

| 板块 | 有俗称动作 | 别名映射 |
| --- | --- | --- |
| back | 201 | 220 |
| chest | 153 | 232 |
| shoulders | 140 | 398 |
| upper-arms-a | 146 | 293 |
| upper-arms-b | 145 | 145 |
| upper-legs | 219 | 291 |
| waist | 163 | 272 |
| lower-cardio | 127 | 276 |

整合后：条目 1324，有俗称 1294，无补充 30，别名映射 2127（唯一别名文本 2021，一对多 90 条文本），别名家族 254。生成时过滤了与动作中文名或英文名规范化相同的别名，避免以重复名称制造词频权重。

审阅状态：候选文件 `review_status=candidate`，来源分别为 `upstream:...`（英文名称）与 `subagent:<板块>`（俗称）。审阅账本 `status=approved`，`reviewer=本会话用户`；文件级与语料决定的 `reviewed_at=1791940096`，查询决定的 `reviewed_at=1791944832`。两条整份结论分别覆盖补充语料与评测查询；评测查询结论在本轮按审阅人反馈修正 q-exact-003 与 q-cond-008/009/012 后重新绑定内容哈希（账本 note 记录该轮修正）。发布语料 `action-supplements.published.json` 已按通过结论生成（1324 条，条目与别名全部 `approved`）。歧义俗称（如“卧推”“划船”“硬拉”“飞鸟”）以同一文本出现在多个变式条目保留，未合并宽握/窄握/单臂/跪姿/器械差异。

## 4. 统一文本编码规则

模块 `app/application/business/retrieval_text.py`，当前规则版本 `retrieval-text-2026-10-14.2`，字段顺序 `name, aliases, english_name, equipment, body_part, target, muscle_group, steps_zh, steps_en`，空字段跳过，段间分隔符 `\n`。默认读取发布语料 `action-supplements.published.json`（索引只用已审阅内容），候选语料常量单独保留供审计与候选评估。

规范化：`unicodedata.normalize("NFKC")` 兼容归一 → 空白折叠为单空格 → `casefold()` 英文大小写折叠；casefold 对中日韩字符为空操作。文档与查询共用同一函数。规范化只作用于检索输入，原始 ID、事实名称与详情步骤保持原样，ID 保持四位零填充字符串。

字段映射段输出 `{canonical} {zh}`（例如 `body weight 徒手`、`back 背部`）。别名按文本排序后确定性拼接；英文名称与别名默认只纳入 `review_status=approved` 的内容；`include_unreviewed=True` 显式允许未审阅内容进入候选评估文本。`provenance()` 返回编码版本、字段顺序、分隔符、目录哈希、映射版本与补充数据版本，供 T5 复现同一文本。

## 5. 字段中文映射

覆盖四个字段的全部 canonical 值：`equipment` 28、`body_part` 10、`target` 19、`muscle_group` 29，与目录枚举逐一相等。`哑铃`→`dumbbell`、`背部`→`back` 由查询词表与显示映射确认。共享查询词组声明 `trapezius/traps`、`latissimus dorsi/lats`、`leverage machine/sled machine`。`query_canonical()` 返回全部命中的 canonical 集合；器械组用于共享“器械/固定器械”泛称，具体器械身份保持独立。仅声明组允许规范化词碰撞；其余规范化碰撞（`cable` 与 `rope` 的中文、`shoulders` 与 `deltoids` 的查询词）已消除。

## 6. 评测查询与相关性标注

128 条查询，按文件 `intent` 字段逐类覆盖：`standard_name` 20、`alias` 26、`english` 15、`mixed` 10、`description` 13、`conditional` 16、`multi_alias` 8、`forbidden` 8、`no_answer` 6、`exact_restricted` 6。q-noanswer-001/006 的 intent 分别为 description/alias，均为 `expect_empty=false` 且带相关性标注。`expect_empty=true` 共 10 条：q-exact-001/002/004/006 与 q-noanswer-002/003/004/005/007/008。标注 405 条，等级 3/2/1 定义见文件 `grade_scale`。

每条查询含稳定 ID、原始 query、意图、用途、显式筛选、环境与器械声明、临时限制、已保存画像限制、候选标注与标注状态、开发/留出拆分、家族键、基线输入与说明。包含“家里有哑铃，想练背”、缺哑铃后的替换、只用哑铃、名称完全一致但动作受限等用例。

器械条件语义（本轮按审阅反馈修正）：

- q-exact-003「引体向上」：已保存画像仅排除徒手（body weight）；辅助（leverage machine/band）与负重（weighted）变式未被排除，仍有合理候选，标注 7 条（辅助引体向上 0017、弹力带辅助引体向上 0970 等）并置 `expect_empty=false`。q-exact-001/002/004/006 继续覆盖“名称一致但受限后无合理候选”的空结果场景。
- q-cond-008/009/012：用户表达为器械可用声明（「有杠铃」「家里有弹力带」「有壶铃」），按契约 §4.2 声明不等价于“只使用该器械”的筛选，`filters.equipment` 全部置空，保留部位筛选与声明字段。q-cond-009 显式居家环境，徒手属于有效可用集合，补徒手背部候选 5 条（0652、3158、3166、3168、3019）；q-cond-008/012 环境未知，默认目录器械可用，标注以声明器械动作为核心。
- 契约 §4.2 的“只用某器械”独立筛选语义由 q-cond-003「只用哑铃练胸」覆盖。

拆分：开发集 61 条、留出集 67 条、家族 22 个。校验确认同一家族不跨集。标注状态经人工审阅后为 `approved`，账本以内容哈希绑定当前 405 条标注。

## 7. 长度统计与预算依据

口径：`json.dumps(..., ensure_ascii=False)` 实际序列化字符数用于工具字符预算；UTF-8 字节数用于索引容量（中文字节数大于字符数）。估算 tokens 复用 `app.agent.budget.CHARS_PER_TOKEN=3.5` 向上取整，属字符估算，不等同真实分词器计数。1024 维向量不返回模型，未计入。统一文本按发布语料（含已审阅别名）统计。

| 指标 | 样本 | P50 | P95 | 最大 | 最长样本 |
| --- | --- | --- | --- | --- | --- |
| 统一检索文本字符 | 1324 | 671 | 888 | 1170 | 0551, 0776, 0028 |
| 统一检索文本 UTF-8 字节 | 1324 | 931 | 1237 | 1604 | 0776, 0551, 1434 |
| 搜索前 10 条投影字符（非空） | 51 | 655 | 835 | 850 | q-alias-017 |
| 九字段详情字符 | 1324 | 847 | 1073 | 1394 | 0776, 0551, 0028 |
| 九字段详情 UTF-8 字节最大 | — | — | — | 1832 | 0776 |
| 中文步骤字符 | 1324 | 115 | 160 | 224 | 0776, 1434 |
| 英文步骤字符 | 1324 | 456 | 624 | 865 | 0776, 0551 |

搜索投影：128 条查询中 77 条空结果（原始 query 名称子串无法命中，见 §8），空结果单独计数不混入分位数；非空 51 条 P95 835、最大 850。

详情批次按目录顺序全量连续切批统计，另以单条最长的组合给出任意一批的体积上界（longest_combo）：

| 批量 | 连续批次 P50 | P95 | 最大 | 字节最大 | 任意组合上界（字符/字节） |
| --- | --- | --- | --- | --- | --- |
| 5 条 | 4288 | 4882 | 5638 | 7350 | 6809 / 8877（0776, 0551, 0028, 1434, 0648） |
| 10 条 | 8540 | 9505 | 10572 | 13694 | 13284 / 17302 |

九字段详情估算 tokens：P95 307、最大 399。

建议：搜索简要候选总字符上限取 1500（非空 P95 835、最大 850，余量给元信息与后续查询提示）；单条完整详情上限取 2000 字符，覆盖最大 1394（字节 1832），无单条超限样本；详情批量默认 5 条，单次响应上限取 7000 字符，覆盖任意 5 条详情组合上界 6809；若采用 10 条批量，响应上限需不低于 13300 字符（组合上界 13284）。上述数值供 T6 参考，T6 必须用最终响应 Schema 复核后再写入契约。T5 的 Milvus 字符串容量按 UTF-8 字节口径设置（统一文本最大 1604 字节），字符数口径只用于工具返回预算。统一文本不进入工具返回上下文，其长度只影响索引与评测。

## 8. 当前名称检索基线

命令（目录 `backend`）：`uv run python -m test.check_retrieval_baseline`，退出码 0。真实使用 `Catalog.load` 与 `Catalog.search`，未加别名展开、翻译、BM25、dense、额外排序或画像过滤。主对照口径与查询文件 `baseline_rule` 一致：把每条查询的原始 `query` 文本作为 name 子串，`filters` 四项作为精确筛选；`baseline_name` 的人工名称改写仅记录在查询文件与逐例输出中，不进入主对照。

返回量：查询 128 条，总候选 1461，空结果 77 条，结果超过 10 条 25 条。名称子串机制无法命中自然语言表达，conditional/description/english/mixed/forbidden 类查询及无答案场景的原始 query 全部空结果，这是当前基线的真实能力边界，也是 T5/T6 语义检索的对照起点。分类指标（每个类别仅统计有标注样本；`label_status=approved`）：

| 类别 | 样本(有标注) | Recall@10 | MRR@10 | nDCG@10 | 无答案误返回 |
| --- | --- | --- | --- | --- | --- |
| standard_name | 20 | 0.450 | 0.935 | 0.668 | — |
| alias | 26 | 0.463 | 0.439 | 0.320 | — |
| multi_alias | 8 | 0.569 | 0.369 | 0.314 | — |
| description | 13 | 0.000 | 0.000 | 0.000 | — |
| english | 15 | 0.000 | 0.000 | 0.000 | — |
| mixed | 10 | 0.000 | 0.000 | 0.000 | — |
| conditional | 16 | 0.000 | 0.000 | 0.000 | — |
| forbidden | 8 | 0.000 | 0.000 | 0.000 | — |
| exact_restricted | 2 (共 6) | 0.857 | 0.750 | 0.808 | 0.250 |
| no_answer | 0 (共 6) | — | — | — | 0.000 |

exact_restricted 有标注样本为 q-exact-003（7 条标注，Recall 6/7）与 q-exact-005（1 条，Recall 1/1）；空结果场景 4 条中 q-exact-006「杠铃耸肩」因基线不执行临时排除而误返回，误返回率 1/4=0.250。no_answer 类 6 条全部合法空结果，误返回率 0。

指标公式：Recall@10 = |相关 ∩ 前10| / |相关集合|（多答案按实际相关集合）；MRR@10 = 首个相关项排名的倒数，无命中记 0；nDCG@10 = DCG@10 / IDCG@10，增益 `2^grade-1`、折扣 `log2(rank+1)`；无答案误返回率 = 该类别 `expect_empty` 查询中返回非空的比例。

上述指标基于已审阅标注。当前代码不支持的条件：环境默认、器械集合（如“哑铃+徒手”）、画像限制、显式排除动作；因此 exact_restricted 的画像排除与临时限制场景、forbidden 的排除能力均无法在基线层生效。基线为该提交快照，T8 必须在同一目标提交重测对照。

## 9. 执行验证

实现与业务测试由 subagent 在 `D:\repository\fit-agent\backend` 实际运行，退出码均由 subagent 回报：

| 命令 | 退出码 | 输出摘要 |
| --- | --- | --- |
| `uv run python -m test.check_retrieval_supplements` | 0 | 1324 动作、2127 别名映射（唯一文本 2021）、254 家族、90 条一对多；四字段映射全覆盖；查询 128 条、标注 405、家族 22、开发 61/留出 67、类别计数见 §6；正相关硬约束（器械筛选）全部满足；审阅账本 approved、账本与发布语料哈希绑定一致；统一文本最大 1170 字符，相同文本 3 组 |
| `uv run python -m test.check_retrieval_text` | 0 | 规范化、字段顺序、别名冲突与前导零检查通过 |
| `uv run python -m test.check_retrieval_baseline` | 0 | 目录 1324、查询 128、总候选 1461、空结果 77、超 10 条 25；分类指标与长度统计见 §7、§8 |
| `uv run ruff check <4 个文件>` | 0 | All checks passed |

`01_check_retrieval_supplements.txt` 输出包含正相关硬约束（器械筛选）与审阅账本内容哈希绑定两条检查说明；`03_check_retrieval_baseline.txt` 输出包含提交身份（commit `ebb1635`、工作区脏状态与改动路径清单）。

原始输出：`tmp/action-retrieval-t2/evidence/01_check_retrieval_supplements.txt`、`02_check_retrieval_text.txt`、`03_check_retrieval_baseline.txt`、`04_ruff_check.txt`。构建与取证脚本：`tmp/action-retrieval-t2/`（`build_supplements.py`、`build_queries.py`、`publish.py`、`check_fragment.py`、`prepare_agent_inputs.py`、`verify_alignment.py` 等）。

## 10. 消费方接口与公共契约同步事项

- T5：读取 `backend/dataset/action-supplements.published.json`（已审阅，别名全部 approved）、`action-field-mappings.json`；调用 `retrieval_text.build_unified_text`、`normalize`、`query_canonical`、`provenance`。Milvus 统一文本字符串容量按 UTF-8 字节口径设置：最大 1604 字节（对应字符 1170），由 T5 按真实服务能力留余量固定；详情不进入 Milvus，按 ID 读取原始 Catalog。
- T6：搜索返回结构与详情批量上限按 §7 建议取值（简要候选 1500 字符、单条详情 2000 字符、5 条批量响应 7000 字符），并用最终响应 Schema 复核；`expect_empty` 与 `false_return_rate` 反映无答案门槛需求。
- T8：以 §8 原始 query 主对照基线为对照，必须在同一目标提交重测；使用 `action-retrieval-queries.json` 的 dev/holdout 与家族拆分评估留出集。
- T3：本任务不消费其产物；`backend/pyproject.toml`、`backend/uv.lock`、`deploy/` 由 T3 维护，本任务未改动。
- 需由协调任务同步进公共契约的事项：统一文本字段顺序包含 `body_part`（契约 §3.2 原文为“器械及肌群映射”）；段分隔符 `\n`；规范化规则 NFKC + 空白折叠 + casefold；编码规则版本 `retrieval-text-2026-10-14.1`；补充数据/发布语料/审阅账本路径与“索引只读取发布语料”；§7 字符预算与详情批量建议及 UTF-8 字节容量口径（T6/T5 复核后定稿）。

## 11. 完成项与未执行验证

已完成：上游对齐核验、英文名称补充、按部位俗称候选与整合、字段中文映射、补充数据与查询 Schema、统一文本编码模块与规范化测试、128 条评测查询与候选标注及拆分、人工审阅记录与发布语料生成、长度统计与预算依据、当前名称检索基线与自动校验（退出码全 0）。本轮按审阅反馈修正 q-exact-003 与 q-cond-008/009/012 的器械筛选语义与相关性标注，审阅账本重新绑定内容哈希（405 条标注），并重取全部验证证据。

主会话复核：当前查询含 405 条标注；q-exact-003 有 7 条正相关标注，q-cond-008/009/012 的器械筛选为空且声明保留。查询、候选与发布文件的 SHA-256 与账本绑定一致。已读取四份运行输出；本轮未重新执行业务测试。

账本语料决定的 `coverage` 覆盖 1324 条动作、1324 个英文名称及 2127 条中文俗称映射，与当前候选语料数量一致。

未执行：留出集质量评测（属 T8，在本提交同一快照上以本基线对照）；Milvus、Embedding API、业务工具与端到端验证（属 T3–T8，本任务未启动）。
