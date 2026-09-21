# Lzheng-fitness：10 个 Skill 与专家库

仓库根：`Lzheng-fitness/`  
索引：`README.md`（§包含内容）· 用户可见流程：`SYSTEM-FLOW.md` · 安装器：`tools/install.py`

角色分层（`README.md`）：**计划 / 周期 / 复盘 / 接回 / 营养** 为专业处方能力；**专家库** 为它们共用的内部知识层；**系统总控 + 工作台构建器** 把结果收束为单一主源与可离线打开的页面。

---

## 10 个 Skill

| # | Skill | 作用 | 典型工作场景 | 源码索引 |
|---|--------|------|--------------|----------|
| 1 | `lzheng-fitness-plan` | 训练建档、安全筛查与分流、训练者分层、动作适配、完整七天计划、短版降级、状态快照、计划 HTML 校验与渲染 | 「制定/重做计划」「一周练几次、怎么分化」「动作怎么选」 | `skills/lzheng-fitness-plan/SKILL.md`<br>refs：`intake-and-state-snapshot.md`、`trainee-classification.md`、`exercise-selection.md`、`program-design.md`、`plan-contract.md`、`html-output-spec.md`、`fitness-ui-contract.md`、`knowledge-routing.md`、`evidence-base.md`<br>scripts：`validate_plan.py`、`render_fitness_plan.py`、`audit_html_plan.py` |
| 2 | `lzheng-strength-cycle-planner` | 单个力量主项 8—12 周周期：组次、重量、RPE/RIR、顶组/回退、减量、测试、渐进规则与未完成分支；含主项渐进曲线 HTML | 「提高卧推/深蹲」「规划 RPE、减量、测试日」「把周期做成网页」 | `skills/lzheng-strength-cycle-planner/SKILL.md`<br>refs：`cycle-design-rules.md`、`output-spec.md`、`cycle-html-contract.md`、`training-plan-website-spec.md`、`evidence-base.md`<br>scripts：`render_strength_cycle_html.py`；`assets/strength-cycle-template.html` |
| 3 | `lzheng-strength-training-review` | 单次 / 滚动 / 基准 / 周训练复盘；体感硬门槛、含糊记录追问、重复问题追因；产出下次处方或周度决策并更新复盘索引 | 「今天练了」「这次练得怎么样」「下次练什么」「这周复盘/要不要减载」 | `skills/lzheng-strength-training-review/SKILL.md`<br>refs：`weekly-review-rules.md`、`weekly-review-template.md`、`rolling-review-rules.md`、`lzheng-cycle-adjustment-rules.md`、`review-output-spec.md`、`local-review-record-spec.md`、`evidence-base.md` |
| 4 | `lzheng-training-return` | 停训 ≥7 天、连续漏练 3 次、病后获准恢复、条件明显变化、4 周内反复中断后的接回：分档任务、48 小时启动、接回卡 | 「我一周没练了」「如何重新开始训练」；漏练 1—2 次或单日状态差仍由 plan 处理 | `skills/lzheng-training-return/SKILL.md`<br>refs：`return-workflow.md`、`return-card-spec.md`、`knowledge-routing.md`、`evidence-base.md` |
| 5 | `lzheng-training-expert-library` | 六个来源限定专家模块 + 选择协议 + 输出协议；内部知识层，**不单独开方、无写入权** | 计划/周期/复盘/接回中，营养、肌肥大、计划结构、专项力量、获准返场等变量会改变结论时按需读入 | `skills/lzheng-training-expert-library/SKILL.md`<br>refs：`expert-selection-contract.md`、`expert-registry.json`、`expert-output-protocol.md`、`experts/<id>/` |
| 6 | `lzheng-nutrition-system` | 营养建档、`nutrition_contract`、训练日型宏量目标、餐食估算候选→用户确认入账、两周趋势复盘；与当前训练计划联动 | 「建立营养起点」「今天吃什么」「报餐入账」「饮食两周复盘」 | `skills/lzheng-nutrition-system/SKILL.md`<br>refs：`nutrition-contract.md`、`learning-and-meals.md`<br>scripts：`validate_nutrition_contract.py`、`update_planner.py`、`test_nutrition_system.py` |
| 7 | `lzheng-video-learning` | 收藏/指定链接来源核对 → 抓取与转写 → 分批进度与失败重试 → 按「问题→观点→边界」组织交互学习；不自动改训练饮食 | 「学习我收藏里关于 X 的视频」「给这个链接做学习材料」 | `skills/lzheng-video-learning/SKILL.md`<br>scripts：`video_learning.py`、`pipeline.py`、`question_tools.py`<br>可选衔接：第三方 `dbs-learning`（见 `THIRD-PARTY-NOTICES.md`） |
| 8 | `lzheng-knowledge-library` | 用户已确认的学习条目（作者、专题、来源视频、正文）入库、多专题展示、来源回看与独立更新；默认空白 | 「把已确认的笔记/视频摘要放进工作台知识区」「更新某个专题」 | `skills/lzheng-knowledge-library/SKILL.md`<br>scripts：`publish_knowledge.py`；`assets/example-knowledge.json` |
| 9 | `lzheng-training-system` | 套件总控：bootstrap / doctor / inspect / upgrade / validate、迁移与升级保护、交接消费、日常路由到专业 Skill；不产出处方 | 「开始建立我的健身系统」「新电脑搭建」「升级并保留记录」「工作台异常排查」 | `skills/lzheng-training-system/SKILL.md`<br>refs：`system-contract.md`、`handoff-schema.md`、`html-template-contract.md`<br>scripts：`lzheng_training_system.py` |
| 10 | `lzheng-fitness-workbench-builder` | 唯一 HTML 模板 + 事实文件 + 构建脚本：初始化工作台、数据刷新、壁纸替换、UI 升级、路径可迁移修复、发布副本与检查 | 「从零建工作台」「刷新计划/复盘数据」「换壁纸」「修侧栏」 | `skills/lzheng-fitness-workbench-builder/SKILL.md`<br>refs：`input-contract.md`、`ui-upgrade.md`、`background-replacement.md`、`visual-contract.md`、`migration-and-release.md`、`path-portability-repair.md`<br>`assets/workbench-template.html`；scripts：`Initialize-` / `Refresh-` / `Inspect-FitnessWorkbench.py` 等 |

**边界摘要**

- 处方权：plan / cycle / review / return / nutrition；cycle 结果须交回 plan 合并才成为当前计划（`lzheng-training-system` 日常路由 §2）。
- 专家库：只供来源限定判断；当前事实、计划版本、营养协议、工作台写入权归主 Skill（`lzheng-training-system` 日常路由 §专家段）。
- video / knowledge：材料与展示；确认后可接入 knowledge-library，非处方。
- training-system + workbench-builder：安装、路由、主源与页面；workbench 只读聚合（日常路由 §6）。

---

## 专家库（`lzheng-training-expert-library`）

### 运转方式（文档协议，非图节点）

1. 主 Skill 判断问题变量会改变结论 → 读 `references/expert-selection-contract.md`。
2. 按变量路由选模块；**默认 1 位**，仅独立变量或真实冲突才追加；禁止投票、禁止为展示加人。
3. 查 `references/expert-registry.json` → 进 `references/experts/<id>/`，读入口 / `module.json` 与模块内骨架文件。
4. 按 `references/expert-output-protocol.md` 输出：来源限定判断、适用前提、保留、仍缺证据。
5. 主 Skill 整合当前事实 → 最终建议 / 处方 / 写入；未采用则不出现专家区块。

红旗（疼痛未评估、急性创伤、术后未获许可、胸痛晕厥等）不进专家讨论，先安全分流（选择协议 §变量路由）。

### 六个模块

| id | 领域（registry `variables` 摘要） | 主来源（`source`） | `status` |
|----|-----------------------------------|--------------------|----------|
| `alan-aragon-flexible-dieting` | 营养、能量平衡、宏量、补剂、依从、维持 | Flexible Dieting (2022) | `book_distilled_plan_review_accepted` |
| `brad-schoenfeld-hypertrophy` | 肌肥大、训练量、频率、力竭、ROM、并行训练 | Science and Development of Muscle Hypertrophy, 2e (2021) | `book_distilled_two_entries_accepted` |
| `brukner-khan-return-to-sport` | 获准返场、功能进阶、二级预防（排除诊断/影像/用药/手术） | Brukner & Khan's Clinical Sports Medicine, 4e (2012) | `book_distilled_plan_return_accepted` |
| `dan-john-intervention-easy-strength` | Point A、目标清晰、动作缺口、回基础、低疲劳练习 | Intervention (2013) and Easy Strength Omnibook (2022) | `content_ready_entry_acceptance_pending` |
| `eric-helms-training-pyramid` | 依从、训练结构、量、强度、频率、渐进、减载、峰值 | The Muscle & Strength Training Pyramid: Training, 2e (2018) | `content_ready_entry_acceptance_pending` |
| `greg-nuckols-strength-periodization` | 力量停滞、专项性、量/频率、周期化、自动调节、峰值、技术假设 | Stronger by Science public articles, cutoff 2026-08-10 | `content_ready_entry_acceptance_pending` |

`status` 含 `…_pending`：内容与结构可审计，**主入口验收未完成**，不得写成「已全面验证」（专家库 `SKILL.md` §验证）。

### 模块目录骨架

每模块 `references/experts/<id>/` 大致同构：

- `module.json` · `PUBLIC-SOURCE-BOUNDARY.md`
- 入口：多数为 `00-专家总入口.md`；**Greg 为 `README.md`**（registry `entry`）
- 覆盖矩阵 / 框架与判断 / 重点问题与门槛 / 问题路由 / 与他专家职责边界 / 测试与遗漏审计
- `knowledge/` 或 `知识卡/`：跨章决策与知识卡

安装：装 plan / cycle / review / return 任一专业 Skill 时，安装器自动带专家库（`README.md` §包含内容）。

### 选择协议速查（`expert-selection-contract.md`）

| 变量 | 主模块 | 需要时追加 |
|------|--------|------------|
| 营养/宏量/补剂/依从/维持 | Alan Aragon | 增肌刺激受限 → Brad；训练结构受限 → Eric |
| 肌肥大机制/测量/有氧并行 | Brad Schoenfeld | 一般不追加 |
| 增肌训练量/频率/力竭/活动范围 | Brad Schoenfeld | Eric 查结构与恢复 |
| 一般计划结构/依从/渐进/减载 | Eric Helms | 不因力量目标自动加 Greg |
| 力量停滞/专项/变量实验/峰值 | Greg Nuckols | Eric 查上游计划与恢复 |
| 目标含糊/反复中断/动作缺口/回基础 | Dan John | 再按主变量选 Eric/Greg/Brad/Alan |
| 获准后功能进阶/返场/复发预防 | Brukner & Khan | 仅「最低可执行路径」独立时追加 Dan John |

整合顺序：主 Skill 读当前事实 → 各专家只出来源限定判断 → 说明共同点与分歧来源 → 主 Skill 生成最终建议（选择协议 §整合顺序）。

---

## 总控日常路由（`lzheng-training-system` §日常路由）

```text
完整建档/长期计划/短版     → lzheng-fitness-plan
单动作 8—12 周周期         → lzheng-strength-cycle-planner（交回 plan 合并）
单练/周复盘/下次处方       → lzheng-strength-training-review
停训≥7天/连漏3次/条件变化  → lzheng-training-return
饮食建档/日型/餐食/两周复盘 → lzheng-nutrition-system
工作台构建/刷新/迁移/发布   → lzheng-fitness-workbench-builder
（按需）来源限定判断        → lzheng-training-expert-library
```

`lzheng-video-learning` / `lzheng-knowledge-library` 走学习与知识条目链路，经确认后可写入 knowledge-library，不进入上表处方路由。
