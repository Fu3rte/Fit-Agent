# 画像身高体重实施交接

## 1. 固定入口

- 仓库与 cwd：D:/Repository/Fit-Agent。
- 基线分支：main。
- 基线 HEAD：68e59aa215c97dcb95783475ac6182c4e8d0b2e7。
- 改动契约：optimization/profile-body-measurements-contract.md，执行前完整阅读。
- 仓库指令：AGENTS.md，以及当前会话已注入的上级指令。
- 已存在的用户文件：context-engineering-answers.md，保持原状。
- 用户已授权任务五、残留实现修复、真实模型测试及无预算上限；同意保留已发布评测版本并派生新版。任务五当前执行依据见 optimization/profile-body-measurements-task5.md。

每个 child 均使用 fresh context，直接读取当前文件。修改前以本地当前内容为准；前置任务的工作区改动是本任务基线。禁止覆盖或回滚其他任务和用户改动。

## 2. 执行约束

- 同一 cwd 串行执行，每次只有一个 writer。禁止子任务嵌套派发。
- 使用 read、grep、find、ls 做限定范围检索；用 edit 定点改动，用 write 创建新文件或完整重写。
- 禁止通过脚本、sed、perl 等程序化手段修改源代码或固定数据。
- 中间文件放 temp/profile-body-measurements/，该目录由已有 temp/ 规则忽略。子任务报告通过 subagent output 路由写入 harness 管理目录。
- 任务五允许 subagent 执行 pytest、测试脚本、lint、编译、构建、真实模型请求、隔离应用启动和接口联调；单个测试命令上限 300s。
- 测试源码和固定数据属于接线范围。失败后修复并复测，按真实输出记录结果。
- 不引入 Mock、假实现、兼容 fallback、新依赖或无需求抽象。现有校验与授权边界保持。
- 不提交 commit，不修改凭据，不上传评测数据，不创建 PR。
- 本文件和改动契约由主会话维护，child 只在回报中提出状态与契约问题。
- 出现未决产品规则、文件冲突、环境不确定或清理风险时停止相应操作，使用 contact_supervisor 联系主会话。

## 3. 任务一：契约文档同步

### 可修改范围

- PRODUCT.md。
- tmp/business/DATABASE.md。
- tmp/business/profile-natural-confirmation-plan.md。
- tmp/contracts/backend-http-sse-contract.md。
- tmp/contracts 下现行计划生成、录入与调整契约中相关字段和门槛段落。
- tmp/design/page-layout-display-conventions.md，以及其他当前适用文档中与新规则直接冲突的段落。

### 必须落实

- 完整画像十一键，身高体重为严格有限正数且非 null，测量日期键存在且值可 null。
- 画像新准备和新提交均必须有身高体重。
- 生成及调整均要求已保存完整画像，调整包括本次上传内容；纯计划录入保持原规则。
- 对话采集、测量日期归属、按钮确认、统一展示与错误语义一致。
- 本次采用旧业务数据清理，新格式直接生效。
- 修正本次触及字段的矛盾现行说明，保持其他文档业务范围。

### 回报

文件路径、变更内容、仍有冲突或未决项、未运行项目。首行输出 STATUS: COMPLETE 或 STATUS: BLOCKED。

## 4. 任务二：后端实现和源码接线

### 入口

- backend/app/domain/business/models.py：ProfileContent、PROFILE_FIELDS、ProfileProposalArguments、PlanSnapshot。
- backend/app/application/business/profile.py：_validate_profile、prepare_profile_update、_commit_save。
- backend/app/application/business/plans.py：_plan_profile_conflict、_prepare_plan、_commit_plan_save。
- backend/app/domain/business/errors.py：复用 ProfileRequired 并更新适用消息。
- backend/app/agent/prompts.py：画像、计划生成及录入与调整指令。
- backend/app/agent/tools/profile.py、plans.py、plan_import.py：仅按实际声明与文案接线需要修改。

### 可修改范围

backend/app 与直接受字段契约影响的 backend/test、backend/evaluation、backend/dataset 固定数据。修改前找全相关构造器、序列化、断言及场景。

### 必须落实

- ProfileContent 非空必填身高体重数值；保留现有其他字段规则与严格校验。
- 所有画像写入链路复用完整 schema 校验，保持保存幂等、授权、事务和版本检查。
- 生成与调整共用已有画像核验路径，缺少已保存画像返回 profile_required；纯 import 不加该前置。
- 依据版本与快照模型必须反映 generation/adjustment 的已保存画像要求。
- 复用项目日期处理和校验模式，前端对照同一 wire 格式。
- 更新提示词并同步真实工具描述，防止通过错误工具类别绕过门槛。
- 新字段对测试和评测固定数据的影响按场景真实意图修改。正常画像补合法字段；原空画像用例改为新契约的拒绝场景或无画像场景，不把随机默认值灌入生产数据。
- 新增或更新最小断言覆盖字段拒绝、计划前置条件与日期归属，随任务五执行。

### 回报

STATUS、修改文件、关键检查路径、源码中新增的测试点、未运行命令、任何残留旧字段调用方。遇到大范围语义变更先联系主会话。

## 5. 任务三：前端实现和源码接线

### 入口与可修改范围

- frontend/src/lib/contract.ts。
- frontend/src/lib/business.ts。
- frontend/src/features/profile/ProfileFields.tsx。
- frontend/src 下实际受字段类型影响的必要调用方。
- frontend/scripts 下直接受完整画像契约影响的检查源码和固定数据。

### 必须落实

- 新字段与后端 schema 一致；exactKeys 校验十一键。
- 复用 requirePositiveNumber、requireDate 等已有原语，避免重复 schema 规则实现。
- 画像页与聊天快照共用字段展示，明确单位和日期。
- 禁止装饰性、引导性或占位文案。
- 同步完整画像对象与断言，并加入缺字段/null/非法数值/日期和数值展示测试源码。
- 保持确认与真实快照绑定、错误显示、类型边界和已有无建档页面状态。

### 回报

STATUS、修改文件、后端字段对照、测试源码变更，以及实际检查命令、退出码、输出和未执行项。

## 6. 任务四：执行旧业务数据清理

### 可操作范围

- 已定位默认业务库 backend/data/fit-agent.db 及其同名 SQLite sidecar。
- temp/profile-body-measurements/ 中的清理脚本和操作记录。
- 确认可归属于本仓库应用的数据库写入进程。

### 安全边界

- 先检查实际数据库路径、工作区服务进程和文件占用。确保停止写入再清理。
- 用户已授权旧画像、计划、训练记录、会话及关联数据删除，无需再次询问相同删除授权。
- 不操作范围外进程。无法确认进程归属或文件写入已停止时联系主会话，并在清理前停止。
- 保留 backend/data/models.json、backend/data/auth.json、独立 trace.db、环境文件、动作目录、原附件文件、用户源文件和其他数据库。
- 可对保留配置计算哈希并核对，禁止输出凭据内容。
- 只清理业务库及指定 sidecar，不递归删除 backend/data、tmp、temp 或其他目录。
- 本轮不启动应用重建数据库；现有 open_database 会在后续启动创建新库。
- 不运行测试，不以清理文件状态冒充功能验收。

### 回报

STATUS、实际执行命令与退出码、进程归属/停写依据、删除文件列表、保留文件完整性核对、应用是否保持停止、空库创建是否仍待后续启动。

## 7. 交接状态

- 改动契约：已冻结。
- 任务一：文档同步完成，运行验收待执行。
- 任务二：后端业务与评测接线、相关专项回归已形成 backend-final.md；真实模型依赖检查由任务五最后工作单补齐。
- 任务三：前端十一键契约、解析、共用展示及对应检查源码已完成，八项指定前端检查和构建已实测通过。
- 任务四：已删除 backend/data/fit-agent.db；其 -wal、-shm、-journal 操作前后均不存在。配置与独立观测库保留。未启动应用，新空库待后续启动创建。
- 任务五：前端与新版数据工作单已有 PASS 回报，后端最终专项报告已收到。十类真实模型 HTTP/SSE 场景（含缺身高、缺体重、小数及 `calculate_date`、文字无权限、纯录入、`upload-continue`、训练记录确认与保存）、同一隔离库重启持久化、真实 wire 跨端解析、40/40 `rebuild-v6` 生产封存执行、`test_real_production_execution_evidence_projection` 节点实测通过及 Docker/Milvus 资源收尾已全部完成，详见 `optimization/profile-body-measurements-verification.md`。

主会话收到各任务回报后，静态对照契约并更新本节。完成实现与通过运行验收分别记录。

### 已有回报

- 工作流 fb5ae2ba-86d6-49d5-b641-1ed67ac8c6a5：任务一 COMPLETE，任务二 BLOCKED。
- 后续工作流 5ac20f19-a548-4d16-a4b4-561f8f9c5080：任务三、四均 COMPLETE，完成状态限于源码与实际数据操作。
- 四份子任务报告存于对应工作流的 harness 管理输出 body-measurements/task-1.md 至 task-4.md。
- 任务四报告已提供实际 PowerShell 命令、退出码 0、进程停写与文件占用依据。未终止进程；清理时未发现本仓库应用候选写入进程。
- 任务四保留 models.json、auth.json、trace.db，子任务回报三者操作前后 SHA-256 一致。主会话目录核对确认业务库已不存在，三份保留文件仍存在。
- 任务三的共享 requireDate 校验同步了 0001–0099 年支持与 0000 年拒绝；该原语同时用于训练日期，后续运行验收需覆盖这些调用方。
- 后端已完成限制投影、完整画像、空画像拒绝、计划画像门槛及当前评测版本接线；当前场景执行核验场景与附件摘要，合规跨版本离线复评保持。后端实际命令见工作流 44cf10fc-870a-4c56-bd7c-05da61f3b4d4 的 body-measurements-task5/backend-final.md。该报告将真实模型依赖项列为 PARTIAL，待专门真实模型 worker 补齐。
- 已发布 rebuild-v1 至 rebuild-v5 均被现有测试引用。用户已批准保持这些版本原样并派生新版适配新契约；新版本为 rebuild-v6，历史文件与发布哈希保持原状。
- 任务一至四的源码实现、文档与固定数据状态以各子任务回报及实际 diff 为准。其 git diff --check 返回 0；任务一至四未运行测试、lint、编译、构建、应用启动、模型请求或接口联调。任务五的结果单独记录。

### 后续接手入口

1. 完整阅读 optimization/profile-body-measurements-task5.md，按已批准策略派生 rebuild-v6 并保留历史冻结版本。
2. 按任务二报告逐项完成残留后端调用方、评测语义和测试源码接线。
3. 对照完整画像字段、计划门槛和既有业务边界，执行确定性测试和跨端契约检查。
4. 从 backend/temp/task5-real-http.py 及 backend/temp/task5-real-http/state.json 核对已有真实场景日志和状态，补齐剩余联调、重启、跨端 wire、真实封存投影及最终资源收尾；命令上限 300s，报告实测证据。

### 当前真实模型工作单

- worker：Role 2 测试与修复 worker 已接续 `38f4b6f0-a8ec-45be-b53f-7d5f7000829b` 完成 R1–R5 全部任务并输出 `optimization/profile-body-measurements-verification.md`。
- 模型调用使用已配置的 `aliyun / deepseek-v4.1-flash`（`openai-completions`），真实 HTTP/SSE、确认与数据库状态核对位于 `backend/temp/task5-real-http/`（`business.db` 画像 v7、计划 5 版、训练记录 1 条、确认操作 16 条；`gate.db` 画像 0 条、纯录入计划 1 条；`contract-wire.json` 已导出）。
- `rebuild-v6` 全部 40 个未修改场景已在 `backend/temp/evaluation-scenarios/task5-v6-batch01..12/` 生成封存 `execution.json` 与 `external-scoring.json`（汇总见 `task5-v6-summary.json`）；`test/test_external_judge_checkpoints.py::test_real_production_execution_evidence_projection` 实测通过（0 skipped），关联回归 `47 passed`。
- 本仓库三个 Milvus 容器已通过 `docker compose stop` 停止（数据卷与集合完整保留），Docker Desktop 已恢复为任务前停止状态；默认用户业务库 `backend/data/fit-agent.db` 仍不存在，保留配置与观测库 SHA-256 校验一致。
- 全仓库测试与完整浏览器点击验收未执行，暂存区为空，代码未提交。
