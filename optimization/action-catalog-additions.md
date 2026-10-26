# 动作目录增补记录

## 街健动作补充

### 用户授权

用户在本会话说明：主练街头健身，使用产品时会逐步补充街健动作，当前最缺街健动作。审阅新增条目内容后答复「没问题，不用开摆荡接力版本」，授权写入目录并重签配套语料与评估数据集。

### 新增条目

| 编号 | 名称 | 英文名 | body_part | equipment | target | muscle_group |
|---|---|---|---|---|---|---|
| 9001 | 高位引体向上 | high pull-up | back | body weight | lats | lats |

- 动作定义：从静止悬垂起，以爆发力拉过下巴过杠，继续上拉让杠从上胸向下胸、下肋骨、上腹、腰际下移，终点为当日能控制的最高接触点；全程为纯拉，肩膀不上杠。
- 与既有条目的分界：`0652 引体向上` 止于下巴过杠；`0466 吉隆达胸骨引体向上` 大幅后仰、手肘在杠后；`0631 双力臂` 含过杠转换。目录此前没有「拉高但不转换」的条目。
- `muscle_group` 记为 `lats`（背阔肌），按用户要求让肌群字段落在背阔侧；同目录其余 22 条引体沿上游数据记为 `biceps`，本条为本规范确立后的写法。
- 别名：`高位引体`（zh）、`high pull up`（en），family 归 `pull-up`。
- 来源记为 `local:fit-agent/street-workout@2026-10-26`，无上游对应条目。

### 编号保留段

`9001` 起为本地新增动作的保留编号段，与上游数据 ID（当前最大 5201）分开，后续街健动作依次递增。

### 本次不含的内容

不含摆荡借力版本（kipping high pull-up）。严格版与借力版发力机制不同，需要时另开条目。

### 级联重签范围

目录由 1320 条增至 1321 条，以下内容按新内容重签：

- `action-supplements.candidates.json`、`action-supplements.published.json`：追加条目，catalog 哈希与数量同步，发布版本 `2026-10-26.3`。
- `action-field-mappings.json`、`action-retrieval-queries.json`：仅更新 catalog 哈希与数量，字段词表与标注不变。
- `action-supplement-reviews.json`：追加目录级决定，绑定新目录哈希。
- 评估数据集 `rebuild-v1`~`rebuild-v4`：corpus 增 1 行并按新来源重建，qrels 与查询标注不变，manifest 计数、文件哈希与发布摘要重签。
- 检索索引 `fit_agent_actions_current` 需重建一次。

新增条目未进入任何既有查询的相关性标注，qrels 等级未受影响。
