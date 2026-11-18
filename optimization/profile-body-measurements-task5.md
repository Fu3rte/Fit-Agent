# 身高体重任务五执行交接

## 1. 授权与基线

用户已明确授权：执行任务五；允许真实模型测试且预算不设上限；保留已发布 rebuild-v1 至 rebuild-v5 原样，派生新版评测数据适配身高体重必填规则。

- cwd：D:/Repository/Fit-Agent，分支 main。
- HEAD：68e59aa215c97dcb95783475ac6182c4e8d0b2e7。
- 当前工作区已有任务一至四改动，禁止重置、覆盖或清理。
- 权威改动契约：optimization/profile-body-measurements-contract.md。
- 已实施状态及残留：optimization/profile-body-measurements-handoff.md。
- 现有用户文件 context-engineering-answers.md 保持原状。
- 默认旧业务库已删除。测试使用隔离业务库与观测库；用户模型配置、凭据、原附件和观测数据继续保留。

本文件为任务五的当前执行授权。任务一至四交接中的暂不测试要求仅描述其执行边界，任务五按本文件执行。

## 2. 共用执行规则

- 主会话派发 worker；所有代码与业务测试由 subagent 实际运行。
- 每个 child 使用 fresh context，直接读取当前源码和文档；不 fork，不嵌套派发。
- 同一 cwd 一次一个 writer，采用串行执行。保留前序所有工作区修改。
- 使用 edit 定点修改已有源码与固定数据，用 write 创建新文件或完整重写。
- 禁止用脚本、sed、perl 等批量修改源代码或既有固定数据。新增版本的数据制品可通过成熟 JSON/JSONL 库及现有发布函数生成，历史版本及既有 schema 均只读。
- 不引入 Mock、伪造模型、假实现或只为通过测试的替代分支，不跳过失败用例或削弱业务断言。
- 不在生产数据中注入身高体重默认值，不给仅有限制条件的查询编造完整画像。
- 单条测试命令超时上限 300s。多轮真实模型链路按场景或既有阶段接口拆分，每段保存可接续状态与证据。
- 模型调用预算无硬上限；执行仍限于本次功能验证和必要回归。
- 中间制品与证据写入已忽略的 temp/profile-body-measurements/task5/ 或 backend/temp/ 对应测试目录。禁止读写 /tmp。
- 不暴露密钥、配置明文或私人会话，不改动用户模型配置文件。复用已有隔离配置机制。
- 不提交 commit，不创建 PR，不上传远端评测集，不修改线上 Langfuse 资源。
- 新产品决策、未获授权的数据操作、不可确认的环境风险需 contact_supervisor；普通实现与断言修复由 worker 闭环处理。
- 不设置硬 toolBudget 或 usageBudget。每份报告必须区分通过、失败、超时、未执行及环境阻塞。

## 3. Worker A：补全接线、派生数据版本、后端回归

### 修改范围

backend/app、backend/evaluation、backend/test、backend/dataset/evaluation/rebuild-v6、新增版本化 schema，以及本次数据派生的授权/溯源记录。历史 rebuild-v1 至 v5、其批准记录及原 schema 文件保持不变。

### 数据版本要求

- 从 rebuild-v5 派生 rebuild-v6，记录父版本与本次用户授权。
- 已批准的 corpus、queries、qrels 与附件继承原内容，不新增检索相关性标签。
- 场景补齐新画像字段，并同步对话中必填信息、确认顺序和预期。新身高体重为明确的合成测试事实，不能冒充真实用户数据。
- 维持原场景意图和主要分类分布；按本契约处理原空画像或无画像调整用例。
- 新 schema 放独立路径，保留旧 manifest 引用的所有 schema 的原哈希。
- 复用现有 schema 校验、摘要和发布机制，记录逐项适配范围及真实委托审阅来源。用户授权派生，不能声称用户逐条人工审阅了新内容。
- 新版本具备当前业务回归可执行性。旧版本保留冻结完整性审计，其历史业务输入不得静默注入当前执行流程。
- 如修改限制投影相关类型，保持实际检索限制语义和生产校验，不构造伪造的数值画像。

涉及评测数据时先读取 .pi/skills/langfuse/SKILL.md 和 references/create-dataset.md。读取其要求的当前官方数据集设计资料；网络访问按 agent-reach skill 路由。新版本只用于本地回归，本轮不写远端资源。

### 已知残留入口

- backend/evaluation/dataset.py：_validate_scenarios、_query_profile 与版本/发布校验。
- backend/evaluation/retrieval_support.py：dataset_conditions、effective_conditions。
- backend/evaluation/deterministic_assertions.py：旧空画像语义。
- backend/test/test_evaluation_contracts.py、test_evaluation_scenarios.py：旧空画像断言。
- backend/test/check_plan_import_tools.py、check_plan_import_harness.py：无画像调整成功的旧预期。
- 搜索全部 ProfileContent 构造、完整画像固定数据和对应计划门槛断言，闭环同步当前调用方。

### 后端验证

在 backend 工作目录运行并闭环修复：

- uv run python -m pytest test/test_profile_body_measurements.py -q。
- uv run python -m test.check_profile_snapshot_store。
- uv run python -m test.check_business_profile。
- uv run python -m test.check_profile_confirmation。
- uv run python -m test.check_plan_core。
- uv run python -m test.check_plan_import_adjustment。
- uv run python -m test.check_plan_import_tools。
- uv run python -m test.check_plan_import_harness。
- uv run python -m test.check_structured_confirmation_http。
- uv run python -m test.check_plan_http。
- 新版数据的 schema、摘要、冻结历史文件完整性及实际加载检查。
- 受影响的评测和检索 pytest 文件；按文件分组执行以满足 300s 上限。

补充测试必须直接覆盖真实准备、提交、未保存画像、版本及幂等边界。确保本轮生成 backend/temp/profile-snapshot/profile-snapshot.json，供后续跨语言检查使用。

本任务可使用真实已配置服务执行需要的回归，不改变凭据。普通失败须修复并复测；环境阻塞明确报告。

## 4. Worker B：前端、跨语言契约和相关回归

前提：Worker A 已提供可验证的后端结果及新契约证据。可修改 frontend/src、frontend/scripts 及直接相关契约错误。后端源码需改动时联系主会话，保持唯一 writer。

在 frontend 工作目录运行并闭环修复：

- npm run verify:profile。
- npm run verify:plan。
- npm run verify:confirm。
- npm run verify:workout。
- npm run verify:stability。
- node scripts/compaction-history-check.mjs。
- node scripts/profile-snapshot-contract-check.mjs，输入必须是本轮 Worker A 生成的证据。
- npm run build。

核对十一键、正有限数值、小数和单位、测量日期与未知日期、实时与历史快照、重复确认及页面错误状态。共享日期原语还须覆盖训练记录日期。

缺少证据时报告依赖，禁止复用旧八键证据冒充本轮结果。

## 5. Worker C：真实模型与 HTTP/SSE 联调、持久化验收

前提：基础回归及跨语言契约已有真实通过证据。可修改本次真实模型测试脚本、断言和必要实现以闭环修复；维持单一 writer。涉及新产品语义时联系主会话。

复用 backend/test/regression_support.py 的真实服务、隔离数据库/观测库和配置机制。检查实际配置支持的模型 API；不得引入模型替身或修改用户凭据。真实请求使用已配置模型，调用预算无上限。

入口参考：backend/test/check_profile_joint.py、check_plan_adjustment_e2e.py 及其 support。画像旧建档输入缺少身高体重，先适配后使用。可新增最小专项脚本，场景分段运行并记录证据。

必须取得以下真实场景结果：

1. 缺少身高或体重时澄清，未创建可保存的不完整画像。
2. 完整画像准备后未点击确认，生成及调整仍受已保存画像前置条件约束。
3. 点击真实画像确认 HTTP 后，读取到正确三字段和新版本。
4. 已保存画像支持生成计划，完整展示后确认保存。
5. 调整当前计划或本次上传内容遵守画像前置规则，确认后保存正确版本。
6. 单项更新保留其他字段；体重新测量日期未知时清空对应日期。
7. 单位或历史体重用途不明时澄清且保持已保存数据。
8. 普通文字确认无保存权限；重复确认幂等；画像版本改变使旧计划依据冲突。
9. 服务停止后用同一隔离数据库重启，画像、计划和记录仍可读取。
10. 本轮实际 HTTP/SSE wire 经 frontend/scripts/profile-joint-wire-check.mjs 或等价生产解析链验证，页面与对话字段一致。

按场景拆分单次运行，均不超过 300s。运行结束关闭本任务启动的服务和资源。普通业务失败修复后复测；不得用模型输出措辞代替数据库和确认操作证据。

## 6. 回报协议与完成标准

每个 child 首行输出 STATUS: PASS、STATUS: PARTIAL 或 STATUS: BLOCKED。

- PASS：本任务必需检查真实执行且通过，源码/数据按范围闭环。
- PARTIAL：部分执行完成，列明失败与未执行项。
- BLOCKED：环境、授权或产品决策导致无法继续，给出精确证据。

报告列出修改文件、新版数据位置、冻结历史核对结果、每条命令及 cwd/退出码/耗时/关键输出/证据路径、修复与复测结果、服务收尾、残留风险。

harness 的正常退出不等于功能通过。主会话依据报告及实际文件对照更新主交接文档与验收状态。

任务五完整通过要求：核心业务、跨端契约、真实模型链路和持久化场景均有实测通过证据；相关回归失败和环境阻塞已经解决。全量检查未执行时须明确，不得宣称全量回归通过。

## 7. 当前执行证据与续接

首轮后端 worker 41d4ef00-f534-45f4-a491-80cbd7acee04 回报 STATUS: PARTIAL。报告在工作流 6bb377be-f6b6-4790-b391-aef04741145a 的 harness 输出 body-measurements-task5/backend.md。

已实测通过：

- test_profile_body_measurements.py：20 passed。
- test_retrieval_stages.py：6 passed，包含完整画像与限制投影等价断言。
- test_evaluation_contracts.py：20 passed。
- test_deterministic_assertions.py：30 passed。
- check_profile_snapshot_store、check_business_profile、check_profile_confirmation、check_plan_core、check_plan_import_adjustment、check_plan_import_tools、check_plan_import_harness、check_structured_confirmation_http、check_plan_http 共九个业务脚本，修复后最终退出码均为 0。
- 本轮跨语言输入已生成于 backend/temp/profile-snapshot/profile-snapshot.json。

后续已完成：前端八项命令退出码均为 0，包含 build、画像/计划/确认/训练/稳定性、压缩历史与跨语言检查；跨语言检查接受 11 条、拒绝 40 条。

rebuild-v6、独立 schema 与溯源记录已生成，版本加载 ready=True、issues=[]；24 项版本及契约测试通过。历史 71 个文件派生前后原始字节校验一致，默认执行计划加载新版 40 场景。

剩余后端 worker 9b7a4ea3-cd5e-45b0-9091-4f9d0cc884ba 达到运行总时限 1800000ms 后终止。该 worker 修改的十五个测试文件已保存于 temp/profile-body-measurements/task5/backend-timeout-partial.diff，并完成静态检查；没有形成最终回报，不将中间日志自行计作完整回归通过。

中间日志位于 temp/profile-body-measurements/task5/backend-*.log。最后运行 external-summary pytest 分组，单命令设有 300s 上限。主会话进程核对未发现匹配该测试的遗留 Python 进程。

后端恢复工作单已结束并形成完整命令账本。下一工作单为真实模型与持久化联调，worker 总时限 90 分钟，每条测试命令为 300s；前端与数据派生工作单已有独立通过证据。

后端最终专项报告已形成：工作流 44cf10fc-870a-4c56-bd7c-05da61f3b4d4 的 body-measurements-task5/backend-final.md，worker 39f4d48c-f215-445b-b1d8-ac556b203658。当前场景、数据集、离线跨版本复评、相关检索与汇总/反馈专项均有实际通过命令；40 个 v6 初始状态已在独立 SQLite round-trip，真实检索重排复测通过，历史 71 文件哈希一致。

该报告 STATUS: PARTIAL 的剩余依赖为真实模型工作单：十类端到端场景，以及 test_external_judge_checkpoints.py::test_real_production_execution_evidence_projection 缺少本轮 v6 封存检查点而条件跳过。后端已具备真实模型验收前置条件，直接派发专门 worker 完成这些依赖；不再重复派发已完成的后端专项。

真实模型 worker 需要产出与 v6 场景/摘要相符的实际执行封存证据，并复测该节点，结果必须为实际通过。可将测试证据发现路径显式接到本轮真实封存位置，保留内容、摘要、请求边界及模型调用核验，禁止构造替身 SDK 运行或伪造执行产物。

尚未完成：真实模型/HTTP链路、重启持久化及真实封存投影检查；全仓库测试未执行。

### 当前真实联调入口

后端最新 run 为 39f4d48c-f215-445b-b1d8-ac556b203658，已提交 backend-final.md。真实模型 worker 38f4b6f0-a8ec-45be-b53f-7d5f7000829b 执行了部分真实场景后因 ECONNRESET 退出，尚未形成最终报告；当前工作单处于中断状态，保留已批准的服务与数据边界。

主会话已保存当前全部 tracked diff：temp/profile-body-measurements/task5/continue-working-tree.diff，静态 diff 检查日志为 continue-diff-check.log。禁止重置任何部分改动。

以下为已批准的技术与环境边界，后继 worker 可直接沿用：

- 跨版本离线检索复评保留原批准/摘要/来源/兼容性门槛。新画像契约门禁位于真实 Agent 业务场景运行入口；通用加载器支持合规历史或派生数据。
- 已授权以当前用户启动既有 Docker Desktop/Linux daemon，按仓库 Compose 启动已有 fit-agent-milvus 服务；保留数据卷和集合，不提权、不更改 Docker/WSL 配置、不重建数据。
- Docker Desktop 原状态为停止。本任务已启动，三个本仓库 Milvus 服务在 recovery-milvus-ownership.log 中核对为 healthy；后续仍须核对当前状态。
- 真实检索重排专项已复测，recovery-retrieval-production-retest.log 记录退出码 0、27.629s，含真实 request_id。恢复 worker 应在报告补全实际命令上下文。
- 最终真实模型 worker 继续使用这些服务，结束时仅清理本任务资源。无其他并发用户工作依赖时按原停止状态收尾；有并发依赖时报告保留情况，避免强停。

已有真实脚本：backend/temp/task5-real-http.py；隔离状态：backend/temp/task5-real-http/state.json；真实模型为已配置 aliyun / deepseek-v4.1-flash。已有实际场景输出覆盖画像、计划生成/调整、单项更新、歧义、冲突及上传调整，完整退出码账本与最终报告待补。

剩余闭环包括同一隔离库重启持久化、实际 wire 的前端解析、v6 真实封存检查点投影，以及资源状态恢复。不得用已有部分场景输出宣称十类场景全部通过。

主会话状态核对：Milvus etcd/minio/standalone 仍 running/healthy，默认用户业务库仍不存在，用户配置与观测库保留，代码未提交。全仓库测试和完整浏览器点击验收未执行。

测试执行统一使用 uv run python -m pytest，单条命令仍不超过 300s。通过项按真实代码版本记录，相关代码变更后补相应复测。

旧版本工作树存在 Git autocrlf 换行规范化。原样保留当前字节，不为消除 SHA 与 HEAD blob 的换行差异重写制品。派生前记录当前逐文件原始字节哈希并在派生后核对；该记录不冒充首轮执行前未采集的原始基线。

