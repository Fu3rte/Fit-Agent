# 行为示例（exercise-guidance reference）

三个示例按「用户请求／Tool Result／期望行为」给出：只列必要事实，每例 3 项期望行为。示例里的
`exercise_id`、口径与数值取自当前动作目录，不代表任何用户的训练事实。

## 示例 1：多个候选的口径差异

**用户请求**：卧推的重量怎么算？杠铃和哑铃不一样吗？

**Tool Result**：`search_exercises` 以 `{"query": "卧推"}` 命中 6 条 `reps_weight` 记录：
`barbell-bench-press`（`barbell`，`barbell_includes_bar_total`）、
`dumbbell-bench-press`（`dumbbell`，`dumbbell_per_hand`）、`barbell-incline-bench-press`、
`dumbbell-incline-bench-press`、`barbell-close-grip-bench-press`、`barbell-decline-bench-press`。

**期望行为**：

- 说明口径差别：杠铃类按含杠铃杆总重记录，哑铃类按单只手里的重量记录，同一个数字跨口径不可比较。
- 6 条命中按器械与体位摆出让用户收敛；用户指明「平板杠铃」后只引用 `barbell-bench-press` 及其口径。
- 可解释返回的肌群、动作模式与指导语；组次、次数区间与个体重量留给计划流程。

## 示例 2：名称相近的独立记录

**用户请求**：引体要加多少重量？我记得负重引体和引体是同一个动作。

**Tool Result**：`search_exercises` 以 `{"query": "引体"}` 命中 3 条：`pull-up`（`reps_bodyweight`，
负重字段为 `null`）、`chin-up`（`reps_bodyweight`，负重字段为 `null`）、`weighted-pull-up`
（`reps_weight`，`external_added_weight`）。

**期望行为**：

- 纠正「同一个动作」的前提：三条记录各自独立、成绩互不顶替，`pull-up` 与 `weighted-pull-up` 不合并。
- 自重两条只有次数可记，没有「加多少公斤」的目录依据；负重引体按外加重量记录。
- 加多少属于个体处方，由计划流程按有效工作组与确定性渐进规则决定；用别名解释检索为什么命中，
  不提组数与次数区间。

## 示例 3：目录无结果，叠加未命中红旗的伤病描述

**用户请求**：我膝盖不太舒服，想练颈后推举，怎么做比较好？

**Tool Result**：`search_exercises` 以 `{"query": "颈后推举"}` 返回 `exercises=[]`；换 `{"query": "推举"}` 命中
5 条，名称与器械形式最接近的是 `barbell-seated-overhead-press`（`barbell_includes_bar_total`）与
`seated-dumbbell-shoulder-press`（`dumbbell_per_hand`），两条都是 `reps_weight`。「膝盖不太舒服」未
命中急性红旗词表，本次请求正常到达 `general`。

**期望行为**：

- 说明目录里没有「颈后推举」这个候选，建议改用标准名或器械词再检索，并说明那是换词后的新结果。
- 介绍上述两条时标注为名称与器械形式相近的另一个动作，各自口径独立引用；不得当作「颈后推举」陈述。
- 膝盖不适只给一般执行信息并建议咨询专业医疗人员；不据伤病名称推导禁用动作，组数、次数与个体
  重量交给计划流程。
