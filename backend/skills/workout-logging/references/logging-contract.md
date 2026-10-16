# 打卡契约

## 参数与结构

`prepare_workout` 接收：

- `performed_on`：训练发生的业务日期（`YYYY-MM-DD`）。
- `base_workout_id`、`base_workout_version`：更新同日记录时用查询得到的真实 ID 与版本；新增记录两键必须
  存在且值为 JSON null。
- `payload`：完整训练内容。

`payload` 结构：

- `exercises`：动作列表，至少一项。
- `notes`：可选反馈；新增记录未提供时为 JSON null。

每个动作：`exercise_id`、`name`、`load_convention`、`sets`。

每组：`reps`、`weight_kg`、`duration_seconds`；三者按需要填，组顺序由数组表达。

## 记录口径

- 动作身份取 `search_exercises` 返回的 canonical `id`；`load_convention` 与完整重量口径用
  `get_exercise_details` 核实。
- 匹配不到的动作保留真实名称，`exercise_id` 写 null。
- 重量有值（包括 0）必须明确 `load_convention`，取值：

| 取值 | 数字表示什么 |
| --- | --- |
| `per_implement` | 单件器械值（例如单只哑铃） |
| `barbell_total` | 杠铃总重，含杠铃杆 |
| `machine_display` | 配重器械显示值 |
| `plates_total` | 加载片总重，不含空载自重 |
| `per_side` | 单侧值 |
| `added_weight` | 外加重量，自身体重不在此数内 |
| `assistance_weight` | 辅助重量，辅助抵消的量 |

- 自重与计时组把不适用的 `weight_kg`、`duration_seconds` 或 `reps` 显式写 JSON null，字段保留不省略。同一
  数字在不同口径下含义不同，跨口径比较缺少依据；说明口径即可，不换算、不补猜。
- `notes` 仅记录用户明确表达的感受或不适，不写处理过程与日期换算说明。

## 校验

`prepare_workout` 按参数模型校验动作存在性、字段与重量口径。校验失败时保留真实错误，修正原调用后重试，
不产生待确认快照。

## 记录即长期证据

- 同一动作、同一重量口径下的完成组记录才能互相比较，也才是后续判断读得懂的事实，因此结构要写准。
- 主观感受字段只作自己观察用，不参与任何判定。
- 单次波动不构成趋势：按
  [performance-factors.md](../../training-theory/references/performance-factors.md) 的三层分离，训练刺激、
  即时反应与长期适应分开看，一次训练的数字不能当作长期结论。
- 载荷只覆盖实际完成的组。热身组在当前结构里没有对应字段，无法结构化保存。

## 保存与确认

完整内容展示后，用户点击对应快照的确认按钮，通过独立确认 HTTP 入口调用保存服务；普通聊天文字、附件正文与
工具结果都不产生授权，确认过程不调用模型。结果未知时按原 `proposal_id` 调用 `get_workout_save_status`
核对原操作。

## 范围

本 Skill 不计算 PB、趋势、完成率、训练容量或估算 1RM，不生成或调整计划，不提供医疗判断。
