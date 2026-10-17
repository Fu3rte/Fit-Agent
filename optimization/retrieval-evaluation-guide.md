# LangSmith + ranx 检索评测操作

## 文件与职责

- `backend/evaluation/retrieval.py`：真实检索、历史重评分、LangSmith 上传入口。
- `backend/evaluation/retrieval_metrics.py`：金标和有序动作 ID 到 ranx 的适配。
- `backend/evaluation/retrieval_support.py`：复用目录、用途适配、条件、索引验证与向量缓存。
- `backend/test/test_retrieval_ranx.py`：验证数学例子、空返回、排序和输入边界。
- `quantitive-index/evaluation.md`：统一记录实际评测指标与实验身份。
- `backend/temp/retrieval-ranx/`：每轮原始逐例结果与 LangSmith 上传回执。

标准排序指标由 ranx 计算。数据适配和业务规则仍由项目代码执行，并保留必要测试。

## 1. 理解数据集和金标

打开 `backend/dataset/action-retrieval-queries.json`，查看一条开发集查询：

- `query` 是真实提交给检索模块的查询文本。
- `labels` 是人工审阅的动作 ID 及相关等级。
- `split` 区分开发集与历史留出集。
- `family` 将相近表达放在同一拆分，减少泄漏。
- `expect_empty` 表示预期无候选。

等级为 1、2、3 的动作都进入二元相关集合。nDCG 按等级区分弱相关、合理变式和核心匹配。

批准状态及内容哈希只能证明文件与批准记录一致。标注是否符合业务，需要人工复核。可以在 LangSmith 查看失分样例，并回到动作目录核实。

用途来自 `action-retrieval-purpose-adaptation.json`。其中暂缓的三条查询单独列出，不参与评分。入口不会自行解释尚未批准的条件。

## 2. 安装与本地核验

在 PowerShell 中进入后端目录：

```powershell
Set-Location D:/repository/fit-agent/backend
uv sync --locked
uv run python -m evaluation.retrieval --help
uv run python -m pytest test/test_retrieval_ranx.py -q
```

`uv.lock` 固定依赖版本。ranx 首次运行时会编译 Numba 代码，后续调用复用缓存。

数学边界测试采用已知金标与排名，直接调用真实 ranx。它们用于验证适配层，不代表业务检索质量。

## 3. 运行真实开发集评测

保证现有 Milvus 服务和 `backend/.env` 检索配置可用。入口复用已校验的真实查询向量缓存，不发起新的 Embedding 或聊天请求。

```powershell
$dev = uv run --env-file .env python -m evaluation.retrieval run --split dev | ConvertFrom-Json
$dev.groups
$dev.artifact
```

一次运行覆盖 BM25、dense、hybrid。三组消费相同查询、条件和最终响应预算。

结果包括：

- Hit、Recall、Precision、MRR、nDCG 的 @5 和 @10。
- 无答案误返回率。
- 按 intent 分类的指标。
- 每条查询的候选 ID、原分数、逐项得分和真实搜索时间。
- 硬约束、字符预算及索引、模型、代码身份。

有效结果自动追加到 `quantitive-index/evaluation.md`。缺失数据、目录外动作或门禁失败会立即报错。

当前耗时范围是“缓存向量搜索及响应构造”。它不包含在线 Embedding，也不等于完整 Agent 响应时间。

## 4. 配置 LangSmith

登录 LangSmith，在设置中创建 API Key，将其保存到已被 gitignore 排除的 `backend/.env`，变量名为 `LANGSMITH_API_KEY`。

使用对应区域或自建服务时，配置正确的 `LANGSMITH_ENDPOINT`。工作区要求时配置 `LANGSMITH_WORKSPACE_ID`。密钥保持本地，不粘贴到聊天、报告或版本库。

本次授权上传范围为现有评测查询、合成条件、金标和候选 ID。上传入口按字段白名单构造内容，不上传真实个人画像、缓存向量、服务凭据或查询端点配置。

上传不会开启生产 Trace，也不会改变原有 Trace 隐私契约。

## 5. 上传实验并在界面分析

沿用第 3 步产生的 `$dev`：

```powershell
uv run --env-file .env python -m evaluation.retrieval upload --source $dev.artifact
```

上传采用 LangSmith 官方外部实验接口。每个检索组成为一个 experiment，共用同一版本的数据集。

上传反馈分数按接口要求保留 4 位小数；本地原始 ranx 指标保持完整精度。

入口按查询集与适配表哈希生成数据集名称。每条查询的行 ID 稳定，便于实验对照。

上传回执记录在实验目录，实验与数据集 ID 追加到 `quantitive-index/evaluation.md`。再次执行时，入口核对已有回执的源文件哈希及远端实验身份，仅提交尚未上传的组。

各实验使用本组真实查询的最早开始时间和最晚结束时间，逐查询的原始耗时保持不变。

在 LangSmith 的 Datasets & Experiments 中：

1. 打开 `fit-agent-retrieval-dev-` 开头的数据集。
2. 选择本轮 BM25、dense、hybrid 实验。
3. 查看平均得分及每条样例的输入和候选。
4. 筛选 `hit@5=0`，检查遗漏的目标动作。
5. 查看低 nDCG 样例，检查弱相关动作的排名。
6. 查看 `false_return=1`，检查无答案误返回。
7. 按 intent 和 family 定位集中退化的请求。

无答案样例只附加误返回评分，不填充不存在的排序分数。排名指标的汇总分母仅包含有金标样例。

## 6. 比较两轮配置

开发集允许通过参数明确指定 hybrid 权重与召回深度。以下实验比较 alpha=0 与当前默认配置：

```powershell
$other = uv run --env-file .env python -m evaluation.retrieval run --split dev --alpha 0 --depth 100 | ConvertFrom-Json
uv run --env-file .env python -m evaluation.retrieval upload --source $other.artifact
```

两轮配置的结果均记录在统一指标文件。在 LangSmith 选择对应实验对照：

- Recall 提升覆盖了哪些新动作？
- MRR 提升是否来自核心查询？
- nDCG 变化是否反映更合理的变式顺序？
- 哪些 query 的得分下降？
- 无答案误返回和硬约束是否变化？

同一指标必须保持相同金标、相关性规则、k 和响应预算。当前查询量有限，小幅差异需要逐例与统计证据支持。

参数选择由人工基于用途判断。入口不自动决定上线参数，也不改写 runtime 配置。

## 7. 冻结配置与历史留出回归

选定开发集实验后，以该实验原件绑定参数、代码、数据和索引身份。比如选定 `$dev` 时：

```powershell
$holdout = uv run --env-file .env python -m evaluation.retrieval run --split holdout --frozen $dev.artifact | ConvertFrom-Json
uv run --env-file .env python -m evaluation.retrieval upload --source $holdout.artifact
```

留出运行要求当前代码、数据与索引身份保持一致，不接受参数覆盖。入口使用开发集证据中的参数。

历史留出集已经参与过分析，记录标记为 `previously_observed_holdout_regression`。严格独立验收需要额外保密样本及审阅授权。

## 8. 用 ranx 重算历史候选

历史候选可以用于学习和验证迁移口径，不需要运行 Milvus 或编码模型：

```powershell
$history = uv run python -m evaluation.retrieval rescore --source temp/retrieval-evaluation/20261017T032643Z-10e40f95/compare.json | ConvertFrom-Json
$history.groups
uv run --env-file .env python -m evaluation.retrieval upload --source $history.artifact
```

结果标记为 `historical_rescore`。入口仅使用历史返回 ID，重新交给 ranx 评分，不采用历史分数字段。

历史证据缺少逐查询检索耗时。上传时间范围表示本次批量重评分，LangSmith 展示的这类延迟不能用于评价检索速度。

## 评分口径核对

| 报告指标 | ranx 名称 | 关键边界 |
|---|---|---|
| Hit@k | hit_rate@k | 至少命中一个相关 ID |
| Recall@k | recall@k | 分母为已标注相关 ID 数 |
| Precision@k | precision@k | 固定分母 k，短返回保留空位 |
| MRR@k | mrr@k | 第一个相关动作的倒数排名 |
| nDCG@k | ndcg_burges@k | 增益为 2^grade−1 |
| 无答案误返回率 | 项目布尔规则 | 非空返回记 1，再按无答案样例平均 |

自定义布尔业务规则可由人工直接核对。平台和指标库不会自动消除金标遗漏、用途偏差或数据泄漏。

## 官方依据

- [ranx 指标定义](https://amenra.github.io/ranx/metrics/)
- [LangSmith 外部实验上传与对照](https://docs.langchain.com/langsmith/upload-existing-experiments)
