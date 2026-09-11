# 方案：身体情况合并的重新实施（存档）

> 性质：历史实施计划存档，**已冻结为只读，今后一律不再更新**。2026-09-11 由 `pre-prj/stage/` 移入本目录。该次合并已于同日实现并提交（`16b9e52`）。
> 实施口径（1A 读旧写新、2A 读取时派生展示）与全部结论已落于现行正本：`pre-prj/architecture/02-profile-security.md` §2.1／2.3、`pre-prj/stage/stage2.md` §4.3／§8、`frontend/plans/stage1.md` §1／§3／§5／§7／§8、`pre-prj/PRD.md` §5.2；与其他文档冲突时以现行正本为准。
> 本文仅供回查拍板溯源（§0.1 用户原话与外部依据、撤回经过与当时的分层实施要求）；不构成实现授权，也不被其他文档引用。

## 0. 背景与本次要解决的失败模式

2026-09-10 的实施已全部撤回（22 个文件回退，后端回到 `396 passed` 基线，撤回当时正本零改动）。失败原因不是方向错误，而是：

1. 编排事故：两个 workflow 并发、4 个 writer 同时写同一批文件。
2. 语义不自洽：最典型的是「清单外症状的读取语义」与 `stage1.md` §7 已拍口径冲突。
3. 未先定稿规格就动手，分多轮试探。

本方案的对策：**先定稿规格 → 单写者串行 → 一次做完 → 独立 reviewer 只读复审**。

### 0.1 拍板结论与溯源（2026-09-10）

方向已于 2026-09-10 拍定，重新实施时不必重开讨论。用户原话逐条附下。

**问题 1｜档案身体情况的结构**：`body_state`（其他身体状态）与 `red_flags`（红旗症状）两个平行字段**合并**为单一事实；分界线从未在任何正本定义，且 `body_state` 在生产代码中**无任何消费方**（死字段）；写入方被迫判断「这条算不算症状」（医学判断），猜错篮子则事实静默逃逸。分类（与 6 类清单匹配）**移到读取时**判定；**保留 `Fact` 三态**（unknown／denied／known），不得退化；`SessionConditions` 一并合并；首次建档清单 9 项 → **8 项**。
原话：「1. A这里如果合并成身体情况，那后端的字段是合并还是按现在这样分开？」（对「后端字段是否也合并」的追问，答复「合并」后继续推进且未反对）；「2. 可以。」（指 `SessionConditions` 一并合并）

**问题 2｜建档是否单独追问「动作限制」**：**不问**。`action_restrictions` 字段与结构**不变**，仍留在首次建档必填清单（必须有结论，只是不独立提问），结论由身体情况推出。
原话：「2. 不问。」；提出该问题时的原话：「现在档案的其他和红旗有语义冲突，建档时的动作限制和身体状态也很奇怪。为什么要说动作限制？直接问身体状态不好吗？」

**问题 3｜限制提议的产生方式**：由 **LLM 参与提议**（符合 `01-shared-transaction.md` §1.3 已拍端到端链路）。边界不变：**安全判定仍由确定性代码负责，不依赖 Prompt 充当正式写入的安全校验**；Agent 只提议、不决定。医学边界：LLM 可提议「避免深蹲模式」，**不可输出诊断**（PRD §5.2：不诊断具体疾病、不自动新增或解除永久禁忌）。
原话：「那就调用llm参与提议，这样OK吗？」

**问题 4｜草稿卡展示规则 3a／3b**：3a——报告了身体情况**且**推导出限制时，草稿卡显示理由链（「你说 X → 因此排除 Y」）；3b——报告非空但**未**推导出任何限制时，草稿卡必须显式展示，不得静默放过（拒绝「系统悄悄得出什么都没发生」这一结论）。具体**措辞延期**。
原话：「那就3a + 3b，具体的提示文本到时候再改。」；对 3a 场景的原话：「3. 如果场景是ai:'你说你膝盖痛, 所以本次计划不纳入跟腿部训练有关的动作' -> 用户确认，那我觉得没问题，我现在不知道你是什么场景。」

**问题 5｜阻断／无结论提示的模板结构**（方案 B）：采纳 Lzheng 模板结构（当前结果／需要暂停的内容／原因／你下一步做什么／状态），但**领域层只补事实字段，不塞面向用户的模板文案**；文案留到 **Stage 4**；「只说明观察到的风险，不进行医学诊断」须落成 **Stage 4 的输出 schema 约束**，不靠提示词自觉。明确丢弃项：不给 `SafetyCheckResult` 加新字段（四段数据已齐备）；不给 `ActionRestriction` 加 `reason`／`source`（3a 的理由链是 Stage 4 生成期产物，不是存储关系）。
原话：「B。」

**外部依据（Lzheng-fitness 调研，用户指定核验）**

- 「红旗」是该项目的**内部行话**：`grep 红旗` 命中 10 处**全在 skill 内部指令与专家模块**，用户可见文档（README／BEGINNER-GUIDE／SYSTEM-FLOW／knowledge）命中 **0**；用户侧只问具体症状（`SYSTEM-FLOW.md:94`）。
- 其结构化侧只有 `safety_status` 枚举 + `limitations`／`stop_signals`，**不存在「症状 vs 其他身体状态」的二分字段**；且它自身缺 unknown 档（本项目 `Fact` 三态更强，不得退回）。其 `caution` 枚举与 `caution_flags` 是同类「字段存在但无消费方」问题，不引入无校验后果的中间档。
- 证据：`knowledge/01-lzheng-safety-and-scope.md:13,25`、`SYSTEM-FLOW.md:94,169-190`、`skills/lzheng-fitness-plan/references/plan-contract.md:11`。
- 原话：「1. 你派个subagent去调研../../githut-repos/Lzheng-fitness怎么处理这个red flag的，我这个red flag是从这个项目借鉴的。」；对术语的质疑：「告诉我红旗症状这四个字，有没有替代的说法，连我一个街健的都不知道这个是什么东西，更不要说是小白了」

**实施方式与授权（已拍）**：立即实施（「现在拍吧。」）；走 workflow + 独立 reviewer（「写workflow开工 还有reviewer」）；reviewer 模型 `commandcode/gpt-5.6-sol:medium`（「不要用sol: high，用sol: medium」）；授权前端一并修改（「前端的也可以改。」）。

**撤回经过（2026-09-10）**：实施中发生编排事故（两个 workflow 并发、4 个 writer 同时写同一批文件）；主会话接手手改后语义仍不自洽（最典型：清单外症状的读取语义与 `stage1.md` §7 已拍口径「清单外症状不阻断但需澄清」冲突）。用户指令撤回（「停，别改了。撤回所有改动。」「撤回这次对话的改动」）：22 个文件回退，后端回到 `396 passed`、前端 `npm run build` 通过，未新建证据文档。备份：`/tmp/fitagent-merge-revert-20260910-184844/`（完整 diff + 24 文件副本）。

**重新实施时须一并解决的已核实事实**：见 §7（前端 mock 语义分叉）与 §11（完成门槛）。

## 1. 实施口径（已拍 1A／2A，2026-09-10）

### 1.1 旧档案兼容方式

**已拍 A：读旧写新，不新增数据库迁移。**

- `profile_from_json()` 同时接受旧版 `body_state + red_flags`。
- 读取时合并、去重为新 `body_conditions`。
- `profile_to_json()` 只输出新结构。
- 用户下次确认档案变更时自然完成格式升级。
- 不改 `user_profile` 表，不增加迁移编号。

旧数据合并规则：

| 旧状态 | 新状态 |
|---|---|
| 两项都是 `unknown` | `unknown` |
| 两项均明确为空／否认 | `denied` |
| 任一项有报告内容 | `known(合并去重后的原文)` |
| 一项 unknown、另一项已回答 | 保留已有内容为 `known`；「历史信息不完整」只作文档／代码注释，不新增运行期标记字段 |

**「明确无」的两种写法折叠为一种（2026-09-10 口径 1A 补完）**：旧字段的「明确无」在旧 HTTP 契约里有两种等价写法——`denied`（`value: null`）与 `known(())`（`value: []`，即 S2-01 契约映射所称「显式空集合」，与 `denied` 语义相同）。合并时一律归一为 `denied`，不做保真分支：

| 旧状态 | 新状态 |
|---|---|
| `unknown` + `denied` | `denied`（已有否认结论，保留） |
| `unknown` + `known(())` | `denied`（同上，归一为同一表达） |
| `denied` + `known(())` | `denied` |

两者在序列化、`assess_red_flags` 与首次建档完整性判定中行为完全一致，故不区分；「明确无」只保留一种表达（合并要消除的正是同一件事的两种篮子）。**注意只影响旧数据兼容的读入路径**；新写入的 `denied` 与 `known(())` 仍是两种合法事实状态，不折叠。

未采纳选项（备查）：

- B：SQLite 迁移直接改 JSON——风险更高且没有必要（单用户本地库，读旧写新已足够）。
- C：不兼容旧 JSON——已有档案无法读取，不可接受。

### 1.2 3b 的表达方式

**已拍 A（口径 2A）：读取时派生展示，不新增存储字段。**

- 有身体情况、但分类结果既无红旗、也无 LLM 提议限制时，草稿卡显示「已记录身体情况，本次未提议动作限制」。
- 这是**展示结论**，不写进正式档案。
- 不给 `SafetyCheckResult`、`ActionRestriction` 增加字段。

该结论已于 2026-09-10 以口径 2A 确认，`PLAN.md` 已同步（问题 4 末行与「实施口径」节）；不必重开讨论。

## 2. 规格正本同步

**先改规格，再动代码**，避免再次出现代码与 Stage 文档互相矛盾。

> 执行状态（2026-09-10）：本节七个写入项已回填至三个正本（`02-profile-security.md` §2.1/2.3、`stage2.md` §4.3/§8、`frontend/plans/stage1.md` §1/§3/§5/§7/§8）；`pre-prj/design-decisions.md` 未改；代码一行未动。

修改文件：

- `pre-prj/architecture/02-profile-security.md`
- `pre-prj/stage/stage2.md`
- `frontend/plans/stage1.md`

写入内容：

1. 档案事实由九项变八项：删除 `body_state`、`red_flags`，增加 `body_conditions`。
2. `body_conditions` 保留 `Fact` 三态（unknown／denied／known），不得退化。
3. 六类清单只在**读取／安全检查时**分类；存储层不做医学判断。
4. `SessionConditions` 使用同一 `body_conditions`。
5. 首次建档**不单独追问动作限制**：`action_restrictions` 仍在必填清单（必须有结论），结论由身体情况推出。
6. 明确职责边界：LLM 提议限制与理由链；确定性代码负责红旗分类、限制命中、正式提交校验；用户确认后才生效。
7. Stage 4 输出 schema 约束：只描述观察到的风险、不做疾病诊断；输出结构为「当前结果／需要暂停的内容／原因／你下一步做什么／状态」。

不改动 `pre-prj/design-decisions.md` 与历史 evidence；历史 evidence 不改写为新证据。

## 3. 后端领域模型改造

主要文件：

- `backend/domain/profile/schema.py`
- `backend/domain/profile/rules.py`
- `backend/domain/profile/safety.py`
- `backend/domain/profile/service.py`

### 3.1 档案结构

`schema.py`：

- `Profile` 删除 `body_state`、`red_flags`；新增 `body_conditions: Fact[tuple[str, ...]]`。
- `SessionConditions.red_flags` → `body_conditions`。
- `FACT_VALUE_KINDS` 改为八项。
- `FIRST_TIME_REQUIRED_FACT_FIELDS` 改为八项。
- `EXPLICIT_NONE_FACT_FIELDS` 保留 `available_equipment`、`action_restrictions`、`body_conditions`。
- 删除 `reported_red_flags`、`unlisted_red_flag_labels` 这类把分类结果挂在存储模型上的属性。

身体情况只保存用户报告**原文**，不在 schema 层分类。

### 3.2 兼容旧 JSON

`profile_from_json()`：

- 新版八字段直接解码。
- 旧版九字段调用一个最小兼容函数：合并 `body_state` 与 `red_flags`、保序去重、生成 `body_conditions`。
- 其他缺字段／未知字段仍按数据损坏拒绝。

`profile_to_json()` 只输出新版八字段。

不新增兼容类、版本注册表或通用迁移框架。

### 3.3 结构校验与补丁

`rules.py`：

- `body_conditions` 复用现有 `text_list` 校验。
- `ProfilePatch` 自动随 `FACT_FIELDS` 接受新字段。
- `validate_session_conditions()` 校验 `SessionConditions.body_conditions`。
- 继续禁止把 `SessionConditions` 当长期补丁。
- 补丁应用保持纯内存，不改输入、不推进版本。

## 4. 安全分类改为读取时执行

核心原则：

```text
存储：      深蹲时膝盖锐痛
读取时分类：confirmed=锐痛 / unlisted=其他未匹配描述 / blocked=true
```

`backend/domain/profile/safety.py`：

1. `assess_red_flags()` 的输入从三个 `red_flags Fact` 改为三个 `body_conditions Fact`：正式档案、拟议补丁、当次条件。
2. 每次调用时执行六类清单匹配：胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛。
3. 输出复用现有结构：`confirmed`、`unlisted`、`unknown_sources`、`is_blocked`、`needs_clarification`。
4. 保持既有合并规则：任一来源命中即阻断；另一来源的 denied 不覆盖；清单外内容不判安全、进入澄清；不写档案；不新增／解除限制。
5. `evaluate_safety()`：从 `profile.body_conditions`、`patch.facts["body_conditions"]`、`session.body_conditions` 读取；动作限制检查逻辑不变。

匹配边界：首轮只做已拍六类的确定性匹配，不扩充医学词典。**不要把前端现有的大量正则搬进领域层形成新的医学规则。**

## 5. 草稿、确认与 API 契约同步

主要文件：`backend/app/drafts.py`、`backend/app/confirm.py`、`backend/api/dto.py`，以及 Stage 2 测试。

现有代码大部分通过 `FACT_FIELDS` 通用遍历，主要工作是更新字段契约与测试。

1. 草稿 Diff：原 `body_state`、`red_flags` 两行合并为 `body_conditions` 一行。
2. 首次建档完整性：九项降到八项；`body_conditions=unknown` 拒绝首次确认；`denied` 或 `known(())` 均视为已回答；`action_restrictions` 仍必须非 unknown。
3. API：请求与响应只暴露 `body_conditions`；新版请求不再接受 `body_state`／`red_flags`；数据库旧 JSON 由 codec 兼容，**不由 HTTP 契约兼容**；未知字段继续明确拒绝。
4. 确认事务不变：最终结构复查、版本检查、一次确认只 `context_version +1`、重复确认返回原凭据、安全分类结果不写入正式事实。

## 6. 前端正式契约与页面

主要文件：`frontend/src/lib/contract.ts`、`frontend/src/lib/profile.ts`、`frontend/src/features/profile/ProfilePage.tsx`、`frontend/src/features/chat/DraftCard.tsx`。

契约：`physical_state.red_flags` + `physical_state.notes` → `body_conditions: string[]`；「缺省＝未知、空数组＝明确无」的表达方式保持不变。

档案页：

- 只显示一项「身体情况」；非空逐条显示原始报告，空显示暂定文案。
- 不再向用户显示「红旗」术语。
- 阻断状态由安全复核投影展示，**不得用 `body_conditions.length > 0` 判断红旗**。

草稿卡：

- 身体情况允许内联纠错；动作限制仍允许增删（Stage 2 已拍 2B）。
- 增加理由链展示：`身体情况 X → 提议限制 Y`。
- 未提议限制时显示 3b 结果。
- 理由链只解释本次提议，**不写入 `ActionRestriction`**（不加 `reason`／`source` 字段）。

**理由链粒度（2026-09-11 用户拍板：不做逐条对应映射）**：理由链只展示「本次报告的身体情况**集合** → 本次提议的限制**集合**」，**不建立「条件 i → 限制 j」的对应关系**。

- 禁止逐条映射的理由：逐条对应要么靠医学推断（属被禁止的通用推断，见 §12），要么靠新增只读展示字段（属方案排除项）；两者都不做。
- 因此**不新增契约字段**（`ProfileDraftPayload` 不加映射字段），也不给 `ActionRestriction` 加 `reason`／`source`。
- 语义边界：理由链表达的是「本次草稿的上下文」，**不是因果论断**。展示时不得暗示某条身体情况必然导致某条限制（例如不得把同一串身体情况按限制条数重复打印，以免读成逐条对应）。
- Stage 4 的 LLM 边界同理：只负责「列出报告」与「提议限制」，不负责解释哪条导致哪条（见 §8）。

## 7. 前端 mock 重构

主要文件：`frontend/src/mock/server.ts`、`frontend/src/mock/plan.ts`。这是最容易再次产生语义分叉的部分，**最后改**。

1. `OnboardingState`：`physical_state.red_flags/notes` → 单一 `body_conditions?: string[]`；事实顺序仍为八项；删除独立动作限制追问文案；身体情况问题一次覆盖当前不适、是否影响动作、六类安全症状、无则明确否认。
2. 写入只保存原文：删除写入阶段的 `RED_FLAG_PATTERNS → red_flags` 分类；「深蹲时膝盖锐痛」保存原文，而不是只保存「锐痛」。
3. 模拟 LLM 提议限制：mock 用少量固定剧本模拟限制提议与理由链，限制进入 `action_restrictions` 草稿，确认前不写正式限制。**不做通用医学推断器，也不放进后端确定性规则。**
4. 计划安全检查：`buildPplDraft()`、`planPayloadError()`、`reviewPlanSafety()`、训练指导 mock 全部改用同一读取时分类函数。正确逻辑是 `classify(body_conditions).confirmed.length > 0`；禁止 `body_conditions.length > 0 ⇒ 红旗`。普通身体情况非空但未命中六类时：不阻断为红旗，返回需澄清或展示 3b；动作限制仍按已确认的 `action_restrictions` 单独执行。

## 8. Stage 4 预留边界

本轮不提前建设 Agent Runtime，只把交接契约写清：

- LLM 输出至少包含：身体情况原文、拟议动作限制、每条提议的理由链、无限制提议时的显式结论。
  - 理由链为**集合级**展示数据（本次身体情况集合 → 本次限制集合），**不建立逐条对应**（§6 已拍）；但每条提议限制仍须有可读的说明文本，不得只给限制名。
- 正式提交内容只有 `body_conditions` 与 `action_restrictions`。
- 理由链是草稿展示数据，不进入长期档案。
- 输出 schema 必须约束「描述观察、不做疾病诊断」。
- 不新增通用提议框架、医学知识库或永久理由关系表。

## 9. 测试计划

### 9.1 后端最小必测

`test_stage1_profile_facts.py`：

- 新档案字段恰八项。
- `body_conditions` 三态正确。
- 旧九字段 JSON 能读取并合并。
- 新写入只产生八字段结构。
- 非法新旧混合结构拒绝。

`test_stage1_profile_patch.py`：

- 长期身体情况补丁纯内存。
- 当次身体情况不进入长期补丁。
- `SessionConditions.body_conditions` 校验。
- 输入对象不被修改。

`test_stage1_profile_safety.py`：

1. 普通情况非空但未命中六类 → 不阻断红旗。
2. 六类逐项命中。
3. 清单外报告需要澄清。
4. 正式／拟议／当次任一命中均阻断。
5. denied 不覆盖其他来源命中。
6. 分类过程不写档案。
7. 动作限制命中逻辑不变。

Stage 2 测试：

- 首次建档八项完整性。
- 身体情况与动作限制均可纠错。
- Diff 只出现一个身体情况字段。
- 确认、幂等、过期、回滚、重开保持原行为。
- 旧存量档案读取后可生成并确认新格式草稿。
- `context_version` 仍只由确认事务推进。

### 9.2 前端验证

项目当前没有前端测试框架，**不新增**（沿用存档 B6）。

```bash
npm run build
```

最小人工剧本：

1. 从空档案开始建档。
2. 不再单独追问动作限制。
3. 报告普通身体情况但无红旗命中。
4. 草稿卡显示身体情况与 3b。
5. 报告「膝盖锐痛」。
6. 草稿卡显示身体情况、限制提议与理由链。
7. 确认前档案页不变。
8. 确认后身体情况与限制落盘。
9. 计划生成／指导被红旗确定性阻断。
10. 普通非红旗身体情况不被 `length > 0` 错误阻断。

## 10. 执行与 Review 编排

按已拍授权，**只启动一个 workflow，串行单写者**：

1. Worker A：规格正本同步。
2. Worker B：后端 schema、兼容 codec、rules。
3. Worker C：后端 safety、API、测试。
4. Worker D：前端契约、页面、mock。
5. 独立 reviewer（`commandcode/gpt-5.6-sol:medium`），只读审查。
6. 主会话修复 reviewer 的 P0／P1。
7. 复跑全量验证。
8. reviewer 二次复审。

禁止：

- 两个 workflow 并发。
- 多个 writer 同时写共享工作区。
- reviewer 模型不可用时静默降级（上次全部落到 fallback 模型，结论只作线索）。
- 编排失败后主会话未经说明直接接管大面积修改。

## 11. 完成门槛

必须同时满足：

- 设计正本、后端、前端契约语义一致。
- 后端全量测试不低于当前基线 `396 passed`，新增测试后总数应增加。
- `npm run build` 通过。
- `lens_diagnostics mode=all` 无阻断错误。
- 独立 reviewer 无 P0／P1。
- Windows 同版本后端全量测试通过。
- 人工建档与计划安全剧本通过。
- 未新增依赖、数据库表、医学规则、限制状态或正式写入旁路。

## 12. 明确不做

- 不新增数据库迁移或表（第 1.1 节方案 A）。
- 不新增限制状态语义、限制 `reason`／`source` 字段。
- 不新增前端测试框架、依赖或浏览器 E2E。
- 不建通用提议框架、医学知识库、通用组合草稿系统。
- 实施阶段不修改 `pre-prj/design-decisions.md`、历史 ADR 与已有 evidence 结论（正本同步已完成，见 §2 执行状态）。
- 不提前实现 Stage 3／Stage 4 业务（Agent Runtime、计划生成、记录、统计）。
