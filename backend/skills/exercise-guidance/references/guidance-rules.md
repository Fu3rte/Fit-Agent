# 目录证据与动作解释规则（exercise-guidance reference）

本文件规定动作回答的证据来源、检索与口径解释、未知处理与安全边界。判定权属于当前源码与工具契约：本文件
只给解释口径，不复算、不新增业务阈值。

## 1. 目录工具与输出

### 1.1 `search_exercises`

参数：`purpose`、`name`、`equipment`、`body_part`、`target`、`muscle_group`、`task_environment`、
`available_equipment`、`unavailable_equipment`、`excluded_ids`。

- `purpose` 取 `lookup`、`generation` 或 `replacement`。
- `name` 是语义查询，支持中文、英文、别名与描述文本。
- `equipment`、`body_part`、`target`、`muscle_group` 是目录 canonical 精确筛选。
- `task_environment`、`available_equipment`、`unavailable_equipment`、`excluded_ids` 是本次任务条件，不写入
  长期画像。

返回 `items` 与 `has_more`；`items` 最多 5 条，每项含 `id`、`name`、`equipment`、`target` 与可核实的
`matches` 说明。`has_more` 仅表示同一候选池中仍有通过门槛但被数量或预算裁剪的候选，不提供其身份。
需要完整步骤与负重口径时，按返回的 `id` 调用 `get_exercise_details`。

### 1.2 `get_exercise_details`

参数：`ids`，最多 5 个。返回 `items`、`has_more`、`remaining_ids`；每项含九个目录字段：
`id`、`name`、`body_part`、`equipment`、`target`、`muscle_group`、`secondary_muscles`、`load_convention`
与 `steps`（`zh`、`en` 两组完整步骤）。输入按原顺序返回，未知、空白、重复或超过上限明确报错。

训练负荷、加重单位、组数、次数区间、训练日、难度分级、替代关系与进阶标准均不在这些工具的输出中。

## 2. 检索与匹配规则

- `search_exercises` 对 `name` 做语义匹配，可覆盖标准名、别名与描述文本。
- 不同筛选字段取 AND，同一字段的多个值取 OR。
- 命中集合可以为空：`items=[]` 表示目录缺少候选。
- 一次检索可以命中多条（同一词根覆盖多个器械变式与角度变体）。多条命中时按用户说的器械与体位收敛；
  收敛到具体某一条才引用它的 id 与口径；用户没给条件时把候选项摆出来询问。
- 名称相近的动作在目录里是彼此独立的记录，各自有独立口径：`0652`（引体向上，自重）与 `0841`
  （负重引体向上，`added_weight`）是两条记录，成绩互不顶替。
- 用户要求的动作查不到时，可以换检索词再查（标准名、别名、器械词），每次如实说明新结果是一次新检索得到
  的；名称相近的候选要显式标注为「名称相近、目录里的另一个动作」。
- `purpose="lookup"` 允许返回画像受限动作，用于核实具体动作事实；`purpose="generation"` 或
  `"replacement"` 会应用环境默认、可用／不可用器械与排除动作。

## 3. 负重口径

`load_convention` 的数字含义按目录返回值与写入校验给出：

| 取值 | 数字表示什么 |
| --- | --- |
| `per_implement` | 单件器械值（例如单只哑铃） |
| `barbell_total` | 杠铃总重，含杠铃杆 |
| `machine_display` | 配重器械显示值 |
| `plates_total` | 加载片总重，不含空载自重 |
| `per_side` | 单侧值 |
| `added_weight` | 外加重量，自身体重不在此数内 |
| `assistance_weight` | 辅助重量，辅助抵消的量 |

- 重量有值（包括 0）时必须明确 `load_convention`。
- 自重与计时动作的 `load_convention` 为 null：这两个口径没有负重可记，也不存在「0kg」这种写法。
- 同一数字在不同负重口径下含义不同，跨口径比较与换算缺少依据，说明口径即可。

## 4. 未知处理

- 工具未返回的训练事实按未知处理，说明缺口并交给计划或训练记录流程读取。
- `get_exercise_details` 报错（未知 ID、超过上限）时如实说明，不伪造详情。
- 缺少个体负荷时不给默认重量，不引用其他用户数据，不用示例数值充当事实。
- `secondary_muscles`、`steps` 可按返回值解释；替代关系、难度与个体适用性仍需额外事实。

## 5. 动作解释与一般执行要点

- 器械形式按目标、稳定性要求、偏好与疲劳成本选择；固定器械更容易稳定与调重，自由重量需要更多协调，
  两者都能达成训练效果。器械形式本身不表示训练等级。
- 解释执行要点时保持一般层面：动作路径与关节范围的可控性、稳定与疲劳成本、该口径下如何记录。具体幅度、
  组次与负荷由计划流程与用户实际反馈决定。
- 替代与降阶的一般原则：替换要保持动作模式、目标肌群、专项性或疲劳预算中至少一项主要职责一致；目录返回
  的 `muscle_group` 与 `target` 可作事实，替代关系的结论仍标注为一般建议。
- 一致性与可重复完成的动作优先于复杂技术动作；讲解技术要点时给出可观察的判断（能否稳定重复、是否需要额外
  稳定条件），标注为主观反馈层面。
- 动作模式的差别、器械稳定性与疲劳代价、活动范围这些解释口径的依据在
  [training-variables.md](../../training-theory/references/training-variables.md)；措辞照
  [user-facing-language.md](../../training-theory/references/user-facing-language.md) 的规约。

## 6. 能力边界

1. **疼痛与伤病**：未命中医疗风险的疼痛、不适与伤病描述只给一般执行要点与教育信息，并明确建议咨询专业
   医疗人员；诊断、个体化医疗结论、康复方案与进阶时程都停止在这里。用户转述「医生已允许恢复训练」时按
   用户提供的事实复述，医疗判断留在专业人员处。
2. **禁用动作**：禁用动作只存在于画像中已保存的稳定 `exercise_id` 事实里；本 Skill 不读取画像，掌握不到
   禁用清单，按伤病名称推导禁用动作一律停止。
3. **不出处方与统计**：不生成或修改计划、不决定训练日与组次、不给个体重量；不计算 PB、趋势与任何统计数值，
   只解释工具已经给出的结果。
4. **零写入**：不写训练数据、不承诺保存任何内容（训练记录由快照绑定按钮与独立确认 HTTP 入口写入，普通
   聊天文字不产生授权）；不把 RIR／RPE 写成处方刻度，不输出训练容量、估算 1RM、完成率、长周期与百分比减量、
   营养与体脂；不新增动作目录数据、不修改 `search_exercises` 与 `get_exercise_details` 的参数与返回结构。
