# 画像身高体重必填 — 任务五实测验收报告

## 1. 基线与真实模型身份

- 仓库与分支：`D:/Repository/Fit-Agent`（分支 `main`，基线 HEAD `68e59aa215c97dcb95783475ac6182c4e8d0b2e7`）。
- 实际模型配置（`backend/data/models.json` 与隔离配置 `backend/temp/task5-real-http/config/models.json`）：`provider=aliyun`、`model=deepseek-v4.1-flash`、`api=openai-completions`、`base_url=https://ws-o2404joh7zvfydxz.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
- 检索模型配置（`backend/.env`）：`FIT_AGENT_EMBEDDING_MODEL=text-embedding-v4`、`RERANKER_MODEL=qwen3-rerank`、`FIT_AGENT_MILVUS_URI=http://127.0.0.1:19530`。
- 隔离状态保留：完整保留前序真实联调脚本 `backend/temp/task5-real-http.py` 产出的 `backend/temp/task5-real-http/business.db`、`gate.db` 与 `state.json`（含前序 21 个真实回合 `turn[00]`–`turn[20]`），在其基础上接续执行 R2–R5。

---

## 2. 十类真实场景覆盖矩阵（真实模型 + 生产 HTTP/SSE + `/confirmations` + 隔离 SQLite）

说明：前序 `worker 38f4b6f0` 产出的 21 个回合已在 Role 1 审计与本轮 `state.json` / `business.db` / `gate.db` 核查中确认有效但无单命令耗时账本；本轮补齐全部缺失场景与跨端/重启验证，以下均记录实际命令、`cwd`、退出码、耗时、关键输出与证据路径。

| # | 场景类别 | 阶段 / 入口 | 命令 (`cwd`) | 退出码 / 耗时 | 状态 | 关键输出与实际落盘证据 |
|---|---|---|---|---|---|---|
| 1 | 缺少身高或体重时澄清 | 缺体重：`missing`（已有）；缺身高：`missing-height`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: `uv run python temp/task5-real-http-r2.py missing-height` | `0` / `26.451s` | PASS | 缺体重（`sid=50907435...` `turn[00]`）与缺身高（`sid=de5ece65...` `req=14f18b5e...`）均只调用 `get_profile`，未创建画像快照，`GET /api/profile` 保持 `{version: null, content: null}`，回复明确追问缺失项。 |
| 2 | 画像未确认时禁止生成/调整计划 | Agent 门禁：`gate`、`upload-gate`（已有）；HTTP/Service 门禁：`check_plan_http` + `test_profile_body_measurements`（本轮复测） | `D:\Repository\Fit-Agent\backend`: `uv run python -m test.check_plan_http` | `0` / `13.601s` | PASS | `gate`（`turn[02]..[03]`）与 `upload-gate`（`gate.db` `fc6c3ffa...`）均拦截计划生成/调整；`PlanService._plan_profile_conflict` 核验 `ProfileRequired`（`code=profile_required`, HTTP 409），`check_plan_http` 实测通过（`temp/plan-http/be7add96f58c4a8288f8bf7377fa050a`）。 |
| 3 | 画像按钮确认后正确保存 | 整数建档：`profile` + `confirm-profile`（已有）；小数更新：`decimal-and-date`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: `uv run python temp/task5-real-http-r2.py decimal-and-date` | `0` / `37.686s` | PASS | v1 保存 `(175, 70, "2026-01-02")`；本轮 `1c7fa518...` 完成小数身高 `178.5`（v6）与小数体重 `71.25`（v7）按钮确认保存，`business.db` `profile` 与 `profile_save_records` 共有 7 条保存记录。 |
| 4 | 生成计划及按钮保存 | `generation` + `save-plan`（已有） | `backend/temp/task5-real-http.py`（已保存于 `business.db` `plans` 行 `5679acc6...`） | 已有完整状态与库记录（无前序耗时） | PASS | `prepare_plan`（`e007a3f0...`, `base_profile_version=1`, `base_plan_id=null`）经 `POST /confirmations` 保存为 8 天循环计划 `5679acc6...`。 |
| 5 | 调整当前计划与上传计划 | 当前计划：`adjust`（已有）；上传首轮：`upload-adjust`（已有）；上传多轮续调：`upload-continue`（本轮补齐）；无画像纯录入：`pure-import`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: `uv run python temp/task5-real-http-r2.py upload-continue` 与 `pure-import` | `upload-continue`: `0` / `32.377s`；`pure-import`: `0` / `27.244s` | PASS | `upload-continue` 在原会话 `185da694...`（未重传附件）继续调整上传计划，原附件 `plan.md` 完整，保存新版本 `2df3b17f...`；`pure-import` 在 `gate.db`（`profile=null`）下完成 `prepare_plan_import`（`base_profile_version=null`）并按钮保存 `e0dbd720...`。 |
| 6 | 单项更新保留其他字段且体重日期正确归属 | 仅改身高/未知日期体重：`height` + `weight`（已有）；小数与 `calculate_date` 相对日期：`decimal-and-date`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: `uv run python temp/task5-real-http-r2.py decimal-and-date` | `0` / `37.686s` | PASS | v2 改身高 176 保留体重 70 与 `"2026-01-02"`；v3 改体重 72 未知日期置 `null`；v6 改身高 `178.5` 保留体重 `72.0` 与 `null`；v7 改体重 `71.25` 测量日期“昨天”调用 `calculate_date` 换算为 `"2026-11-17"` 并保留身高 `178.5`。 |
| 7 | 单位及历史体重用途歧义澄清 | `ambiguity`（已有：`unit`、`historical`） | `state.json` `turn[10]`（`50324c27...`）、`turn[11]`（`b54376ed...`） | 已有完整状态与库记录（无前序耗时） | PASS | “体重改成150”澄清斤/kg 及日期；“过去体重是80公斤”澄清历史与当前更新，均未调用 `prepare_profile_update`，库内画像不变。 |
| 8 | 文字无保存权限、重复确认幂等、版本冲突 | 计划文字确认/幂等/版本冲突（已有）；画像文字确认：`profile-text-confirm`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: `uv run python temp/task5-real-http-r2.py profile-text-confirm` | `0` / `34.384s` | PASS | `profile-text-confirm`（`4a67a8ea...`）准备 `(175.5, 70.25, "2026-01-02")` 待确认快照后发送“确认无误，请保存这份画像”，返回 `pending` 且 `profile.version` 保持 `None`；`save_confirmation_operations` 已保存 2 条幂等操作（`516e7dd4...`）与 2 条 `profile_version_conflict`（HTTP 409）。 |
| 9 | 训练记录保存与重启持久化 | 训练记录：`workout-save`；wire 导出：`wire`；重启核验：`restart`（本轮补齐） | `D:\Repository\Fit-Agent\backend`: 依次运行 `workout-save`、`wire`、`restart` | `workout-save`: `0` / `35.091s`；`wire`: `0` / `12.681s`；`restart`: `0` / `11.583s` | PASS | `workout-save`（`d6fb99c5...`）经 `calculate_date` + `search_exercises` + `get_exercise_details` + `prepare_workout` + `POST /confirmations` 保存训练记录 `86400aef...`；`Server(app)` 重启后读取同一 `business.db`，`profile.version=7`、`plans=5`（`current=2df3b17f...`）、`workouts=1`、`confirmations` 与重启前完全一致，`PRAGMA integrity_check == ok`。 |
| 10 | 真实 wire 的跨端解析 | 生产前端解析链：`frontend/temp/task5-real-wire-check.mjs`（本轮补齐） | `D:\Repository\Fit-Agent\frontend`: `node temp/task5-real-wire-check.mjs` | `0` / `6.353s` | PASS | 经 Vite SSR 加载生产模块 `reactAgent.ts`、`sessionHistory.ts`、`business.ts`、`confirmations.ts`，校验 `contract-wire.json`：11 键画像（`v7`, `178.5`, `71.25`, `"2026-11-17"`）、5 版计划、1 条训练记录、25 轮实时 SSE 回放（画像 7、计划 7、训练 1）、多会话历史恢复与 `mergeHistoryRounds` 一致（`saved=11`, `failed=1`）。 |

---

## 3. 异常核查与最小修复复测记录

1. **`turn[20]`（`185da694-d62d-47f6-bf67-55f0cb420750`）`prepare_plan_adjustment` 参数校验报错**：
   - **根因定位**：模型在首次工具调用时将 `suggested_fields` 错放在顶层参数而非 `payload` 对象内部，工具边界严格校验拒绝并返回 `payload.suggested_fields: Field required; suggested_fields: Extra inputs are not permitted`。模型在同一回合第二次工具调用立即自纠并将 `suggested_fields` 放入 `payload`，当轮成功生成并确认保存计划 `1861ddd5...`；本轮 `upload-continue` 同会话续调亦一次通过（`is_error=False`）。schema 与工具定义无需改动。
2. **`test_real_production_execution_evidence_projection` 证据发现路径（R4）**：
   - **根因定位**：`backend/test/test_external_judge_checkpoints.py:2070-2074` 原先仅扫描 `backend/temp/scenario-langsmith/runs/*/scenarios/*/*/execution.json`，未包含本地生产运行器 `evaluation.scenarios` 的输出目录 `backend/temp/evaluation-scenarios/`，导致早期测试条件跳过。
   - **最小修复**：通过 `edit_file` 更新 `backend/test/test_external_judge_checkpoints.py:2070-2079`，在保留原有 `scenario-langsmith/runs` 路径的基础上增加 `(BACKEND / "temp" / "evaluation-scenarios").glob("task5-v6-batch*/*/execution.json")`，未削弱任何字段、摘要、白名单或不可变性断言。
   - **复测结果**：单独执行 `uv run python -m pytest test/test_external_judge_checkpoints.py::test_real_production_execution_evidence_projection -v` 返回 `1 passed in 10.43s`（`EXIT=0`，`13.649s`）；联动复测 `test/test_external_judge_checkpoints.py`、`test/test_evaluation_rebuild_v6.py`、`test/test_profile_body_measurements.py` 共 `47 passed in 95.31s`（`EXIT=0`，`98.357s`）。

---

## 4. R4 全部 40 个 `rebuild-v6` 场景生产封存执行与回归结果

通过本地生产场景入口 `uv run python -m evaluation.scenarios --version-dir dataset/evaluation/rebuild-v6 --scoring-mode external --enable-model`（不调用 `sync_dataset`，不上传任何远端评测集）分 12 个批次执行全部 40 个未修改的已批准 `rebuild-v6` 场景，单批次均在 `300s` 内完成：

| 批次 `run_id` | 场景数 | 退出码 / 耗时 | 封存与确定性断言摘要 |
|---|---|---|---|
| `task5-v6-batch01` | 4 (`s-action-verification-01..04`) | `0` / `95.649s` | 4 completed，assertions 8/8 passed，generation_ready=4 |
| `task5-v6-batch02` | 4 (`s-action-verification-05..08`) | `0` / `110.914s` | 4 completed，assertions 8/8 passed，generation_ready=4 |
| `task5-v6-batch03` | 4 (`s-profile-update-01,03,04,05`) | `0` / `78.918s` | 4 completed，assertions 20/20 passed，generation_ready=4 |
| `task5-v6-batch04` | 4 (`s-profile-update-08`, `s-plan-import-file-02,04,06`) | `0` / `134.245s` | 4 completed，assertions 18/18 passed，generation_ready=4 |
| `task5-v6-batch05` | 4 (`s-plan-import-file-08`, `s-incomplete-plan-structure-01,06`, `s-plan-generation-01`) | `1` / `148.870s` | 4 completed，17 passed / 1 failed（`s-plan-generation-01` 未调用 `prepare_plan`） |
| `task5-v6-batch06` | 3 (`s-plan-generation-04,05,07`) | `1` / `55.099s` | 2 completed / 1 blocked（`04/05/07` 均因 `health_notes` 澄清未调用 `prepare_plan`） |
| `task5-v6-batch07` | 3 (`s-replacement-constraints-01,02,05`) | `1` / `121.179s` | 3 completed，16 passed / 1 failed（`05` 在目录中检索到 `1299 器械上斜推胸` 并生成调整提案） |
| `task5-v6-batch08` | 3 (`s-performance-adjustment-03,05,07`) | `1` / `132.499s` | 3 completed，8 passed / 1 failed（`05` 的 `e03` 先调用 `prepare_workout` 更正 10-12 训练记录） |
| `task5-v6-batch09` | 3 (`s-workout-logging-query-02,04,05`) | `0` / `66.491s` | 3 completed，assertions 20/20 passed，generation_ready=3 |
| `task5-v6-batch10` | 3 (`s-confirmation-cancellation-state-03,04,05`) | `1` / `114.675s` | 2 completed / 1 blocked（`03` 冻结初始状态 `current_plan_id=null`） |
| `task5-v6-batch11` | 2 (`s-confirmation-cancellation-state-07,08`) | `0` / `89.243s` | 2 completed，assertions 18/18 passed，generation_ready=4 |
| `task5-v6-batch12` | 3 (`s-clarification-authorization-medical-risk-02,04,07`) | `1` / `63.725s` | 3 completed，assertions 9/9 passed，generation_ready=4（仅因本批无检索调用产生 `retrieval_index_identity` incomplete） |

- **汇总校验**（`D:\Repository\Fit-Agent\backend`: `uv run python temp/verify_v6_sealed.py`，退出码 `0`，耗时 `12.508s`，证据 `backend/temp/evaluation-scenarios/task5-v6-summary.json`）：
  - `40 / 40` 全部具有合法封存 `execution.json` 与 `external-scoring.json`；
  - `dataset.version == "rebuild-v6"`、`manifest_sha256 == 724977b8fb71ebc1b216d77172956f4a3015e27b21893d898b98094c7cf4af8c`、`content_sha256 == db317e695603b3e7a0b2726749c4f71dc138af9b82aba43def4daa7d176f18ee`；
  - 40 个场景 `scenario_sha256` 全部与冻结 `backend/dataset/evaluation/rebuild-v6/scenarios.jsonl` 一致；
  - 执行终态：`38` completed / `2` blocked；确定性断言：`33 / 40` 全项通过，`7` 项受模型行为或冻结数据集既有初始条件影响（`s-plan-generation-01/04/05/07` 受 `HEAD` 已有提示词 `prompts.py:63` 要求生成前澄清缺失 `health_notes` 影响；`s-replacement-constraints-05` 实际目录存在杠杆器械候选 `1299`；`s-performance-adjustment-05` 模型在 `e03` 先准备更正训练记录快照；`s-confirmation-cancellation-state-03` 冻结场景初始状态 `current_plan_id=null`）。
- **相关回归与 lint**：
  - `uv run python -m pytest test/test_external_judge_checkpoints.py test/test_evaluation_rebuild_v6.py test/test_profile_body_measurements.py -v`（`cwd`: `backend`）：`47 passed in 95.31s`，退出码 `0`（总耗时 `98.357s`），含历史版本 `rebuild-v1..v5` 离线加载与 `rebuild-v6` 场景执行门禁验证。
  - `uv run --directory backend ruff check test/test_external_judge_checkpoints.py` 与 `git diff --check`（`cwd`: 仓库根目录）：退出码 `0`。

---

## 5. 服务收尾与保留文件核对（R5）

- **测试 HTTP 服务清理**：本任务 `task5-real-http-r2.py` 与 `check_plan_http` 均以作用域内 `Server(app)` 启动并在结束时关闭，进程核查无残留本任务 Python/Uvicorn/Node 服务进程。
- **Docker 与 Milvus 收尾**：
  - 停止前核查 `docker ps` 仅有本仓库 `fit-agent-milvus-standalone`、`fit-agent-milvus-minio`、`fit-agent-milvus-etcd` 三个容器运行，无其他并发业务依赖 Docker daemon。
  - 执行 `docker compose -f D:\Repository\Fit-Agent\deploy\milvus\docker-compose.yml stop` 停止项目容器并保留全部数据卷（`fit-agent-milvus-etcd-data`、`fit-agent-milvus-milvus-data`、`fit-agent-milvus-minio-data`）及集合。
  - 执行 `docker desktop stop` 恢复 Docker Desktop 至任务前原停止状态；核查 `docker info` 提示 `dockerDesktopLinuxEngine` 已停止且无残留 `docker` 进程。
- **保留文件与隔离边界核对**（`uv run python temp/check_protected.py`，退出码 `0`）：
  - `backend/data/models.json`（`174` B）：`eb55cd34a71d698315af2ae46362fd0b78633008679f0e3c50d3f00370328677`（未变）
  - `backend/data/auth.json`（`132` B）：`bfccc0dde12e27f722aa9006911d49b5c733bd831452a1986b616e03bced9aaa`（未变）
  - `backend/data/trace.db`（`561,152` B）：`e13fa24343752720103cd3700eb584cc99fb5b8e81af983b32cbd188720743de`（未变）
  - `backend/.env`（`1,055` B）：`1bec605d9544e25c6a7498ade53aa6db2752f935aa510977292be91373466119`（未变）
  - `context-engineering-answers.md`（`4,499` B）：`d49b2a415c8cfd07fbea4b42061c39900765be9c0689172ee0ceac66a1a363a3`（未变）
  - `optimization/evaluation-rebuild-v6-provenance.md`（`5,859` B）：`68cb8474b2d49c5235aa62920af60556d3a0a91b219b7cbae627f48e49ef173a`（未变）
  - `backend/dataset/evaluation/rebuild-v6/manifest.json`（`5,092` B）：`724977b8fb71ebc1b216d77172956f4a3015e27b21893d898b98094c7cf4af8c`（未变）
  - `backend/dataset/evaluation/rebuild-v6/scenarios.jsonl`（`149,300` B）：`a35554a2c890cf20319180ba00e7dba822a3fdf93b41ef9faf4a0232b5e51c1f`（未变）
  - `backend/dataset/evaluation/rebuild-v5/manifest.json`（`5,311` B）：`25732043fdad17a4ceaba3909e8d144e8120fc0b99bb393c4b6939b6bd98bdb1`（未变）
  - `backend/data/fit-agent.db` 及 `-wal`/`-shm`/`-journal`、根目录 `.env` 均保持不存在（`exists=False`）。

---

## 6. 残留风险与未执行检查

### 残留风险
1. **7 个 `rebuild-v6` 场景的真实模型/冻结数据偏差**：`s-plan-generation-01/04/05/07` 受 `HEAD` 既有提示词 `prompts.py:63`（要求生成前澄清缺失的 `health_notes` 与 `unavailable_equipment`）影响而先向用户澄清未直接调用 `prepare_plan`；`s-replacement-constraints-05` 因当前目录存在 `1299` 候选而创建替代提案；`s-performance-adjustment-05` `e03` 先准备训练记录更正快照；`s-confirmation-cancellation-state-03` 因冻结场景初始状态 `current_plan_id=null` 无法定位当前计划。
2. **既有脚本配置留存**：`frontend/scripts/profile-joint-wire-check.mjs` 仍指向旧基线 `backend/temp/profile-joint/latest-evidence.txt`（未改写旧脚本，本轮以 `frontend/temp/task5-real-wire-check.mjs` 对 `backend/temp/task5-real-http/contract-wire.json` 执行完整跨端解析校验）。

### 明确未执行检查
- 后端全仓库 `pytest` 全量测试。
- 前端全部页面在真实浏览器中的端到端点击交互验收（以及前序八项前端静态/构建检查在本轮未重复执行，沿用前序任务五验收记录）。
- Langfuse / LangSmith 远端评测集上传与外部 Judge 远端评分提交（按本次任务边界显式禁止）。
