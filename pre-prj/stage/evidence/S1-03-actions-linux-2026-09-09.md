# S1-03 动作目录与受检种子（Linux/WSL2）

> 子任务：Stage 1 S1-03。范围：动作目录结构校验、按身份读取、别名候选查询、推荐候选筛选、停用保留；核心种子按已拍清单与来源录入。
> 只写 `backend/**` 与 `pre-prj/stage/evidence/**`；未改 001/002 迁移、`frontend/**`、PLAN、设计正本、stage1.md；无 `git add`/commit/stash；未新增依赖；未下载或写入任何媒体文件/字段。
> 验收对照：stage1.md §5 S1-03 验收 1–5；口径依据 stage1.md §5「已确认的来源、首批范围与维护方式／记录类型／负重口径／动作模式映射」、§4、§7、architecture 03 章 3.1–3.2、design-decisions「架构不变量」。

## 0. 结论摘要

- 已拍 **24 项**清单全部核上来源并导入；原候选「哑铃分腿蹲」在数据集中无可靠对应，经 **2026-09-09 人拍方案 C 移出清单**（见第 3、4 节）。
- 种子经编号迁移 `003_stage1_action_seed.sql` 写入：**只 INSERT**（无 UPDATE/DELETE/CREATE/DROP/ALTER/PRAGMA），不建表、不写用户事实、不预填媒体字段；`exercises` 24 行、`user_profile=(1, NULL, 0)`、`user_version=3`。
- 全部种子 `recommendable=0`（未通过可推荐检查）、`active=1`；推荐候选筛选对产品种子返回空集，受检标记逻辑用测试虚构动作验证。
- 共享契约（13 项模式词表 + 24 项「动作→模式集合」）以 Python 常量暴露，导入路径见第 6 节。
- 全量自动化：**110 passed，退出码 0**（S1-02 基线 81 + 新增 29）。

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `backend/storage/migrations/003_stage1_action_seed.sql` | 24 行精选种子，只 INSERT；每行附数据集来源注释 |
| `backend/domain/actions/rules.py` | 13 项模式词表、24 项清单原词、动作→模式映射、三类记录口径、五种负重口径、单侧口径校验 |
| `backend/domain/actions/schema.py` | `Exercise`（身份）、`AliasCandidate`、`AliasResolution`（保留全部候选） |
| `backend/domain/actions/repo.py` | 唯一 SQL 面：按 ID／标准名读取、别名精确候选、推荐候选、全量读取 |
| `backend/domain/actions/service.py` | `ActionCatalogService`：`get_by_id` / `resolve` / `recommendation_candidates` |
| `backend/tests/test_stage1_actions_rules.py` | 词表与口径契约（9 用例） |
| `backend/tests/test_stage1_actions_seed.py` | 逐项核对表、只 INSERT、编号迁移增量维护、重复导入原子回滚（10 用例） |
| `backend/tests/test_stage1_actions_catalog.py` | 别名候选／歧义、变式区分、推荐筛选、停用保留、版本不推进（10 用例） |
| `backend/tests/test_stage1_schema.py` | 既有断言按真实种子条数更新（第 8 节逐处说明） |

## 2. 已拍口径落地对照

- **记录口径**：`reps_bodyweight` 3 项（自重引体向上、自重双杠臂屈伸、悬垂举腿），其余 21 项 `reps_weight`；首批无 `time` 动作（类型保留）；无 `reps_assisted`（不新增辅助负重型）。
- **负重口径**（仅 `reps_weight` 非空，自重型恒 `NULL`）：`barbell_includes_bar_total` 5 项、`dumbbell_per_hand` 8 项、`machine_pin_displayed_value` 7 项、`plate_loaded_total_excluding_empty` 1 项（45°腿举）；`unilateral_setting_per_side` 本批无条目（清单内无单侧器械动作）。
- **单侧／每侧次数**：`保加利亚分腿蹲`、`单臂哑铃划船` `unilateral=1`（哑铃口径仍为每只重量）。
- **模式**：13 词表逐条落地；多归属仅两处（哑铃上斜卧推＝水平推＋垂直推；自重双杠臂屈伸＝垂直推＋肘伸）。
- **可推荐**：全部 0；`active` 全部 1；停用只由后续业务置 0，种子不重置。

## 3. 逐项来源核对表（核对依据＝数据集 instructions.zh / equipment / target 与动作定义一致，非名称相似）

| # | 清单原词（中文标准名） | 数据集来源 | equipment | target | 记录口径 | 负重口径 | 单侧 | 模式 | 核对依据（zh 说明要点） | 结论 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 杠铃背蹲 | 0043 barbell full squat | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 深蹲 | 杠铃置于上背部／斜方肌后束，屈膝屈髋下蹲——与「背蹲」定义一致 | 导入 |
| 2 | 杠铃传统硬拉 | 0032 barbell deadlift | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 髋铰链 | 杠铃置于身前地面，屈膝髋铰链、正手握距略宽于肩、起立伸髋伸膝——传统硬拉 | 导入 |
| 3 | 杠铃罗马尼亚硬拉 | 0085 barbell romanian deadlift | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 髋铰链 | 膝微屈、髋铰链下放至腘绳肌拉伸后伸髋——罗马尼亚硬拉 | 导入 |
| 4 | 45°腿举 | 0739 sled 45° leg press | sled machine | glutes | reps_weight | plate_loaded_total_excluding_empty | 0 | 深蹲 | 坐于雪橇机、双脚踩踏板蹬伸——45°腿举；挂片类单负载器械 | 导入 |
| 5 | 哑铃分腿蹲（**已移出清单**） | — | — | — | — | — | — | — | 数据集无「哑铃＋分腿蹲（后脚落地）」条目；0410 为后脚抬高（保加利亚式）、2812 踏步、0411 单腿蹲、0336 箭步蹲 | **2026-09-09 拍定方案 C：移出清单** |
| 6 | 保加利亚分腿蹲 | 0410 dumbbell single leg split squat | dumbbell | quads | reps_weight | dumbbell_per_hand | 1 | 深蹲 | 每手一哑铃，前脚平放地面、**后脚抬高在长凳或台阶**——保加利亚分腿蹲定义 | 导入 |
| 7 | 杠铃平板卧推 | 0025 barbell bench press | barbell | pectorals | reps_weight | barbell_includes_bar_total | 0 | 水平推 | 平躺长凳、杠铃下放至胸部再推起——平板卧推 | 导入 |
| 8 | 哑铃平板卧推 | 0289 dumbbell bench press | dumbbell | pectorals | reps_weight | dumbbell_per_hand | 0 | 水平推 | 平躺长凳、双手各一哑铃至胸侧推起——平板哑铃卧推 | 导入 |
| 9 | 哑铃上斜卧推 | 0314 dumbbell incline bench press | dumbbell | pectorals | reps_weight | dumbbell_per_hand | 0 | 水平推＋垂直推 | 45° 上斜凳、双手各一哑铃推起——上斜哑铃卧推（跨界高复合型，多归属） | 导入 |
| 10 | 坐姿哑铃肩推 | 0405 dumbbell seated shoulder press | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 垂直推 | 坐姿、哑铃从肩高推至头顶上方——坐姿哑铃肩推 | 导入 |
| 11 | 哑铃侧平举 | 0334 dumbbell lateral raise | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 肩孤立 | 站姿双手各一哑铃、手臂向两侧抬至与地面平行——侧平举 | 导入 |
| 12 | 哑铃反向飞鸟 | 0383 dumbbell reverse fly | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 肩孤立 | 髋部前倾、双臂向两侧抬起至与地面平行——反向飞鸟 | 导入 |
| 13 | 杠铃俯身划船 | 0027 barbell bent over row | barbell | upper back | reps_weight | barbell_includes_bar_total | 0 | 水平拉 | 俯身、正手握杠铃拉向下胸——俯身划船 | 导入 |
| 14 | 坐姿绳索划船 | 0861 cable seated row | cable | upper back | reps_weight | machine_pin_displayed_value | 0 | 水平拉 | 坐于电缆划船机、手柄拉向身体——坐姿绳索划船 | 导入 |
| 15 | 单臂哑铃划船 | 0292 dumbbell one arm bent-over row | dumbbell | upper back | reps_weight | dumbbell_per_hand | 1 | 水平拉 | 单手哑铃、俯身将哑铃拉向胸部、换边——单臂哑铃划船 | 导入 |
| 16 | 高位下拉 | 0198 cable pulldown | cable | lats | reps_weight | machine_pin_displayed_value | 0 | 垂直拉 | 坐于下拉机、宽握正手将杆拉向胸部——高位下拉 | 导入 |
| 17 | 自重引体向上 | 0652 pull-up | body weight | lats | reps_bodyweight | — | 0 | 垂直拉 | 悬挂单杠、正手将胸部拉向杠——自重引体向上 | 导入 |
| 18 | 坐姿腿弯举 | 0599 lever seated leg curl | leverage machine | hamstrings | reps_weight | machine_pin_displayed_value | 0 | 膝屈 | 坐姿器械、小腿置于垫下屈膝——坐姿腿弯举 | 导入 |
| 19 | 腿屈伸 | 0585 lever leg extension | leverage machine | quads | reps_weight | machine_pin_displayed_value | 0 | 膝伸 | 坐姿器械、伸膝抬起脚垫——腿屈伸 | 导入 |
| 20 | 器械站姿提踵 | 0605 lever standing calf raise | leverage machine | calves | reps_weight | machine_pin_displayed_value | 0 | 小腿（踝跖屈） | 站姿器械、肩垫负重下提踵——站姿提踵 | 导入 |
| 21 | 哑铃弯举 | 0294 dumbbell biceps curl | dumbbell | biceps | reps_weight | dumbbell_per_hand | 0 | 肘屈 | 站姿双手各一哑铃、屈肘至肩高——哑铃弯举 | 导入 |
| 22 | 绳索下压 | 0201 cable pushdown | cable | triceps | reps_weight | machine_pin_displayed_value | 0 | 肘伸 | 高滑轮直杆、上臂固定伸肘下压——绳索下压 | 导入 |
| 23 | 绳索过顶臂屈伸 | 0194 cable overhead triceps extension (rope attachment) | cable | triceps | reps_weight | machine_pin_displayed_value | 0 | 肘伸 | 绳索过顶、上臂靠头伸肘——过顶臂屈伸 | 导入 |
| 24 | 自重双杠臂屈伸 | 0251 chest dip | body weight | pectorals | reps_bodyweight | — | 0 | 垂直推＋肘伸 | 双手支撑于**双杠**、屈肘下降再推起——双杠臂屈伸（多归属） | 导入 |
| 25 | 悬垂举腿 | 0472 hanging leg raise | body weight | abs | reps_bodyweight | — | 0 | 核心 | 悬挂单杠、伸直双腿抬至与地面平行——悬垂举腿 | 导入 |

> 注：自重次数型 3 项（自重引体向上、自重双杠臂屈伸、悬垂举腿）的负重口径均为 `NULL`（见第 2 节）。

- 每行 `source_ref = exercises-dataset:<id>`、`attribution = "© Gym visual — https://gymvisual.com/"`（逐行保留）；`instructions_zh` 使用数据集中文说明。
- 别名＝数据集英文 `name`（0739 另存规范化写法，见第 4 节）；中文口语别名不入库（第 5 节）。

## 4. 缺项与歧义表

| # | 事项 | 事实 | 处置 |
|---|---|---|---|
| 1 | **哑铃分腿蹲核不上来源（已拍 C 移出）** | 数据集无「哑铃＋分腿蹲（后脚落地）」条目；0410 为后脚抬高（保加利亚式）、2812 踏步、0411 单腿蹲、0336 箭步蹲、2368 自身体重分腿蹲、2810 杠铃分腿蹲 v.2 | **2026-09-09 人拍方案 C：从首批清单移除**，清单 24 项；已同步改「深蹲」模式行与共享词表 |
| 2 | 杠铃背蹲的候选条目 | 0043 barbell full squat（通用背蹲）、1436 high bar、1435 low bar 均为杠铃背蹲系 | 取**通用条目 0043**（清单原词未限定高／低杠，通用名对通用条目）；1436/1435 视为更细变式，不入本批 |
| 3 | 45°腿举源数据名称乱码 | 0739 `name` 为 `sled 45в° leg press`（`в°` 非 `°`）；1463/1464 为同一动作的 side/back POV | 别名同时登记规范化 `sled 45° leg press` 与源文件原样；`source_ref` 仍指向 0739；POV 重复条目不导入 |
| 4 | 高位下拉候选条目 | 0198 cable pulldown（通用）、0197 pro lat bar、2330 full ROM、0818 twin handle、0007 alternate、0150 bar lateral | 取通用 0198；其余为附件／握法变式 |
| 5 | 坐姿绳索划船候选条目 | 0861 cable seated row（通用）、1323 rope seated row、0180 low seated row、0239 straight back | 取通用 0861；1323 为绳索附件变式 |
| 6 | 绳索下压候选条目 | 0201 cable pushdown（通用）、0200 rope、0241 v-bar、0207 reverse-grip | 取通用 0201；其余为附件／握法变式 |
| 7 | 自重双杠臂屈伸候选条目 | 0251 chest dip（双杠）、2363 wide-grip 双杠、2462 straight bar、0814 triceps dip（**长凳屈伸**，非双杠） | 取 0251（zh 说明明确「双杠」）；0814 非双杠，不构成歧义 |
| 8 | `leverage machine` 的插销／挂片归属 | 数据集 `equipment` 只到「leverage machine」，未区分插销配重与挂片 | 按已拍口径把 3 项器械动作（坐姿腿弯举／腿屈伸／器械站姿提踵）记为 `machine_pin_displayed_value`（插销配重标示值）；若实际器械为挂片需改口径，列入 needsDecision 与残留风险 |
| 9 | 中文口语别名 | 见第 5 节 | 只出提议表，不入库；其中「划船」「分腿蹲」会命中多个身份，建议人工澄清后再定 |

## 5. 待人工确认别名提议表（未入库）

| 提议别名（中文口语） | 拟对应标准名 | 风险／备注 |
|---|---|---|
| 深蹲、背蹲 | 杠铃背蹲 | 「深蹲」亦可能指 45°腿举/分腿蹲，建议限定为「杠铃深蹲」 |
| 硬拉 | 杠铃传统硬拉 | 与罗马尼亚硬拉、直腿硬拉需区分 |
| RDL、罗马尼亚硬拉 | 杠铃罗马尼亚硬拉 | 低风险 |
| 腿举、倒蹬 | 45°腿举 | 低风险 |
| 分腿蹲 | 保加利亚分腿蹲 | 哑铃分腿蹲已移出清单；建议人工限定为「保加利亚分腿蹲」 |
| 卧推、平板卧推 | 杠铃平板卧推 | 哑铃平板卧推需区分 |
| 上斜卧推 | 哑铃上斜卧推 | 杠铃上斜不在本批 |
| 肩推、推举 | 坐姿哑铃肩推 | 站姿/杠铃需区分 |
| 侧平举 | 哑铃侧平举 | 低风险 |
| 反向飞鸟、后束飞鸟 | 哑铃反向飞鸟 | 低风险 |
| 划船、俯身划船 | 杠铃俯身划船 / 坐姿绳索划船 / 单臂哑铃划船 | **歧义**：三个身份，需人工澄清 |
| 下拉、高位下拉 | 高位下拉 | 低风险 |
| 引体、引体向上 | 自重引体向上 | 低风险 |
| 腿弯举 | 坐姿腿弯举 | 俯卧腿弯举不在本批 |
| 腿屈伸 | 腿屈伸 | 低风险 |
| 提踵 | 器械站姿提踵 | 坐姿提踵不在本批 |
| 弯举、二头弯举 | 哑铃弯举 | 杠铃/EZ 弯举需区分 |
| 三头下压、下压 | 绳索下压 | 低风险 |
| 过顶臂屈伸 | 绳索过顶臂屈伸 | 低风险 |
| 双杠臂屈伸、双杠撑 | 自重双杠臂屈伸 | 长凳臂屈伸不是同一动作，不得合并 |
| 悬垂举腿、吊杠举腿 | 悬垂举腿 | 低风险 |

> 已拍：中文口语别名「暂不入库，待人工确认」（stage1.md §9.1 第 3 条）。入库别名仅为数据集英文 `name`（测试断言别名不含 CJK 字符）。

## 6. 共享契约导入路径（S1-04／S1-05 复用）

| 契约 | 导入路径 | 说明 |
|---|---|---|
| 13 项模式词表 | `from domain.actions.rules import MODE_VOCABULARY` | 限制判定的被限制模式集合取值域 |
| 24 项清单原词 | `from domain.actions.rules import CATALOG_STANDARD_NAMES` | 已拍清单（2026-09-09 移除哑铃分腿蹲） |
| 动作→模式集合 | `from domain.actions.rules import EXERCISE_MODES, modes_for` | 多归属仅两处；未知名抛 `UnknownCatalogExercise` |
| 模式集合校验 | `from domain.actions.rules import validate_modes, InvalidMode` | 非空且全部在词表内 |
| 三类记录口径 | `from domain.actions.rules import RECORD_TYPES` | 不新增辅助负重型 |
| 五种负重口径 | `from domain.actions.rules import LOAD_CONVENTIONS` | 与 002 迁移 CHECK 一致 |
| 单侧口径 | `from domain.actions.rules import UNILATERAL_STANDARD_NAMES, is_unilateral` | 每侧次数口径 |
| 目录查询 | `from domain.actions.service import ActionCatalogService` | `get_by_id` / `resolve` / `recommendation_candidates` |
| 目录读取（底层） | `from domain.actions.repo import ExerciseRepo` | 唯一 SQL 面 |

> S1-05 的限制判定口径为「动作的模式集合 ∩ 被限制模式集合 ≠ ∅ 即阻断」；本模块只提供集合，判定函数归 S1-05。

## 7. 实测命令与结果

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning`（改动前基线） | 0 | `81 passed in 8.41s` |
| 2 | 同上（改动后） | 0 | `110 passed in 4.86s`（新增 29：rules 9 + seed 10 + catalog 10） |
| 3 | 临时库探针（`storage.db.Database` + `ExerciseRepo` + `ActionCatalogService`） | 0 | `migrate_returns=3`、`user_version=3`、`exercise_count=24`、`profile_row=[(1, None, 0)]`、`recommendable_count=0`；`resolve("sled 45в° leg press")→[('leg-press-45','alias')]`；`resolve("哑铃分腿蹲")→()` |
| 4 | 003 文本结构检查（去注释后统计） | 0 | 仅含 1 条 `INSERT INTO exercises`；无 UPDATE/DELETE/CREATE/DROP/ALTER/PRAGMA；无 `user_profile` |
| 5 | `git diff --cached --name-only` / `git status --porcelain -- backend` | 0 | 暂存区为空；`backend/**` 改动均为本任务文件 |
| 6 | 数据集只读提取（python 解析 `exercises.json`，不 cat、不拷贝、不下载） | 0 | 1324 条；24 项来源逐条打印 instructions.zh 核对 |

## 8. 既有测试断言更新说明（`tests/test_stage1_schema.py`，按事实更新，未删／未跳过／未放宽为无意义）

| 位置 | 原断言／写法 | 现写法 | 理由 |
|---|---|---|---|
| 模块常量 | 无种子条数常量 | 新增 `SEEDED_EXERCISE_COUNT = 24` | 003 种子条数为活动事实，集中一处 |
| `test_fresh_database_migrates_to_latest_...`（原 `..._without_seed_or_profile_facts`，已改名） | `exercises == 0`，注释「不导入动作种子」 | `exercises == SEEDED_EXERCISE_COUNT` | 003 已导入种子；仍断言无用户事实 |
| `test_stage0_upgrade_preserves_...` | 升级后 `exercises == 0` | `== SEEDED_EXERCISE_COUNT` | 升级会补跑 002+003，种子随迁移写入 |
| `test_repeated_start_does_not_reset_...` | 插入样本行后 `exercises == 1` | `== SEEDED_EXERCISE_COUNT + 1` | 重复启动不重跑种子，样本行叠加 |
| 4 处 `_exercise_params(..., "杠铃背蹲")`、1 处 `"自重引体向上"` | 与产品种子标准名同名（`standard_name_zh` UNIQUE） | 改为测试虚构名 `示例动作-杠铃`／`示例动作-自重` | 避免测试样本冒充产品种子，也避免 UNIQUE 冲突 |
| 其余断言 | 不变 | 不变 | 表集合、`record_type` 三类、负重口径、停用不删除、版本不推进等语义未变 |

> 未改 `test_migrations.py`、`test_provider_settings.py`、`test_db.py`、`test_run_repo.py`、`test_app.py`、`test_config.py`、`test_timezone.py`：003 不新增表、不改变迁移编号连续性，其断言（`len(load_migrations())` 推导版本、表集合精确相等、凭据扫描面）继续成立。

## 9. 未覆盖范围与残留风险

- **Windows 未实测**：本文件全部为 Linux/WSL2 证据；结项门槛（方案 A）要求同版本 Windows 全量自动化 + 隔离库人工实测。
- **哑铃分腿蹲已移出清单**：2026-09-09 拍定方案 C；清单 24 项全部导入。`modes_for("哑铃分腿蹲")` 现抛 `UnknownCatalogExercise`（不再按已拍映射），`resolve` 返回空。
- **可推荐检查未完成**：24 项 `recommendable=0`；「可推荐检查」的标准与执行人未拍板，Agent 主动推荐当前对产品种子为空集（符合「未检查不得推荐」，但不等于目录已可用于计划生成）。
- **插销／挂片推断**：`leverage machine` 三项按插销配重口径记录；若实际器械为挂片，需改口径（第 4 节 #8）。
- **别名覆盖有限**：仅数据集英文名（+0739 规范化写法）；中文口语别名待人工确认，未入库。
- **无打卡匹配交互**：本层只提供候选集合与歧义标记，不实现打卡询问、确认或写入。
- **未接 HTTP／Agent／CLI**：无只读 API、无前端接口形状；`domain/actions` 未被 `app/`、`runtime/` 引用。
- **数据来源版权**：文字数据来自 `exercises-dataset`，逐行保留 attribution；媒体（图片/GIF）未导入、未下载、未建字段。
