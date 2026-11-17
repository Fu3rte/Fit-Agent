# rebuild-v5 冻结候选池补标委托记录

## 用户授权

本轮由用户显式授权通过父会话委托 Worker 补齐 `text-embedding-v4` 切换与语料 `2026-10-26.3` 下四组检索候选池缺失的 qrels，并派生发布 `backend/dataset/evaluation/rebuild-v5/` 及上传 Langfuse Cloud。

委托确认方式：当前任务会话中经父会话转交的用户显式授权。授权范围为补齐冻结运行 `20261117T013337Z-963c72ed` 四组 Top20 候选池汇集的 `413` 个缺失 `(query_id, corpus_id)` 相关性判定，以及创建 `rebuild-v5`、离线复评验证、测试、生成 `rebuild-v5` 查询向量缓存并上传 Langfuse Cloud。实际标注者为 **Worker**；新增 qrel、受影响 query 和 release 的 `reviewer="用户（委托Worker标注）"`、`reviewer_type="user"` 表示该委托批准，comment 明确实际逐条语义判断人为 Worker。

## 判断规则和依据

继承 `backend/dataset/evaluation/schemas/qrels.schema.json` 与 `backend/evaluation/retrieval_rescore.py:GRADE_RULES` 的 0–3 标准：

- `3`：高度相关——动作与查询目标、条件及用途直接一致。
- `2`：相关——动作满足查询意图，存在非关键差异。
- `1`：弱相关——仅部分满足查询意图或条件。
- `0`：已审阅且不相关——动作不能满足查询意图或条件。

尺度依据各查询原正文、用途、`conditions`（包含环境默认与已保存限制）、`backend/dataset/actions.agent.json` 的实际器械/部位/目标肌群/完整步骤，以及 `backend/dataset/action-supplements.published.json` 的已批准英文名/别名，并沿用 `rebuild-v4` 同家族查询已批准口径。排名、RRF 分数、重排分数未参与等级决定。

目录来源保持以下冻结 SHA-256（与 `rebuild-v4` 一致，语料版本 `2026-10-26.3`）：

- `backend/dataset/actions.agent.json`：`e6c3781611f4b9cda4b1646445f934200189d33f227c7a46c2b46890ce1bec26`
- `backend/dataset/action-supplements.published.json`：`ea568653cdc9ab3eac8d0a7dd440a236f8860e054537f16f28a5714565988e46`
- `backend/dataset/action-field-mappings.json`：`67c1192f3c32040910cf2f48f62d92e198bcc9c58f19314efe48de7bd08f3b67`

## 版本与审阅范围

- 派生版本：`backend/dataset/evaluation/rebuild-v5/`；父版本：`rebuild-v4`。
- 继承 `corpus` `1321` 条、`queries` `150` 条、原 `qrels` `3266` 条、`scenarios` `40` 条、`assets` `8` 个。
- 追加 `413` 条 qrels，涉及 `86` 条查询；总 qrels `3679` 条。
- 新增等级分布：`0` 分 `303`、`1` 分 `84`、`2` 分 `26`、`3` 分 `0`。
- `corpus.jsonl`、`scenarios.jsonl` 与 `assets/` 从 `rebuild-v4` 逐字节继承；原 `3266` 条 `qrels.jsonl` 字节作为新文件前缀保留。
- 查询业务正文、条件、用途、`family`、`tags`、`scenario_id` 与 `expect_empty` 原值保留；仅更新受影响 `86` 条查询的 `judgement_scope` 和 `review` 摘要。
- `backend/evaluation/dataset.py` 在 `SCENARIO_BASELINES` 登记 `"rebuild-v5"` 沿用 40 场景基线。

## 事实限制的具体处理

- `0325`（哑铃上斜上举）：实际步骤为上斜凳肩高向过顶推举（`target=delts`），`q-lookup-006` 给 `1` 分，`q-lookup-025` 给 `0` 分。
- `0316`（哑铃上斜夹胸）：实际步骤为 45 度上斜凳屈肘降至胸两侧后推回，上斜卧推核对 `q-lookup-032` 给 `1` 分（中文名称为夹胸），上斜胸推安排 `q-generation-017` 给 `2` 分。
- `3292`（电梯式卷腹）：实际步骤为站姿体前屈上背动作（`body_part=back`、`target=upper back`），`q-generation-035` 给 `0` 分。
- `9001`（高位引体向上）与 `0466`/`1430`/`3288`：需要引体杆或双杠设施，居家无设施或明确排除引体的查询（`q-generation-001`、`q-generation-026`、`q-replacement-026`）均给 `0` 分；引体握法/变式查询（`q-lookup-028`、`q-lookup-044`、`q-replacement-002`、`q-replacement-020`）中 `9001` 给 `1` 分。
- `1271`（弹力带绕肩拉伸）：`equipment=body weight` 且步骤为徒手站姿胸肩伸展，在 `q-generation-013` 与 `q-replacement-026` 均给 `1` 分辅助分。

## 离线复评源

- 冻结源：`backend/temp/retrieval-mteb/20261117T013337Z-963c72ed/evaluation.json`（SHA-256 `3bc79ca4ddd7b1380afe348f7c8d731490e46cf3d3f49c6500dfdfae6ed94213`）。
