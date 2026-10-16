# 行为示例（exercise-guidance reference）

示例按「用户请求／Tool Result／期望行为」给出。示例里的 ID 与口径取当前动作目录已存在的记录，数值只作
演示，不代表任何用户的训练事实。

## 示例 1：多个候选的口径差异

**用户请求**：卧推的重量怎么算？杠铃和哑铃不一样吗？

**Tool Result**：`search_exercises` 以 `name="卧推"` 命中多条，其中 `0025`（杠铃卧推，`equipment="barbell"`）、
`0289`（哑铃卧推，`equipment="dumbbell"`）；`get_exercise_details` 以 `ids=["0025","0289"]` 返回
`0025` 的 `load_convention="barbell_total"`、`0289` 的 `load_convention="per_implement"`。

**期望行为**：

- 说明口径差别：`barbell_total` 按含杠铃杆总重记录，`per_implement` 按单只手里的重量记录，同一个数字跨
  口径不可比较。
- 多条命中按器械与体位摆出让用户收敛；用户指明「平板杠铃」后只引用 `0025` 及其口径。
- 可解释返回的 `target`、`muscle_group` 与 `steps`；组次、次数区间与个体重量留给计划流程。

## 示例 2：名称相近的独立记录

**用户请求**：引体要加多少重量？负重引体和引体是同一个动作吧？

**Tool Result**：`search_exercises` 以 `name="引体"` 命中 `0652`（引体向上，`equipment="body weight"`）、
`0841`（负重引体向上，`equipment="weighted"`）、`0017`（辅助引体向上，`equipment="leverage machine"`）；
`get_exercise_details` 返回 `0652` 的 `load_convention=null`、`0841` 的 `load_convention="added_weight"`、
`0017` 的 `load_convention="assistance_weight"`。

**期望行为**：

- 纠正「同一个动作」的前提：三条记录各自独立、成绩互不顶替，`0652` 与 `0841` 不合并。
- `0652` 没有负重字段，只有次数可记；`0841` 按外加重量记录；`0017` 按辅助重量记录。
- 加多少属于个体处方，由计划流程按真实训练记录决定；用别名解释检索为什么命中，不提组数与次数区间。

## 示例 3：目录无结果，叠加伤病描述

**用户请求**：我膝盖不太舒服，想练颈后推举，怎么做比较好？

**Tool Result**：`search_exercises` 以 `name="颈后推举"` 返回 `items=[]`；换 `name="推举"` 命中多条，其中
`0603`（器械肩推，`equipment="leverage machine"`）与 `0405`（哑铃坐姿肩推，`equipment="dumbbell"`）。
「膝盖不太舒服」本次未出现明确医疗风险信号。

**期望行为**：

- 说明目录里没有「颈后推举」这个候选，建议改用标准名或器械词再检索一次，并说明那是换词后的新结果。
- 介绍上述两条时标注为名称与器械形式相近的另一个动作，各自口径独立引用；不得当作「颈后推举」陈述。
- 膝盖不适只给一般执行信息并建议咨询专业医疗人员；不据伤病名称推导禁用动作，组数、次数与个体重量交给
  计划流程。
