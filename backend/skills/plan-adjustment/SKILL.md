---
name: plan-adjustment
description: 基于当前 active 计划与最新训练事实调整计划：用户请求调整训练计划时加载，读取当前 active 计划、其涉及动作的最新 PB、最近四次训练与确定性 trend_summary，在旧计划上做局部修改并产出待用户确认的新版本。不用于首次生成计划（改用 workout-planning）、打卡记录、统计解释、复盘或伤病判断。
---

# plan-adjustment：计划调整 Skill

本 Skill 是**模型指令**，不是业务规则的实现者。调整结果仍必须通过统一计划 Pydantic Schema 与确定性
校验器；两者由 Stage 4 定义，本 Skill 不另造一套输出 Schema、也不替确定性规则下结论（讨论总结
§3.4／§6.2、REFACTOR_PLAN §8.3／§9.2）。

## 职责

- 在当前 active 计划上做**局部修改**：保留未被调整证据推翻的安排，只改有依据的部分。
- 调整依据只用客观事实：目标组次完成情况、PB 是否变化、体重与体脂趋势、距上次训练天数、用户本次
  明确说明的偏好变化。
- 产出待用户确认的新版本（`draft`），并写明每处改动的触发事实。

## 何时加载

Router 判定 `intent = adjust_plan` 后由 `load_skill` 加载本 Skill。没有 active 计划时不走本 Skill：
那属于生成新计划。Graph 已在此之前执行 `safety_check`，急性关键词命中时走 `safety_stop`。

## 可用工具与数据边界

本 Skill 只声明它需要的数据能力，不假设任何未被本次运行绑定的工具；具体工具名与绑定由 Stage 4 的
计划子图节点给出（REFACTOR_PLAN §9.2）。能力与既有实现入口：

| 需要的能力 | 内容 | 既有入口 |
| --- | --- | --- |
| 读取当前 active 计划 | 计划版本、`source_plan_id`（追溯来源）、结构化内容与计划日程 | `domain.plans.PlanReadService.get_active` / `list_sessions` |
| 读取长期画像 | 目标、每周可训练次数、可用器械、明确偏好、当前水平、已知伤病、禁用动作 ID | `domain.profile.ProfileService.read` |
| 读取近期训练 | 最近 4 次训练及其全部组与组类型 | `domain.records.WorkoutRecordsService.list_recent` |
| 读取相关 PB | 只含当前 active 计划涉及动作的 PB（含来源训练、组序号、`performed_on`） | `domain.stats.StatsService.list_personal_bests` + MemoryAssembler 按动作 ID 过滤 |
| 读取趋势摘要 | 确定性 `trend_summary`（最近两条体重／体脂变化、距上次训练天数） | `domain.stats.StatsService.trend_summary` |
| 读取动作目录 | 记录口径、负重口径、`min_load_increment_kg`、是否可用于计划 | `domain.actions.ActionCatalogService` |
| 交回计划草稿 | 新版本按统一 Schema 校验后由 Stage 4 节点保存为 draft | Stage 4 |

禁止：本 Skill 不直接写库、不激活计划、不自行计算 PB 或趋势，也不扩大读取范围去装配无关动作的成绩。

## 所需记忆

以下内容来自本次运行的 MemoryAssembler 装配结果，只读不改：

- 当前 active 计划（作为被调整的对象；缺失即不执行本 Skill）。
- 长期画像七个字段（`unknown` / `denied` / `known` 三态分开处理，不把 `unknown` 当默认值）。
- 最近 4 次训练及其全部组：作为目标组次完成情况与最新负荷的事实来源。
- **只含当前 active 计划涉及动作**的最新 PB，按稳定 `exercise_id` 过滤（外加重量动作只有重量 PB、
  纯自重动作只有次数 PB、计时动作只有时长 PB；来源与日期原样保留，不重算）。
- 确定性 `trend_summary`（数据不足时为 `no_data` / `insufficient_data`，不补造变化值）。
- 当前请求（用户本次明确提出的调整诉求）。

## 调整规则

- **不脱离旧计划重新生成**：新版本必须建立在当前 active 计划之上，保留 `source_plan_id` 追溯；未被
  证据推翻的动作、训练日与安排继续沿用。
- 负荷只按最近一次有效工作组记录决定：达标晋级按 `min_load_increment_kg`，未达标回退到最近一次
  完整完成的负荷，无可回退负荷才重新标记“待校准”。不从 PB 反推训练重量。
- 一次调整以**一个主要变量**为主（重量或次数或组数），且必须落在 Fit-Agent 已冻结的渐进／回退口径内：
  同一负荷连续两次完成全部目标组且达到次数上限，下一次才按 `min_load_increment_kg` 递增；连续两次
  未达次数下限才回退到最近一次完整完成的负荷；没有客观记录支持时不改次数或组数。
- 需要改变频率、训练日或动作体系时，作为明确的**结构调整**提出并说明触发事实，等用户确认，不静默
  替换。
- 一次训练波动不作为调整依据；客观记录不足时先补记录，不凭单次波动下结论。
- 禁用动作按画像中已明确保存的稳定 `exercise_id` 确定性排除。

## 禁止事项

- 不重新生成一份与旧计划无关的新计划，也不改写未被调整证据覆盖的内容。
- 不诊断伤病、不做安全分流、不评价进步／退步／停滞／疲劳；`safety_check` 与禁用动作过滤由 Graph
  节点确定性执行。
- 不输出 RIR 或主观用力等级，不讲训练容量、估算 1RM、完成率。
- 不使用硬编码训练阈值（固定周数、百分比减量、固定组数表、按天的停训阈值等）。
- 不读取当前 active 计划之外动作的 PB，也不引用本文与 `workout-planning` 知识来源之外的材料。
- 不激活、不归档、不拒绝：新版本只有经用户确认后才由 Stage 5 事务启用，原 active 计划在此之前不变。

## 结构化输出要求

- 输出必须是一个**计划草稿对象**（新版本），交由 Stage 4 统一计划 Pydantic Schema 校验；本 Skill
  不定义字段名、不输出第二套结构。
- 草稿必须保留对当前 active 计划的追溯关系，并逐项说明改动与触发事实；未被改动的部分按原样沿用。
- 确定性校验器（Stage 4）对负荷来源、次数区间、递增条件与禁用动作做硬检查；校验不通过只允许修订
  一次，二次不通过即结束流程且不产生可激活计划，原 active 计划保持有效（讨论总结 §3.4）。
- 涉及频率、训练日或动作体系的结构调整必须作为待确认项列出，不能混在“已确定”的改动里。

## 训练知识来源

本 Skill 只使用下列本地来源中与 Fit-Agent 已冻结口径兼容的训练知识归纳。它们都是**本地二次归纳**
（作者对公开材料的整理），不是 Fit-Agent 独立核验的原始文献，也不代表原作者最新观点；冲突时以
Fit-Agent 权威文档为准。路径以本地克隆 `Lzheng-fitness` 根为起点。

| 本地来源文件 | 采用的内容 | 性质 |
| --- | --- | --- |
| `skills/lzheng-strength-training-review/references/rolling-review-rules.md` | 可比记录条件（同动作、同动作标准、同器械与重量口径、正式组而非热身或辅助组）；一次只改变重量、次数或组数中的一个主要变量；频率、训练日、动作体系等改动属于需要用户确认的结构调整 | 作者二次归纳 |
| `knowledge/05-lzheng-training-review.md` | 外部训练记录默认只读，不得篡改或补全原始事实；改变频率、动作、组数、次数、重量等之前说明触发证据、当前路径与建议路径并等待用户确认；一次状态差不等于平台期 | 作者二次归纳 |
| `knowledge/00-lzheng-knowledge-map.md` | 事实优先级（用户本次明确确认 > 可核验当前记录 > 快照 > 历史）；缺失数据必须标为未知或假设 | 作者二次归纳 |
| `knowledge/evidence-base.md` | 从用户当前能力与可执行条件开始；Skill 不作医疗诊断，也不替代合格专业人员的个体评估 | 作者二次归纳 |

不采用的内容与 `workout-planning` 一致：RIR／RPE 主观用力口径、P0–L3 分层与专家路由、固定周数周期
与百分比减量、短时降级版本与按天阈值、训练量数值表、估算 1RM、完成率，以及医疗或康复类内容。
