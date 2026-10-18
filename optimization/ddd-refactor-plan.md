# 三文件 DDD 拆分执行方案

## 授权与边界

已批准完整拆分：`backend/app/interfaces/http.py`、`backend/app/application/session/service.py`、`backend/app/application/business/service.py`。保留全部用户未提交修改，不重置、不提交、不改前端功能、schema或依赖配置。唯一写入者顺序执行；第18项只读。

- HTTP：`interfaces/http_api` 承接 DTO、安全、异常、公开投影、SSE及 provider/sessions/business/agent APIRouter；`http.py` 保留生命周期装配和注册。application/session 的 execution、runtime、coordination、admission 拥有执行、共享运行状态、收尾和受理，应用层无 interfaces/FastAPI 依赖。
- SessionService：查询、附件协调、operation幂等、节点提交、运行生命周期、内容移除分工；原service保留会话命令。所有组件共用真实repository/database；迁出调用方直接接线，无全量转发门面、mixin或动态代理。
- BusinessService：画像、计划、训练、待确认提案、动作查询分工；复用现有catalog/retrieval/confirmation/coordination。共享授权/分支核验有唯一实现，各能力prepare/bind/authorize/save/status连贯；service保留必要组合协调，无通用提案框架。
- 必须保持事务、快照身份、附件省略/空集合差异、取消屏蔽与补偿前真实提交核实、唯一锁/全局名额/故障状态、终态持久化后发布与释放、故障保留真实句柄、关闭排空、SSE最终字节与时限、HTTP安全/脱敏及保存授权/版本/replacement语义。

细化依据：`tmp/ddd-refactor-brief.md`、`tmp/http-ddd-explained.md`、`tmp/session-service-decomposition.md`、AGENTS.md、PRODUCT.md；执行任务定义见 `tmp/ddd-refactor-workflow.js`。

## 执行与文件门禁

拆一份、测一份：HTTP第8项完整验收通过后才进入session；session第12项通过后才进入business；business第16项通过后执行集成及只读审查。单文件内部中间迁移仅静态审查，交接标明待文件完整验证。每个测试命令timeout不超过仓库AGENTS.md上限（当前300秒），实际命令/退出码/关键输出必须记录；关键失败或阻断均为 `GATE: BLOCKED`，停止后续文件。源码仅精确edit/write，不用程序化搬移或自动修复。

## 18项 key 与交接

完整交接路径均为仓库根 `tmp/ddd-stage-N.md`。

| 项 | key | 职责/门禁 | 交接 |
|---|---|---|---|
| 1 | http-projections | HTTP投影/事件出口 | tmp/ddd-stage-1.md |
| 2 | http-execution | 应用执行适配 | tmp/ddd-stage-2.md |
| 3 | http-runtime | 应用状态/SSE传输 | tmp/ddd-stage-3.md |
| 4 | http-coordination | 独立收尾/故障封闭 | tmp/ddd-stage-4.md |
| 5 | http-admission | 受理/幂等/调度 | tmp/ddd-stage-5.md |
| 6 | http-session-routes | 会话HTTP路由 | tmp/ddd-stage-6.md |
| 7 | http-business-routes | 业务HTTP路由 | tmp/ddd-stage-7.md |
| 8 | http-bootstrap | HTTP完整文件验收门 | tmp/ddd-stage-8.md |
| 9 | session-queries | 查询用例 | tmp/ddd-stage-9.md |
| 10 | session-attachments-operations | 附件/幂等协调 | tmp/ddd-stage-10.md |
| 11 | session-entries | 节点/压缩提交 | tmp/ddd-stage-11.md |
| 12 | session-runs-removal | 生命周期/移除；session验收门 | tmp/ddd-stage-12.md |
| 13 | business-profile | 画像/共享授权 | tmp/ddd-stage-13.md |
| 14 | business-plans | 计划能力 | tmp/ddd-stage-14.md |
| 15 | business-workouts | 训练能力 | tmp/ddd-stage-15.md |
| 16 | business-proposals-catalog | 提案/动作；business验收门 | tmp/ddd-stage-16.md |
| 17 | integrated-verification | 跨文件集成验证 | tmp/ddd-stage-17.md |
| 18 | final-review | 只读验收审查 | tmp/ddd-stage-18.md |

## 当前状态与实际证据

HTTP第1–8项迁移已接线，第8项 `GATE: PASS`（HTTP文件最终验收闭环），交接见 `tmp/ddd-stage-8.md` 与 `tmp/ddd-http-recovery-3.md`。

SessionService 第9–12项已完成拆分并接线，文件为 `queries.py`、`entries.py`、`runs.py`、`operations.py`、`attachments.py`、`content_removal.py`、`credentials.py`、`positions.py`；`service.py` 仅保留会话创建/删除与 send/edit/regenerate/steering 命令受理。第12项 `GATE: PASS`，交接见 `tmp/ddd-session-final.md`。

BusinessService 第13–16项已完成拆分并接线：`profile.py`（画像 + 共享授权/保存核验）、`plans.py`、`workouts.py`、`proposals.py`（提案引用/取消/失效原因/快照移除协作）；`service.py` 仅保留四能力组合与动作查询入口（`search_exercises`/`exercise_details`/`exercise_equipment`）。第16项 `GATE: PASS`，交接见 `tmp/ddd-business-final.md`。

- Business 拆分后实际证据：`234 passed` 全量 pytest 三次（269.68s / 257.54s / 257.43s）、业务 `12 passed`、HTTP 契约 `6 passed`、skills/governance `42 passed`；显式 pytest 文件入口补测 `test/check_proposal_cancellation.py` `7 passed in 13.93s`、`test/check_before_permissions.py` `6 passed in 9.62s`、`test/check_attachment_projection.py` `6 passed in 4.83s`；画像、计划（含 import/adjustment）、训练（含 batch bindings、COMMIT 前后崩溃）、提案取消、三类确认处理中互斥、工具权限与凭据/取消边界、结构化确认与 HTTP、授权拒绝、版本冲突、保存幂等、替换/删除交错、catalog/retrieval 条件、工具绑定、附件与会话连接处、Trace、Steering、编辑/重新生成、恢复、HTTP/SSE 全部退出 0。
- 测试入口口径：`backend/pyproject.toml` 的 `[tool.pytest.ini_options]` 未设置 `python_files`，pytest 默认文件模式为 `test_*.py` 与 `*_test.py`，`check_*.py` 不在 `234 passed` 内。`check_proposal_cancellation.py`（7 例）、`check_before_permissions.py`（6 例）、`check_attachment_projection.py`（6 例）无 `__main__`，模块执行只完成导入，通过证据以 `pytest test/<file> -q` 为准；`check_session_run_http.py` 无 `__main__` 但顶层直接运行 node 断言，模块执行有效。
- Business 拆分修复的接线与基线问题：`check_plan_import_*` 的 `getattr(f.business.plans, tool)`；`check_attachment_http` 的会话服务重建改传 `business.proposals`；`check_plan_core` 迁移保真补 `session_runs` 原始列；`check_business_loop` 装配共享 `RequestBudget`；`check_profile_joint` 历史/持久化断言按可见消息口径过滤隐藏节点；`check_retrieval_business_e2e` 声明入口改指真实定义模块。
- Business 拆分未验证：`check_plan_adjustment_e2e` 真模型全流程、`check_retrieval_business_e2e` 在线模式、`check_retrieval_functional_final` 非 selfcheck 模式（缺 `FUNCTIONAL_RUN_DIR`）、`check_skills_acceptance` 人工评审、`check_workout_notes_live`。
- 预存在基线偏差（HEAD 即存在，未修复）：`check_retrieval_production_fixes` 与 `frozen_parameters_unchanged` 的 `alpha=0.5` 期望对应仓库 `0.1`；`legacy_blob_provenance`/`legacy_blob_commit_is_head` 依赖历史 blob 与固定 commit；`audit_guard_blocks_default_trace` 需 `VALIDATION_ROOT`。

- 本会话聚焦pytest：HTTP contracts/run events/credentials/stability task3/task6/附件路径 `45 passed in 127.54s`，governance/skills `57 passed in 11.46s`，均退出0。
- 本会话Windows四项稳定性检查全部退出0并`completed.result=PASS`：完整故障链+中断确认恢复（windows-r3）、worker_stuck（worker-stuck-r2）、db_down（db-down-r2）、graceful_stop（graceful-stop-r2）。证据在 `tmp/ddd-http-stability/`。
- 本会话静态门禁：分层检查（application/domain无HTTP依赖、http.py仅生命周期/凭据组合、运行表与锁唯一、启动入口不变）退出0；迁移范围ruff退出0；24 path/27 operation路由清单与OpenAPI一致；`git diff --check`无输出、无暂存文件。
- 真实HTTP/SSE及HTTP regression、会话历史、附件实际字节限制、edit/regenerate、provider CRUD/端口、7项业务HTTP/确认/删除交错、Trace run/工具最终事件/压缩公开契约已在源码未变情况下实际退出0。
- HTTP恢复第2项完整`check_http_contract`已实际通过：取消读取由ai/stream真实task取消完成，fixture核验error/cancelled、无done、已提交消息与message_end对应及Steering/资源收尾。length经授权单用例注入128真实输出预算，provider返回length、usage.output=128、公开error/model_output_limit_exceeded/failed、持久化与无工具执行/文件不落盘均严格通过；生产默认16384保持，未验证16384-token长流性能。直接回归dispatch、HTTP contracts/events 4例及ruff退出0，详见`tmp/ddd-http-recovery-2.md`。
- HTTP恢复第1项已完成：`check_followup_fixes`按coordination/runtime实际定义模块捕获日志，真实SQLite终态提交故障验证受控fault reason、凭据隔离、无终态、数据库仍running及名额保留；实际退出0。`check_agent_regression`共享RequestBudget已接线，取消fixture覆盖同步/asyncio取消异常，实际退出0。
- compaction loop、model request dispatch、overflow recovery、provider quota逐项实际退出0。HTTP及迁移application模块和本次fixture的ruff已手工整理并退出0。Anthropic真实诊断缺环境，明确未验证。
- 最后worker的57例governance/skills pytest与业务资料runtime/compaction、overflow loop/side effect、Trace spans/audit、资料store/projection均已按其原始会话日志核对为退出0；本会话重跑57例与45例聚焦集合通过。
- 第8项已修复拆分输出身份：受理构造RunState时注入持久化run_id，使普通/终态SSE与真实运行一致，并有回归。
- Windows首轮失败根因：副本`robocopy /XD data`漏掉`backend/app/ai/data`与`node_modules/*/data`，补齐后消失。`scripts/stability-task5/process.ps1`修复PID复用导致的进程树误判并增加系统进程保护；复验期间该缺陷曾导致一次交互会话进程被终止并由系统重新拉起，已修复且后续四项复验快照仅含app树进程。

## 分文件 Review 签收

- HTTP：`check_plan_import_tools.py` 与 `check_prompt.py` 均直接导入 `application.session.execution.PLAN_PREPARE_TOOLS`。计划导入专项已实际退出0；HTTP聚焦45例、请求调度与端口检查的独立review证据保留于 `tmp/ddd-http-review.md`。
- SessionService：共享事务、附件补偿、幂等与快照清理的只读审查完成。`check_session_store.py` 同时隔离业务库与Trace库，通过真实连接的 `PRAGMA database_list` 核验并恢复路径函数；修复验收中存储专项3次及ruff均退出0，见 `tmp/ddd-session-review-fix.md`。
- BusinessService：review实际验证37例pytest（18+取消7+权限6+附件投影6）和6个本地专项。取消、权限、附件投影均采用显式pytest文件入口，命令与结果已写入 `tmp/ddd-business-final.md` §4.7。新增投影与运行HTTP证据已核对worker `426a66d6-137c-4c37-8357-931780ff12d6` 的完整回报。
- 本次分文件review提出的修复项均已闭环；验收范围内未发现新增运行时缺陷。当前状态页为 `tmp/ddd-business-review-fix-prompt.html`，复核证据为 `tmp/ddd-business-review-tests.md`。
- 未验证项、检索alpha基线偏差与第17–18项独立完整门禁保持各自原状态，不因文档签收自动标为通过。本次仅同步文档，未重新运行测试或启动agent。

本状态随后续修复更新；后续worker必须同步维护本方案与各项交接。

SessionService 最终证据（本会话，由 subagent 实际执行，`PYTHONPATH=.;test`，单命令 timeout 300 秒）：

- 迁移 application 范围与 `ruff` 退出 0。
- 新增 `test/check_session_service_split.py` 退出 0：共享 repository、受理原子回滚、凭据失败与 pending 丢弃、压缩准备位置核验、独立查询协作者。
- `check_session_store/history/branch/run_http`、`check_attachment_session/persistence/files/http`、`check_business_context_store/projection/compaction/runtime`、`check_compaction_session`、`check_session_compaction_persistence`、`check_steering/consumption`、`check_structured_confirmation(_http)`、`check_profile_delete_interleaving`、`check_followup_fixes`、`check_http(_regression)`、`check_attachment_agent/projection`、`check_compaction_joint`、`check_overflow_recovery`、`check_prompt`、`check_run_steering_lists` 全部退出 0。
- pytest 聚焦 6 例退出 0；`pytest -q --ignore=test/test_v2_b6_lifecycle.py --ignore=test/test_v2_metrics.py --ignore=temp` 为 `234 passed`。
- 未闭环：v2 基准保护基线快照与当前工作树差异导致 `test_v2_b6_lifecycle`/`test_v2_metrics` 控制流用例失败，属基准快照漂移，未在本次修复；`backend/temp` 历史修复副本会被裸 pytest 收集。

## 工作流与恢复

用户已要求停止自动执行，改为自行安排agent逐份接续。工作流 `26a24ea7-875e-4940-bfb0-e996aa24d89f` 和当前子run `8a6e9491-ffeb-4748-8bed-be87298cd24f` 已停止；禁止自动启动下一文件。HTTP完整文件门禁已PASS，SessionService已通过第12项门禁，BusinessService已通过第16项门禁（三份文件拆分全部完成）。

最新手动交接见 `tmp/ddd-manual-handoff.md`。隔离验收副本为 `tmp/fit-agent-task5`，包含当前未提交拆分源码；其依赖与能力目录已补齐并核对。首轮Windows验收退出1的execution_failed与node-releases缺失已定位为副本缺`backend/app/ai/data`与`node_modules/*/data`并修复；随后又定位并修复`process.ps1`的PID复用进程树缺陷；最终四项Windows检查全部PASS，证据在 `tmp/ddd-http-stability/`。

手动任务状态：HTTP、SessionService、BusinessService三份拆分与分文件review均已完成，review修复项已闭环。`tmp/ddd-manual-prompts.html` 仅作已完成任务归档，不再派发。后续任务由用户单独安排。

三文件拆分当前状态：HTTP、SessionService、BusinessService 均已验收。Business 最终交接为 `tmp/ddd-business-final.md`；后续仅剩第17项跨文件集成验证与第18项只读审查，由用户单独安排。

工作流 `b0b4ea05-34fc-4c08-825e-84ce2453683a` 已停止：第8项子run `8b28c5ef-d3b7-43f6-9b22-8b405384fcb9` 达到30分钟外层运行上限。旧工作流 `40c0ecca-7e80-4ac4-a86e-eecab7e8bc9f` 也已停止；第1–7项成果与交接继续保留。

恢复第1、2、3项均已闭环（`tmp/ddd-http-recovery-1.md`、`tmp/ddd-http-recovery-2.md`、`tmp/ddd-http-recovery-3.md`），第3项为HTTP最终门禁。HTTP文件验收完成后，SessionService 已按第9–12项拆分并通过门禁；BusinessService 已按第13–16项拆分并通过门禁。旧工作流保持超时停止状态。

超时后已核对当前工作树和第8项交接，保存 tracked 差异与状态至 `tmp/ddd-http-timeout-checkpoint.patch`、`tmp/ddd-http-timeout-status.txt`；未跟踪的新文件保留在原路径。HTTP门禁已由恢复第3项闭环。

接续脚本支持 `args.recoverHttp=true`，依次执行 `http-fixture-repair`、`http-contract-repair`、`http-final-gate`；交接为 `tmp/ddd-http-recovery-1.md` 至 `tmp/ddd-http-recovery-3.md`。HTTP、SessionService 与 BusinessService 均已验收通过；剩下的第17–18项由用户单独提交任务。新的运行ID由主会话实际启动回执记录，后续worker同步此文档。

工作流脚本 `args.start` 为零基索引；恢复前由主会话查询工作流及子run状态，确认唯一写入者；重新读取git状态、当前目标文件、brief和最后实际完成交接，从首个未完成项接续。不得跳过失败的文件验收门，不恢复用户已删除内容，不把未运行检查标通过。遇新产品/架构决策或无法证明的契约修改，checkpoint并联系主会话。
