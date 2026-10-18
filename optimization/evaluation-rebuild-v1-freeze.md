# rebuild-v1 数据冻结记录

## 用户授权

用户在当前会话明确授权：

> 没事，可以直接冻结数据了。ragas这些我已经应用到代码里了，你直接冻结吧。

此授权用于批准并冻结当前 `backend/dataset/evaluation/rebuild-v1/` 的 corpus、150 条查询、1079 条 qrels、80 个业务场景、8 个附件及其来源、Schema、完整标注范围与发布清单。

批准记录使用 `reviewer_type=user`，由助手根据本条用户决定写入。此授权不表示真实检索、生成、业务断言或第三方服务调用已经验证通过。

## 冻结操作

只更新逐条 review、manifest 文件哈希及 release 批准记录；样例正文、相关等级、条件、参考答案、断言、语料、附件与 Schema 保持完整。

冻结时间为 `2026-10-18T12:03:51+00:00`。操作前备份与回执位于 `tmp/evaluation-freeze/receipt.json` 所记录的本地目录。

- 内容摘要：`634fd90d4a9326efb92129afb416daf061e5cad1466b0c7e049447886e96cef1`。
- manifest SHA-256：`dcb7ec660c2c22aed106bef1abfa525cbf33e17f6568daf3b4137fff0678fc3e`。

## 冻结验收

worker 实际执行 `powershell -NoProfile -ExecutionPolicy Bypass -File D:/Repository/Fit-Agent/tmp/evaluation-freeze/run_verification.ps1`，退出码 `0`。

真实 `load_for_evaluation` 通过，`ready=true`、`issues=[]`。150 条 query、1079 条 qrel、80 个 scenario 及 release 全部为用户批准，内容哈希与实际文件一致。

去除 review 后的全部数据正文与操作前备份一致；语料、来源、Schema 和附件保持不变。验证证据为 `tmp/evaluation-freeze/verification.json`。

本次只验证冻结与正式数据加载门禁，未执行真实检索、Agent、Ragas 或 LangSmith 调用。
