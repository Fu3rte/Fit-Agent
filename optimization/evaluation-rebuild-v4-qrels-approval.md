# rebuild-v4 冻结候选池补标委托记录

## 用户授权

本轮用户原文：

> 你派个worker补齐答案吧，随便修改

随后补充：

> 授权所有改动

委托确认方式：当前对话中的上述两条用户文字授权，任务由父会话转交唯一实施 Worker。授权范围为补齐运行 `20261021T165402Z-320c0620` 四组冻结 Top20 候选池汇集的 2168 个缺失 `(query_id, corpus_id)` 相关性判定，以及必要派生版本、离线复评、测试与报告。实际标注者为 **Worker**。用户没有逐条亲自审阅本次标签；本记录保存用户委托批准与 Worker 逐条语义判断的各自来源。

沿用 `optimization/evaluation-rebuild-v3-scenarios-approval.md#用户授权` 的用户委托 Agent 选择并发布派生集合流程。新增 qrel、受影响 query 和 release 的 `reviewer=用户（委托Worker标注）`、`reviewer_type=user` 表示该委托批准；comment 明确实际判断人为 Worker。审批门禁保持原值。

## 判断规则和依据

继承 `backend/dataset/evaluation/schemas/qrels.schema.json` 与 `backend/evaluation/retrieval_rescore.py:GRADE_RULES` 的 0–3 标准：

- 3：动作与查询目标、条件及用途直接一致。
- 2：动作满足查询意图，存在非关键差异。
- 1：仅部分满足查询意图或条件。
- 0：已审阅且不能满足查询意图或条件。

按原查询正文、用途、显式器械/动作限制、环境和已保存限制，结合动作目录名称、器械、目标肌群、完整步骤，以及已批准补充事实的英文名/别名作判断。lookup 继承查询核实用途口径；generation/replacement 结合训练任务及替代模式。满足器械筛选条件只说明候选可考虑，等级仍取决于具体动作意图与模式。排名、RRF、重排分数未参与等级决定。

尺度参考：父版本各查询已有批准 qrels，以及 `optimization/evaluation-rebuild-v2-qrels-approval.md` 的 25 条补标。原标签保持完整原值。每项新增判定在 `rebuild-v4/qrels.jsonl` 保存原查询指向的针对性理由、目录 JSON Pointer 与完整事实摘录。逐条英文名/别名及冻结池出现记录保存在本轮 `tmp/retrieval-gold-completion/labels.jsonl`。

目录来源保持以下冻结 SHA-256：

- `backend/dataset/actions.agent.json`：`d3fd22334038a8a3803ce191acf28847003cb1057673dbbb0a470d153fa80b30`。
- `backend/dataset/action-supplements.published.json`：`9429aa3c7cfaf9e89c267843f5e6d1adfa55b075a347996c9c8db7c095e949ec`。

## 版本与审阅范围

- 派生版本：`backend/dataset/evaluation/rebuild-v4/`；父版本：`rebuild-v3`。
- 继承 corpus 1321、查询 150、原 qrels 1104、场景 40、附件 8。
- 追加 2168 条 qrels，涉及 112 条查询；总 qrels 3272。
- 新增等级分布：0 分 1402、1 分 594、2 分 171、3 分 1。
- 原 qrels 原始字节作为新文件前缀保留；corpus、scenarios、assets 逐字节继承。
- 查询业务正文、条件、用途、family、tags、scenario_id 与 expect_empty 原值保留；仅更新涉及查询的 judgement_scope 和 review 摘要。
- 新增标签仅覆盖本轮四组 Top20 候选池的汇集。未声明新增全语料穷尽审阅。继承父版本已批准范围，并逐查询明确本次追加范围。
- `rebuild-v4` 显式登记继承的 40 场景配额；历史及未知版本默认配额保持原值。默认运行版本保持原值。

## 事实限制的具体处理

以下判定均按具体查询独立作出，目录及补充事实保持原值：

- `q-lookup-038/0290`：肩部三角肌元数据有关，步骤描述后倾、从胸两侧向上推，坐姿过顶路径无法完全核实；1 分，保留局部家族关联。
- `q-replacement-007/0016`：名称为腘绳肌弯举，步骤要求保持膝盖伸直，无法确认所需抗阻屈膝腿弯举；0 分。
- `q-generation-013/1290`：步骤为坐球肩高向上推，与胸部元数据冲突；继承已批准补标中该动作的 0 分尺度。
- `q-generation-028/0075`：持同一杠铃同时向两侧直臂举起的步骤无法协调核实；0 分。
- `q-generation-013/0764`：卧推名称与胸部元数据有关，步骤为站姿胸前推杠，卧凳支撑无法核实；1 分。
- `q-lookup-028/0720`、`q-lookup-028/0466`：chin 名称有关，步骤记录正手握杆，无法确认反握身份；各 1 分。
- `0316`：步骤明确为上斜凳屈肘降至胸两侧并推回；`q-lookup-036` 仅给 1 分，胸推替代查询 `q-replacement-001` 与 `q-replacement-021` 各给 2 分。

## 离线复评边界

冻结源：`backend/temp/retrieval-mteb/20261021T165402Z-320c0620/evaluation.json`，4,964,002 字节，SHA-256 `da451330512ac8ffbb5bde2a6038f77eba053c5a3fe3d0c0bb35228a23921359`。

保留原 blocked、metrics=null、采集身份及历史 followup。利用已保存的 120 次真实重排结果离线复评；新增 Embedding、重排、生成、裁判、LangSmith、Milvus 与网络请求均要求为 0。当前重排额度已耗尽，未探测额度、重试服务或切换模型。本轮不验证真实服务可用性。

复评 Schema 保持本地离线边界；未伪造 release.effective、上传或云端回执。准确复评标识、指标、测试执行命令/退出码/日志、身份及字节校验记录于 `tmp/retrieval-gold-completion/report.md`。
