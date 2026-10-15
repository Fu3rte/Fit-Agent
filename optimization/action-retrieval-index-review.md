# T5 源码与零配额复核

## 1. 结论

当前 P2 查询热路径修复通过源码与现有独立 worker 证据复核，可以开展 T6 业务接线。主会话没有重新执行测试或发起 Embedding 请求。

审查范围为本机 Standalone、1324 条目录和单发布者。当前没有发现阻止 T6 开发的问题。

## 2. 查询校验与实例生命周期

`retrieval_index.py::validated_manifest()` 共享清单格式、字节数、SHA-256、compatibility、collection 与 publisher 身份校验。`validate_index()` 在其基础上保留物理 schema、索引、数量、ID、字段及向量完整校验。

`HybridRetriever._validated` 保存实际集合名及 `json_text(manifest)` 的完整指纹。`_manifest_identity()` 每次核验实际源文件哈希、服务与 SDK、模型、维度、编码及索引兼容，并读取和验证清单。

`_ensure_collection()` 在实例内异步锁内完成身份检查。命中成功身份时走轻量路径；无成功记录、集合或清单身份变化时执行完整验证。成功身份只在 `validate_index()` 正常返回之后记录，异常与取消直接传播。

`_validate_published()` 解析正式 alias，检索沿用返回的物理集合名。缓存不持久化，实例重建会重新完整验证。

索引依照当前发布流程作为不可变构建使用，数据变更通过新集合发布。稳定查询路径仍进行完整兼容清单与源版本检查。

## 3. 独立 worker 已执行验证

主会话已读取当前实现、专项测试、命令、退出码及保存的 JSON。以下命令由用户派发的独立 worker 实际运行，工作目录为 backend：

| 命令 | 退出码 | 原始证据前缀 |
| --- | --- | --- |
| `uv run --frozen --env-file .env python -m test.check_retrieval_search_validation validation` | 0 | `tmp/action-retrieval-t5/p2-01-validation` |
| `uv run --frozen --env-file .env python -m test.check_retrieval_search_validation config` | 0 | `p2-02-config` |
| `uv run --frozen --env-file .env python -m test.check_retrieval_search_validation search` | 0 | `p2-03-search` |
| `uv run --frozen ruff check app/application/business/retrieval_index.py app/application/business/retrieval_search.py test/check_retrieval_search_validation.py` | 0 | `p2-04-ruff` |

各前缀包含 command、stdout、stderr、exit 文件。测试使用已有真实 Milvus 集合；调用计数原样透传真实 SDK，不替换数据库结果。

- 启动完整验证：get=14、query=1、load_collection=1。
- 稳定查询：仅 list_aliases、describe_alias、get_server_version、describe_collection 各一次；get/query/load_collection 均为 0。
- 并发同一新身份：get=14，完整验证一次。
- 实例重建首次：get=14。
- 真实集合身份变化：两份兼容保留集合首次分别 get=14；第二份再次检查 get=0。
- 缺失集合与取消：成功身份没有记录；取消后连接可用。
- 正确兼容清单通过；非法模型、维度及损坏清单明确拒绝。
- BM25、dense、hybrid 复用 ID 0001 的已存真实向量，验证目录身份、字段、排序及有效硬约束。

专项结果保存在 `p2-validation-{startup,concurrent,rebuild,identity,failure,state}.json`、`p2-config.json`、`p2-search.json`。Embedding API 请求数为 0。

## 4. 发布状态

`p2-validation-state.json` 记录两份真实保留集合、正式 alias `fit_agent_actions_current` 及实际绑定。验证前后集合、alias 与绑定完全一致，`collections_aliases_binding_unchanged=true`。

当前正式集合为 `fit_agent_actions_build_df2e2f6042934c79b626c29fe8562bb3`，另一兼容保留集合为 `fit_agent_actions_build_84ae20ff94d74d089f6f2a41070436f8`。集合身份由实际只读状态发现，测试未切换正式 alias。

## 5. T6 接线与未验证项

T6 复用当前 HybridRetriever、SearchConditions、同循环 Embedding 与 Milvus 上下文，保持实例内轻量验证。业务用途、画像及临时条件、简要搜索、批量详情、工具预算和调用方更新属于 T6。

Milvus 连接建立约 10 秒，RPC 单次尝试、timeout=None；服务端暂停且连接存活时，RPC、取消和关闭可能持续等待，遵循当前已接受契约。

本次没有覆盖完整校验中途非取消失败的真实数据故障；取消与缺失集合覆盖失败不记录成功身份的部分路径，源码显示身份赋值位于完整验证成功之后。

未重跑在线 Embedding、构建、发布切换、重启、限流、UNAVAILABLE 恢复、端到端业务或质量评测。生产并发与延迟未测量。T4 Ruff I001 保持独立验收状态。
