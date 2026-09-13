# S3 运行证据：Stage 3 领域（计划／记录／统计／复盘）+ Windows 结项

> 契约与端点／DTO／前端待改正本：`../stage3.md`、`../stage3-handover.md`（§1 端点清单、§3 F1–F10）；本文件只记证据，不复制契约。
> **证据分层**：Linux 侧为 pytest 离线证据（均在 `backend/` 下用 `tmp_path` 临时文件库，不触碰真实用户库）；Windows 结项为同版本全量完整收集、无 skip／xfail、退出码 0；逐任务 reviewer 与 Windows 结项只读复核均为静态阅读，**未复跑本文件命令**，计数不由复审背书；无真实端到端验收，前端未切真轨。
> **阶段门槛**＝同版本 Windows 全量完整收集、全部通过、退出码 0。
> **基线**：后端 `94dbc49`（feat: complete stage 3 records and statistics），随后 frontend `38073e5`（未改后端）；Linux WSL2 2026-09-11～12（Python 3.13 venv、ruff、pyright 全局安装；逐任务证据在各自工作区采集、非同一棵树，各任务自证）、Windows 11 2026-09-12（`D:\Repository\Fit_Agent`，pytest 用临时文件库）；迁移止于 `012`；未新增依赖、未改 `001`–`011`。
> ⚠️ **2026-09-12 设计变更（待实现）**：努力程度改为仅处方字段。下表 S3-08／09／10／12 行中记录侧 RIR 字段与判定为**变更前状态**，实现落地后须追加新行复核；决策见 `pre-prj/design-decisions.md`「训练域设计增量」。

## 1. 结果正本

| 门 | 命令 | 结果 |
| --- | --- | --- |
| **Windows 结项（阶段门槛，2026-09-12 实测）** | `backend\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | **658 passed in 50.18s**，`$LASTEXITCODE = 0`，全量收集、无 skip／xfail |
| Linux 收官全案（S3-14） | `.venv/bin/python -m pytest tests -q -W error::…`（同口径） | **650 passed**，exit 0（连续两次干净复现） |
| 各任务聚焦命令 | `python.exe -m pytest <聚焦文件> -q -W error::…`（届时 `C:\Temp\fit-agent-win-venv`；Linux `backend/.venv`） | 逐任务计数见 §2；ruff／pyright 于 S3-12／13／14 轮干净 |
| 复审 | 逐任务 reviewer ＋ Windows 结项只读复核 | 各任务 PASS／OK with notes；Windows 结项 **PASS-with-warnings**（无 BLOCKER／MAJOR，2×P2 已修）；S3-11／12／13 复核**未声称最终 PASS** |

## 2. 覆盖对照（子记录）

> 各任务的变更文件与测试文件清单见本文件 git 历史（如 `5675ad3`）与 `git show --stat 94dbc49`，不在此复述；本表只记验证结果、修复链与残留。

| 任务 | 结果（验证了什么） | 验证 → reviewer | 残留 |
| --- | --- | --- | --- |
| S3-01 | 基线核对：迁移止于 004、草稿无 `kind`／计划载荷、`recommendable` 恒 0、`domain/{plan,records,stats}` 仅占位、D1–D9 已收口 | 全量 415 passed，exit 0（HEAD `5a30760`）→ PASS | 无 |
| S3-02 | 迁移 005–007：计划三表＋草稿 `kind`／计划载荷／档案补丁列，24 项置 `recommendable=1` | 聚焦 13 → PASS | 指针归 S3-06、安排载荷列归 S3-08（后均落地） |
| S3-03 | D9 payload、目录词表映射、PPL 模板＋器械／限制过滤、D3 校准（无猜重）、缺档案／红旗 fail-closed、`[starts_on, review_on)` 投影 | 聚焦 26 → 首轮 P1（非 7 日循环频率未折算）已修 → PASS | 落盘／读取接线归 S3-04/06（后均落地）；清单外身体情况澄清归 Stage 4 |
| S3-04 | 同一快照生成输入、Pending 计划草稿（含档案补丁）、按身份／会话查询、结构化 Diff、替换取消预览（仅拟议） | 聚焦 14、kind 联测 56 → 4×P1（kind 分派、Diff 完整处方、补丁后条件复查、`template_key`）已修 → PASS | 指针与确认事务归 S3-06；纠错／丢弃归 S3-05；DTO 归 S3-14（后均落地） |
| S3-05 | 纠错仅改日期与 payload 已拍可变字段（逐层白名单）；按当刻目录＋补丁后条件全量复查后 revision+1；旧 revision／非法内容零写入；丢弃只改状态、幂等、终态不可纠错 | 聚焦 17 → 1×P1（payload 内白名单未强制）已修 → PASS | 确认与日程原子切换归 S3-06；HTTP 面归 S3-14（后均落地） |
| S3-06 | 计划确认事务：启用 v1＋日程同事务原子写入；替换归档旧版、只取消旧版未来未锁定日程；受限组合补丁后复查＋频率／时长上限；一次确认 `context_version` 恰好 +1；幂等凭据；失败注入全回滚 | 聚焦 15 → PASS（无 P0／P1） | P2×3 → §7（S3-06①②③） |
| S3-07 | 业务日期锁定判定（存储标记∪到期规则、不写标记）；当前／历史计划只读投影；「基于计划的指导」**整份**复核（限制冲突与红旗独立阻断、目录身份缺失 fail-closed、限制未收集只给需澄清项） | 聚焦 10、计划族 95 → PASS（无 P0／P1；复核后由任务侧复跑同口径） | 已拍 A `plan_action_unavailable`；当次条件转递归 S3-14（已落地）；→ §6、§7 |
| S3-08 | 迁移 008 `proposed_arrangement_json`；完整目标快照；确认同事务追加修订＋真实 `accepted_at`（入口无调用方时间参数）、`context_version`+1、幂等；修复轮（已拍 B）调整只许更安全方向（组次只减、RIR 只增、无基线不新造） | 修复轮聚焦 17、计划族 105（修复前 13／117／523；565 含同工作区 S3-09 新用例）→ 修复轮无 P0／P1（独立复核确认方向约束与错误类；旧事务／幂等未变） | ②③④⑤⑦⑧ → §6、§7 |
| S3-09 | 迁移 009 记录四表（`training_sessions`／`session_revisions`／`exercise_logs`／`training_sets`）＋最小索引；`record_type` 取**目录词表**（拍板 A：`reps_weight` 等，处方措辞经映射）；辅助／热身／RIR 可空且 NULL＝未明确；负重原文＋单位＋换算整数键（lb×0.45359237→×1000 HALF_EVEN）；`load_notation` 在 `exercise_logs`、复用目录五种；当前修订指针复合外键 | 聚焦 21、联测 73 → PASS（无 P0／P1；OK with notes） | ②③⑤⑥ → §7（② 由 S3-10 定为仅 `timestamp`，⑤ P2 已修） |
| S3-10 | 记录草稿准备／创建／持久化查询／后端现算 Diff／纠错：归属与安排关联显式、不推断（安排关联须为已接受修订且 `target_item_key` 对应）；状态由事实完整性派生；纠错白名单＋CAS；不含确认落盘／统计（归 S3-11/12）；含 S3-09 P2 最小修 | 聚焦 15、联测 114 → PASS（无 P0／P1；OK with notes） | ②④⑤ → §7；⑦ 已修（S3-11）；⑧ 已收口；⑨ 归 S3-14 |
| S3-11 | 记录确认／更正／作废：单事务新建或追加身份／修订、原子切换当前指针；每次确认 `context_version` 恰好 +1；幂等按 `source_draft_id` 读回首笔；作废只改状态不回退；关 S3-10⑦；辅助口径按 01 1.4 原样落盘（未提辅助保持 NULL，不自动物化 `none`） | 聚焦 9、记录四文件 47、联测 77；复核后 ruff／pyright 复现 → 独立只读复核 OK with notes（无 P0／P1；1×P2 表述已改） | ① 归 S3-12（已落地）；④ → §7；⑤ → §6 |
| S3-12 | 统计确定性计算：迁移 010 `pr_candidates` 视图（当前修订、`valid`、排除回归／热身／`assisted`、负重次数明确、词表 `reps_weight`；NULL 辅助按独立完成）；完成率 Wn、三桶、PR 现算；修复 P2：非 `assisted` 携带 `assisted_reps` 即拒＋迁移 012 视图兜底 | 聚焦 35、联测 197；ruff／pyright 干净 → reviewer OK（无 P0／P1；P2×2 report-only） | ②⑥ → §7；⑦ 已修并迁移 012 兜底 |
| S3-13 | 复盘存取与 stale：迁移 011 `reviews`＋`review_source_revisions`（不可改写正文＋快照＋精确来源修订引用）；`app/review_store.py` 唯一保存 seam；stale 读时现算；重生成追加不覆盖；当前统计不读快照 | 聚焦 24、联测 143；ruff／pyright 干净 → reviewer OK with notes（无 P0／P1；1×P2 report-only） | ①②⑧ → §7 |
| S3-14 | 业务 API 与契约交接：17 个端点（只读计划／记录／统计／复盘＋草稿 revise／confirm／discard／void、guidance 当次条件转递）；S2-07 错误形状与 Host／Origin 不变；旁路扫描无建草稿／重算／直写／Run／SSE | Linux 聚焦 16、联测 173、全案 650（两次）；**Windows 结项 658 passed in 50.18s（`94dbc49`）**；ruff／pyright 干净 → Windows 结项只读复核 PASS-with-warnings | ③ → §7；⑧ → §5；⑨⑩ 已修；→ §6、§7 |

## 3. 领域／存储要点（S3-02–S3-13）

- **迁移与事务**：`005`–`012`（计划三表＋草稿列、安排载荷、记录四表、统计视图、复盘两表；012 为 `pr_candidates` 修复视图）；未改 `001`–`004`；`load_migrations` 要求编号自 1 连续。一次成功确认恰好 `context_version` +1；版本／日程／修订／当前指针／草稿 Committed／凭据同事务同成败。
- **计划／安全**：确认只追加 `plan_versions`，当前计划＝最新 `version` 等价唯一指针（无 `current_plan_version_id`，见 §7）；替换只取消旧版**未来未锁定**日程，日程按 `[starts_on, review_on)` 投影（替换时新版本仍投影出已过去日期，S3-12 按同一到期规则计入分母——S3-06②）。所有「基于计划的指导」经 `PlanReadService` 整份复核，限制冲突与红旗独立阻断，目录身份缺失 fail-closed 为 `plan_action_unavailable`（已拍 A），`usable=false` 不给任何处方。
- **记录**：只追加（唯一 UPDATE 是 `training_sessions.current_revision_id`），无触发器（报告允许「明确写入路径＋必要触发器」二选一）；词表用目录三类、处方侧经 `CATALOG_RECORD_TYPE_TO_PRESCRIPTION` 映射；NULL＝未明确；非 `reps_weight` 拒组级 `load`，非 `assisted` 拒 `assisted_reps`。
- **统计**：`pr_candidates` 只取当前修订、`status='valid'`，排除回归／热身／`assisted` 与非明确负重次数；未申报辅助（NULL）按独立完成；完成率分母＝周窗∩未取消∩有效锁定（存储标记∪到期规则）、分子按日程去重、分母为 0 返回 `None`；三桶纯函数（已知未满足优先 → 缺必要判定事实待补全 → 全部满足）；PR 现算不落结果表。
- **复盘／安排**：复盘正文不可改写＋快照＋精确来源修订引用，stale 读时现算，重生成追加，保存不推进 `context_version`。安排确认同事务追加完整目标快照＋真实 `accepted_at`（入口无调用方时间参数），不写 `plan_versions`／`scheduled_sessions`／档案；锁定与接受分离（已锁定仍可接受、已取消拒结）；同一日程「当前安排」＝最大 `revision_no`；调整只许更安全方向（已拍 B）。

## 4. 已接线端点（S3-14）

17 个（含 S2 的 `GET /api/profile`，未扩展计划投影；完整清单与语义见 `stage3-handover.md` §1）：`GET /api/profile`（S2-07 原样，Stage 2 用例逐字未改）；`/api/plan`、`/api/plans/{plan_version_id}`、`/api/plan/guidance` → `PlanReadService`（`api/` 不持 SQL、不重建投影）；`/api/records`、`/api/records/{session_id}`、`/api/records/{session_id}/judgement` → `RecordReadService`／`StatsService.judge_session`；`/api/stats/completion`、`/api/stats/pr` → `StatsService`（现算）；`/api/reviews`、`/api/reviews/{review_id}` → `ReviewStore`；`/api/sessions/{id}/drafts`、`/api/drafts/{draft_id}` → 各 kind 查询入口（返回全部 kind）；`POST /api/drafts/{id}/revise`、`confirm`、`void`、`discard` → 按 kind 分派，`void` 仅记录草稿。未提供（旁路扫描断言 404/405）：公开建草稿 `POST /api/drafts`、`/recalc`、正式事实直写、`/api/runs`、`/api/chat`、`/api/models`、`/api/events`。

## 5. DTO／错误／前端待改（要点）

错误形状沿用 S2-07 `{http_status, error_code, message, detail?}`，不新增业务 error_code；`InvalidRecordFact`→422，缺参→400 统一形状（S3-14 修复⑨）。`GET /api/plan/guidance` 的 `safety`：`usable=false` 时不给任何处方；`block_code=plan_action_unavailable` 为具体用户可见阻断（已拍 A）；`?arrangement_revision_id=` 按该安排绑定的计划版本复核。前端待改 F1–F10（`/api/profile` 不扩展、`plan`／`arrangement` kind、revise 带 `revision`、确认凭据字段、`/api/stats` 拆为两现算端点、`/api/review`→`/api/reviews`、`DraftStatus.stale` 非草稿状态等）见 `stage3-handover.md` §3；前端本轮未改。

## 6. Stage 4 接入约束（已拍与交接）

- **已拍 A（2026-09-11）**：计划引用的目录身份缺失 → 对用户给具体阻断 `plan_action_unavailable`、不给基于计划的指导（不降级「需澄清」、不当作「无冲突」）。
- **已拍 B（2026-09-12）**：当次安排只能向更安全方向调——`work_sets` 只减不增、`target_rir` 只增不减、计划无 RIR 时不新造；仅这两字段可调、需非空白原因。
- **记录绑定**：统计／记录必须绑定**执行时所依据**的 `arrangement_revision_id`（当前安排＝最大 `revision_no`），不得绑「最新」。
- **辅助口径**：未提辅助保持 NULL（009：NULL＝尚未明确）；「非 `assisted` 即独立完成」由 S3-12 视图落地；确认按 01 1.4 原样落盘，不自动物化 `none`；字面物化（确认时写 `none`）属产品级选择、未拍，若改需 S3-14 HTTP 契约变更。
- **写入边界**：草稿创建只在内部应用层；Stage 4 对话／工具层复用 Stage 3 revise／confirm／discard／void，不新增 HTTP 建草稿入口；一键重算（01 1.6）未实现。
- **安全复核**：任何指导必须走 `PlanReadService` 并尊重 `usable=false`／`block_code`；`evaluate_plan_safety(session_exercise_ids=)` 已可转递当次条件。
- **业务日期／版本**：确认与到期锁定按固定业务时区（`api/deps.current_business_date`）；每次确认 +1，前端按 409 `draft_stale` 重备草稿。**复盘正文生成**归 Stage 4（显式请求 D6），经 `ReviewStore.save_review` 保存；HTTP 面仅查询。

## 7. 开放残留（不得当成已验证）

- **S3-06**：① 当前计划＝最新 `version` 等价指针未建列，后续写入路径必须继续守等价性；② 替换时 `starts_on` 可早于当刻 → 过去日期名额已在 S3-12 收口（同一到期规则）；③ `_seed_confirmed_plan` 等合成夹具宜改经 `confirm_plan_draft`。
- **S3-07**：① `PlanReadService` 当时唯一调用方是测试（04 验收 7 在应用层取证）；S3-14 不得在 `api/` 重建投影（已遵守）；② 未建 `current_plan_version_id`（等价不变式未被证明不足）；④ 当次条件已由 S3-14 接线。
- **S3-08②／S3-14③**：安排草稿纠错／丢弃 HTTP 面未做；**S3-08③**：接受时不做限制／红旗复核（04 4.3 定在「使用时」）；**S3-08④**：安排不做计划级频率／时长／器械上限复查（属计划版本约束）；**S3-08⑤**：空白 `adjustment_reason` 已修为 `InvalidArrangementTarget`（有专门用例），`require_arrangement_binding` 绑定不一致分支无测试（仅损坏态可达，报告项非缺陷）。
- **S3-09**：② `time_precision` 现仅 `timestamp`（S3-10 定），未加 CHECK，扩取值前先定词表；③ 库层不可变性取「明确写入路径」口径、无触发器；⑥ `previous_revision_id` 未加复合外键（防御性、按需零成本补），数值列 CHECK 不拒非数值文本（SQLite 亲和性），主防线在服务端／领域层。
- **S3-10②／S3-11⑥**：草稿测试的原始 SQL 夹具宜改经真实确认链路；丢弃入口未做（repo 条件更新可用）；**S3-10④**：更正可改日期但不重查同日身份（归属显式锁定；如需按新日期复查，最小加测 `prepare_input(new_date)`）；**S3-10⑤**：`target_set_key` 无对应目标组身份，仅校验 `target_item_key`；**S3-11④**：一次确认 +1 ⇒ 多条草稿先备后确认时第二条 `DraftStale`（前端按 409 重备）。
- **S3-12**：② `locked_at` 无生产写入方（当前等价到期规则；未来若出现提前写标记的路径会改变分母）；③ 跨版本不迁移由构造保证（按版本＋`starts_on` 派生 Wn）；④ 三桶读不到安排修订抛 `InvalidPlanRow`（数据损坏，非降级）；⑤ `LATER_STAGE_TABLES` 断言按 `type='table'` 过滤，字面含已建对象（S3-14 接 API 时可收口）；⑥ 完成率分子取分母集合内（口径选择；若改拍字面读法，改 `domain/stats/service.py` 计数范围＋固定时钟用例）。
- **S3-13**：① 未采纳 spike 的 `plan_version_id`／`week_no` 等字段，复盘范围口径未拍；② 生成后新增同范围训练不使旧复盘 stale（已知上界）；⑥ `list_reviews` 按 `(generated_at, id)` 排序（依赖追加不覆盖，非严格时间序）；⑦ pi-lens 对未跟踪新文件的 import 误报已取证为假阳性，未据此改代码；⑧ 四处 `LATER_STAGE_TABLES` 恒真断言待清理（P2、行为无影响）。
- **S3-14⑧**：`/api/stats` 聚合形状未提供（前端聚合或后续拍板，→ handover F7）。
- **Windows 结项只读审查 MINOR×3（不阻断）**：`source_draft_id` 无库层 UNIQUE；安排草稿 revise／discard 复用档案入口；迁移 012／010 视图 SQL 重复。
- **设计变更待实现**：努力程度改为仅处方字段（2026-09-12）——记录侧 RIR 字段／判定现状为变更前；实现落地后须追加新行复核 S3-08/09/10/12 相关行。
- **未验**：真实端到端验收；Stage 4（Agent／聊天／Run／SSE／重算）未接入；前端未切真轨（F1–F10 待收口）。
- 已知非回归抖动：`tests/test_app.py::test_lifespan_runtime_exception_closes_connection` 曾单次失败，隔离与复跑均过（S3-08⑧／S3-14 已记录），Windows 结项全量 658 通过。

**声明**：结项只表示「Stage 3 领域（计划／记录／统计／复盘）达到本阶段门槛」；离线自动化通过 ≠ 产品端到端验收。Windows 结项证据见 §1／§2 的 S3-14 行。

**追加行**：后续子任务在本文件 §2 表尾追加一行（`任务 | 结果 | 验证 → reviewer | 残留`），不新建报告、不复制 `stage3.md` 契约。
