---
name: workout-planning
description: 生成新的个性化训练计划草稿：用户请求生成训练计划时加载，按长期画像、当前 active 计划、最近四次训练、全部已有动作 PB 与确定性 trend_summary 产出待用户确认的计划草稿，并声明负荷来源、渐进与禁用动作边界。不用于调整已有 active 计划（改用 plan-adjustment）、打卡记录、统计解释、复盘或伤病判断。
---

# workout-planning：训练计划生成 Skill

本 Skill 是**模型指令**，不是业务规则的实现者。计划内容最终必须通过统一计划 Pydantic Schema 与
确定性校验器才算成立；两者由 Stage 4 定义，本 Skill 不另造一套输出 Schema、也不替确定性规则下结论
（讨论总结 §6.2／§6.3、REFACTOR_PLAN §8.3／§9.2）。

## 职责

- 产出一份**待用户确认的**计划草稿（`draft`），内容覆盖：训练目标对应的动作安排、计划训练日、每个
  动作的组次安排与负荷来源。
- 计划里的每个动作使用动作目录的稳定 `exercise_id`，并写明负荷来源（最近一次有效工作组记录，或
  “待校准”）。
- 只生成被请求的那一个目标版本；生成新计划时不依赖任何旧计划去“猜”未确认的事实。

## 何时加载

Router 判定 `intent = generate_plan` 后由 `load_skill` 加载本 Skill 正文与
[planning-rules.md](references/planning-rules.md)。生成计划前 Graph 已经完成 `safety_check`：急性
关键词命中时走 `safety_stop`，本 Skill 不会被执行，也不需要自行判定安全分流。

## 可用工具与数据边界

本 Skill 只声明它需要的数据能力，不假设任何未被本次运行绑定的工具；具体工具名与绑定由 Stage 4 的
计划子图节点给出（REFACTOR_PLAN §9.2）。能力与既有实现入口：

| 需要的能力 | 内容 | 既有入口 |
| --- | --- | --- |
| 读取长期画像 | 目标、每周可训练次数、可用器械、明确偏好、当前水平、已知伤病、禁用动作 ID | `domain.profile.ProfileService.read` |
| 读取当前计划 | 当前 active 计划与版本身份（用于判断是否已存在计划） | `domain.plans.PlanReadService.get_active` |
| 读取近期训练 | 最近 4 次训练及其全部组与组类型 | `domain.records.WorkoutRecordsService.list_recent` |
| 读取 PB | 全部已有动作的最新 PB（含来源训练、组序号、`performed_on`） | `domain.stats.StatsService.list_personal_bests` |
| 读取趋势摘要 | 确定性 `trend_summary`（最近两条体重／体脂变化、距上次训练天数） | `domain.stats.StatsService.trend_summary` |
| 读取动作目录 | 记录口径、负重口径、`min_load_increment_kg`、是否可用于计划 | `domain.actions.ActionCatalogService` |
| 交回计划草稿 | 计划内容按统一 Schema 校验后由 Stage 4 节点保存为 draft | Stage 4 |

禁止：本 Skill 不直接写库、不调用统计计算、不自行计算 PB 或趋势、不激活计划。

## 所需记忆

以下内容来自本次运行的 MemoryAssembler 装配结果，只读不改：

- 长期画像七个字段。每个字段有 `unknown` / `denied` / `known` 三态：`unknown` 是“用户没填”，
  `denied` 是“用户明确为空”，两者不得混为默认值；画像里已知伤病与禁用动作是**用户已明确记录**的
  事实，不是可推导的结论。
- 当前 active 计划（可能不存在；不存在就用空基线生成新计划，不用 draft／archived 顶替）。
- 最近 4 次训练及其全部组（不裁成 PB 有效工作组）。
- 全部已有动作 PB（生成新计划时读取全部；数值与来源原样使用，不重算、不从 PB 反推日常训练重量）。
- 确定性 `trend_summary`（数据不足时为 `no_data` / `insufficient_data`，不得补造变化值）。
- 当前请求。

## 禁止事项

- 不诊断、不评价伤病，不根据伤病名称推导禁用动作；禁用动作只取画像中已明确保存的稳定
  `exercise_id`，并在计划里确定性排除。
- 不扩写患者安全词表、不做安全判定：急性关键词与 `safety_stop` 由 Graph 的 `safety_check` 节点决定。
- 不输出 RIR 或主观用力等级，不讲训练容量、估算 1RM、完成率，也不给进步／退步／停滞／疲劳判断。
- 无有效训练历史或无可回退负荷时，不得给出任何具体重量，只标记“待校准”。
- 不使用硬编码训练阈值（固定周数周期、百分比减量、短时降级版本、固定组数表等）。
- 不引用本 Skill 与 [planning-rules.md](references/planning-rules.md) 之外的知识来源。
- 不把未确认的计划当成已启用计划：激活、归档与拒绝都由用户确认后的 Stage 5 事务处理。

## 结构化输出要求

- 输出必须是一个**计划草稿对象**，交由 Stage 4 统一计划 Pydantic Schema 校验；本 Skill 不定义字段名、
  不输出第二套结构，也不以 Markdown 计划表代替结构化结果。
- 确定性校验器（Stage 4）对负荷来源、次数区间、递增条件和禁用动作做硬检查；模型只负责目标匹配、
  安排合理性与解释质量（REFACTOR_PLAN §9.2）。校验不通过只允许修订一次；二次不通过则由流程终止，
  不产生可激活计划（讨论总结 §3.4）。
- 每个动作必须写稳定 `exercise_id` 与负荷来源：`known` 负荷来自最近一次有效工作组记录；无记录时
  写“待校准”。动作目录的 `min_load_increment_kg` 是唯一加重单位。
- 次数区间与组次由本计划给出，但它们是可被后续客观记录推翻的起点假设，不是硬编码阈值；具体区间
  取值以用户画像、动作目录与 Stage 4 Schema 为准，本 Skill 不引用固定次数表。

## 训练知识来源

本 Skill 的训练知识只取 [planning-rules.md](references/planning-rules.md)：Fit-Agent 已冻结口径 +
`Lzheng-fitness` 本地知识库中与这些口径兼容的归纳。该文件逐条登记了实际采用的本地来源文件，并明确
标明是**本地二次归纳**（不是 Fit-Agent 独立核验的原始文献，也不代表原作者最新观点）。
