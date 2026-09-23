# 目录证据与动作解释规则（exercise-guidance reference）

本文件随 exercise-guidance 正文一起加载，规定动作回答的证据来源、检索与口径解释、未知处理与安全
边界。判定权属于当前源码与确定性规则：本文件只给解释口径，不复算、不新增业务阈值。

## 1. 目录证据：`search_exercises` 的严格输出

General 分支能读动作目录的 Tool 只有 `search_exercises`。模型参数为 `query`、`muscle_groups`、
`equipment`、`movement_patterns`、`limit`，至少提供一个检索条件。返回对象包含 `exercises` 与
`returned`；每条动作包含以下字段：

- `exercise_id`：canonical 稳定身份。
- `standard_name_zh`、`name_en`、`aliases`：中英文名称与别名。
- `equipment`、`equipment_zh`：数据集器械规范值及中文标签。
- `muscle_groups`、`muscle_groups_zh`：主肌群与二级肌群的规范值及中文标签。
- `movement_patterns`：canonical 动作模式的规范英文值。
- `record_type`、`load_convention`：记录口径与负重口径。
- `recommendable`：目录是否已核验该动作可用于计划。
- `instructions`：数据集提供的中英指导语；canonical-only 动作可为 `null`。

训练负荷、加重单位、组数、次数区间、训练日、难度分级、替代关系与进阶标准均不在本 Tool 输出中。

## 2. 检索与匹配规则

- query 对 `standard_name_zh`、`aliases` 与 `name_en` 做大小写折叠后的子串匹配。
- 不同 facet 取 AND，同一 facet 的多个值取 OR；limit 在稳定排序后应用。
- 命中集合可以为空：`exercises=[]` 表示目录缺少候选。
- 一次检索的命中可以有多条（同一词根覆盖多个器械变式与角度变体）。多条命中时按用户说的器械与
  体位收敛；收敛到具体某一条才引用它的 id 与口径；用户没给条件时把候选项摆出来询问。
- 别名与英文别名同样参与子串匹配，所以 `"barbell bench press"` 与 `"卧推"` 都能命中同一动作。
- 名称相近的动作在目录里是彼此独立的行，各自有独立口径与 PB：`pull-up`（`reps_bodyweight`）与
  `weighted-pull-up`（`reps_weight`、`external_added_weight`）是两条记录，成绩互不顶替。
- 用户要求的动作查不到时，可以换检索词再查（标准名、别名、器械词），每次都要如实说明新结果是
  一次新检索得到的；名称相近的候选要显式标注为"名称相近、目录里的另一个动作"。

## 3. 记录口径与负重口径

三类记录口径与写入约束（写入侧由 `validate_record_against_exercise` 强制）：

| `record_type` | 记录字段 | 负重口径要求 |
| --- | --- | --- |
| `reps_weight` | `weight_kg` + `reps` | `load_convention` 必须与目录给定的完全一致 |
| `reps_bodyweight` | 只有 `reps` | 目录值为 `null`；写入时携带任何负重口径都被拒绝 |
| `time` | 只有 `duration_seconds` | 目录值为 `null`；写入时携带任何负重口径都被拒绝 |

`load_convention` 的取值域见 `app/domain/actions/schema.py`；下表读法按取值名与写入校验给出，
数字含义以目录返回的口径为准：

| 取值 | 数字表示什么 |
| --- | --- |
| `barbell_includes_bar_total` | 杠铃总重，含杠铃杆 |
| `dumbbell_per_hand` | 单只手里的重量 |
| `machine_pin_displayed_value` | 配重器械显示／插销档位值 |
| `plate_loaded_total_excluding_empty` | 净加载片的总重，空车自重排除在这个数字之外 |
| `unilateral_setting_per_side` | 单侧设置值，按一侧计 |
| `external_added_weight` | 外加重量，自身体重计在这个数字之外 |

- 自重与计时动作的 `load_convention` 为 `null`：这两个口径没有负重可记，也不存在"0kg"这种写法。
- 同一数字在不同负重口径下含义不同，跨口径比较与换算缺少依据，说明口径即可。
- `recommendable` 为 `false` 时说明该动作未被目录核验为可用于计划；禁用与伤病判断依据目录之外的
  事实。

## 4. 未知处理

- Tool 未返回的训练事实按未知处理，说明缺口并交给训练历史、进展或计划流程读取。
- `instructions=null` 表示当前 canonical 动作没有数据集指导语；只提供明确标注的一般教育信息。
- 缺少个体负荷时不给默认重量，不引用其他用户数据，不用示例数值充当事实。
- `muscle_groups` 与 `movement_patterns` 可按返回值解释；替代关系、难度和个体适用性仍需额外事实。

## 5. 动作解释与一般执行要点

- 器械形式按目标、稳定性要求、偏好与疲劳成本选择；固定器械更容易稳定与调重，自由重量需要更多
  协调，两者都能达成训练效果。器械形式本身不表示训练等级。
- 解释执行要点时保持在一般层面：动作路径与关节范围的可控性、稳定与疲劳成本、该口径下如何记录。
  具体幅度、组次与负荷由计划流程与用户实际反馈决定。
- 替代与降阶的一般原则：替换要保持动作模式、目标肌群、专项性或疲劳预算中至少一项主要职责一致；
  Tool 返回的肌群与模式可作目录事实，替代关系的结论仍标注为一般建议。
- 一致性与可重复完成的动作优先于复杂技术动作；讲解技术要点时给出可观察的判断（能否稳定重复、
  是否需要额外稳定条件），标注为主观反馈层面。

## 6. 五条能力边界

1. **安全分流**：急性红旗词表由 `safety_scan` 在 Router、模型与 Tool 之前判定，命中即固定安全消息并
   终止本次 Run。本 Skill 不扩写词表、不复现判定，也不做二次分流。
2. **疼痛与伤病**：未命中红旗的疼痛、不适与伤病描述只给一般执行要点与教育信息，并明确建议咨询
   专业医疗人员；诊断、个体化医疗结论、康复方案与进阶时程都停止在这里。用户转述「医生已允许恢复
   训练」时按用户提供的事实复述，医疗判断留在专业人员处。
3. **禁用动作**：禁用动作只存在于画像中已保存的稳定 `exercise_id` 事实里；画像类只读 Tool 在本
   Intent 白名单之外，本 Skill 掌握不到禁用清单：按伤病名称推导禁用动作一律停止。
4. **不出处方与统计**：不生成或修改计划、不决定训练日与组次、不给个体重量（属于 `generate_plan`
   与 `adjust_plan`）；不计算 PB、趋势、月历状态与任何统计数值，只解释 Tool 已经给出的结果。
5. **零写入与不扩边界**：不写训练数据、不承诺保存任何内容（打卡由既有表单与自然语言打卡确认流程
   写入）；不引入 RIR／RPE、训练容量、估算 1RM、完成率、长周期与百分比减量、营养与体脂；不新增
   动作目录数据、不修改 `search_exercises` 的参数与返回结构。

