# rebuild-v1 取消评测契约修订记录

## 用户授权

授权来源：本次对话中的用户指令。

用户明确授权：

- 直接把取消成功检查放到实际发送取消指令的消息回合。
- 调整相关状态断言并重新审批。
- 直接修改现有 `rebuild-v1`，覆盖原冻结内容。
- 旧测试结果不参与新 baseline。
- 保留 `rebuild-v1` 名称与目录。

批准范围限定为取消时点、相关状态断言及必要配套实现。批准基于用户的明确授权，本次对话中不存在额外的人工逐条审阅过程。

## 修订范围

只修改以下三个场景的业务正文、检查点映射与审批摘要：

- `s-confirmation-cancellation-state-05`
- `s-confirmation-cancellation-state-07`
- `s-confirmation-cancellation-state-08`

corpus、queries、qrels、附件及其余 77 个场景保持原有字节内容。共享 Schema 未修改。

## 检查点映射

### s-confirmation-cancellation-state-05

- `cp1` / `e01`：准备计划快照，检查快照完整性、保存授权与未保存状态。
- `cp2` / `e02`：真实取消消息回合。检查本回合 `pending→invalidated`、真实成功的取消工具证据、已保存数据不变、快照完整性、保存授权与未保存状态。
- `cp3` / `e03`：取消后真实边界。检查 `invalidated→invalidated`、该事件自身的前后投影、真实取消证据与未保存状态。

### s-confirmation-cancellation-state-07

- `cp1` / `e01`：准备计划快照。
- `cp2` / `e02`：真实取消消息回合。检查本回合 `pending→invalidated` 与真实取消证据。
- `cp3` / `e03`：取消后真实边界。检查 `invalidated→invalidated`、该事件自身的前后投影与真实取消证据。
- `cp4` / `e04`：重复取消消息。检查完整业务投影不变与未保存状态。

### s-confirmation-cancellation-state-08

- `cp1` / `e01`：准备计划快照。
- `cp2` / `e02`：真实取消消息回合。检查本回合 `pending→invalidated` 与真实取消证据。
- `cp3` / `e03`：取消后真实边界。检查 `invalidated→invalidated`、该事件自身的前后投影与真实取消证据。
- `cp4` / `e05`：重启后的问答检查。检查未保存状态与画像未变。

## 断言语义

- 消息检查点的失效迁移目标来自场景中明确的后续 `cancel` 事件目标，并由真实 `proposal_ref` 绑定到实际提案。
- 成功取消证据必须匹配当前会话、助手运行、请求节点、来源助手节点、工具结果节点、工具调用 ID 与目标提案。
- 证据缺失、目标不匹配、证据重复或身份歧义时，检查失败或场景阻断。
- 消息检查点使用本消息真实的前后投影；后置 `cancel` 检查点使用该事件自身真实的前后投影，不借用驱动事件的 `before`。
- `saved_data_unchanged` 只比较已保存业务内容；`state_unchanged` 比较完整业务投影。
- 纯取消状态检查点保留生成指标 `not_applicable` 与准确原因。

## 数据身份

- 场景文件：`backend/dataset/evaluation/rebuild-v1/scenarios.jsonl`
- 场景文件 SHA-256：`6ed828f8685b2ef87c33849ae5793f39c6cd0ee264f4547fcdb848555f7fc379`
- manifest 文件 SHA-256：`f63a9efa2beca168647758c7ef8d14bf6f3bc4e4dd18665fd07213e3828c920b`
- release 内容摘要：`0b67ac8477536f5f3e6e8360adc2302808ee360e2ba39f71c9e015a34478c32b`
- 冻结时间：`2026-10-19T12:03:15+00:00`

逐场景审批内容摘要：

- `s-confirmation-cancellation-state-05`：`339c954ae3524e7236425313700d317536d9f1807b2d4ee82627d0aa41a7ae0b`
- `s-confirmation-cancellation-state-07`：`e0dfbaaff1247dee181749cf1d8542b197f6192a999f906fbe76aae880977954`
- `s-confirmation-cancellation-state-08`：`743b9410a4fcb57d04d55c81a49dcc63766f3f53b2e951d9456ef322f7ec2655`

数据规模保持为 corpus 1321、queries 150、qrels 1079、scenarios 80、附件 8。执行计划为 80 场景、134 事件、127 检查点、254 个生成指标条目。

## 旧结果与缓存隔离

- 旧运行结果保留原有 `dataset.content_sha256` 与 `manifest_sha256`，不修改为本次身份。
- 新 baseline 只接受本次 release 摘要与 manifest 身份。
- 检索查询向量缓存继续按现有完整身份规则核验；语料与查询文件未变化，缓存身份不因本次修订被伪装成新缓存。
- 历史运行目录与缓存不自动删除。
