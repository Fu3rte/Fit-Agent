## 模块说明与不变式

### 1. events.py — 事件契约（地基，先写）

Pydantic discriminated union（`Field(discriminator="type")`）。

必含：`agent_start` / `agent_end` / `agent_settled` / `turn_start` / `turn_end` /

`message_start` / `message_update` / `message_end` /

`tool_execution_start` / `_update` / `_end` / `compaction_start` / `_end` / `auto_retry_*`。

- **不变式**：`agent_end` ≠ 结束，其后可能有重试/压缩/排队消息；判断真正落定用 `agent_settled`。
- **不变式**：`message_update.assistantMessageEvent.type` ∈ 
  `text|thinking|toolcall` × `start|delta|end`。

### 2. loop.py — agent loop

无状态 async generator，消息列表由调用者持有。

收流式事件 → 发 `message_update` / `tool_execution_start` → 并行执行工具 →

结果回灌 messages → 无工具调用则收敛返回。

- **不变式**：loop 不持有状态、不写磁盘、不认识 session 存储。

### 3. harness.py — 大脑外壳

持有 transcript，委托 loop 执行，维护 **steer / followUp 两个 deque**。

- **不变式**：`steer` 在当前 turn/工具批次后注入并跳过剩余；`followUp` 在 run 停止后注入。
- **不变式**：工具异常在工具边界内转成 error result，不冒泡炸 loop。

### 4. tools/base.py — 工具抽象

`AgentTool = name + description + input_schema(Pydantic) + executor + prompt_snippet + prompt_guidelines`。

- **不变式**：返回结构化 `{content: [...], details: {}}`，非裸字符串。
- **不变式**：`prompt_snippet` 常驻 system prompt；`prompt_guidelines` 按需注入。

### 5. cache.py — 缓存断点（三处，克制）

Anthropic 配额 4 个断点，只用在刀刃：

1. system 最后一个 block
2. **最后一个**工具定义（断点语义是"缓存到此为止的全部前缀"，一个标记即覆盖 system+全部工具）
3. 最后一条 user 消息的最后一个 block

OpenAI 侧改发 `prompt_cache_key=session_id` + `prompt_cache_retention`。

- **不变式**：工具集合顺序固定 + 集合哈希；画像内容哈希写入 `<!-- profile:v{hash} -->`， 
  画像未变则不重建 system prompt。**前缀稳定性优先级高于断点数量。**
- **权衡**：压缩会替换 provider 可见历史、开启新 cache epoch → 压缩非越勤越好。
- **不变式**：压缩/分支摘要请求使用**全新 routing session id，并关闭 prompt-cache 写入**。

### 6. compaction/cutpoint.py — 切点算法（已回归验证）

触发：`context_tokens > context_window - reserve_tokens`

（默认 `reserve_tokens=16384`，`keep_recent_tokens=20000`）。

切点**只能在 user / assistant / custom_message**，落在 tool_result 上须回退至配对的调用。

- **不变式**：单个 turn 超预算 → 切点落在 assistant 中间 = **split turn**， 
  需生成两份摘要再合并（turn 前缀摘要 + 历史摘要）。
- **不变式**：重复压缩时，摘要区间从**上一次的 `firstKeptEntryId`** 起算， 
  不是从压缩条目起算。

### 7. session/jsonl.py — append-only JSONL

追加写，崩溃安全，可 `cat` 检视；分支 = 从历史 entry 派生新链。

### 8. skills/loader.py — 渐进式披露

启动只扫 frontmatter（name + description）→ XML 注入 system prompt；

触发时用 read 工具加载完整 SKILL.md；`references/` 按需再读。

- **不变式**：`description`（≤1024 字符）决定加载时机；写在 body 里的 "when to use" 太晚。
- **不变式**：SKILL.md ≤500 行，超出拆 `references/`；路径相对 skill 目录。

### 9. tools/registry.py — 动态注册的代价

pi 中 `registerTool()` 触发 `refreshTools → _refreshToolRegistry → _rebuildSystemPrompt`，

会**破坏缓存前缀**。

- **不变式**：工具集合变更必须走显式版本号，禁止会话中途无版本化地增删工具。

## 实施顺序

events → tools → harness(+loop) → session → compaction → cache → skills → server → web

前 4 步约 700 行可得最小闭环。

```
---

## 给 agent 的验证任务

把下面这段连同本报告和 pi 源码分析报告一起发给执行 agent：
```

markdown

# 验证任务

我有两份材料：

1. 本移植地图（pi → Python，健身 agent 基座）
2. pi 源码分析报告

请以后者为准，逐条核验前者的**每一条不变式**。对每条给出三选一判定：

- ✅ 确认（附 pi 源码位置/函数名佐证）
- ⚠️ 纠正（地图表述有误或不完整，给出正确版本）
- ❌ 缺失（pi 有此机制但地图漏了，补充之）

## 必查清单

**A. 事件契约**

- `agent_end` 与 `agent_settled` 的语义区分是否准确？`agent_end` 后到底还会发生什么？
- 事件列表是否有遗漏？特别关注：branch summary、extension error、 
  bash execution、queue update 相关事件。
- `message_update` 的 delta 类型集合是否完整？

**B. Agent Loop 与 Harness**

- steer / followUp 的注入时机语义是否与 pi 一致？
- pi 的 loop 是否真的无状态？消息所有权在哪一侧？
- CancellationToken 的中断语义：中断发生在工具执行的哪个阶段？
- 并行工具执行时，事件顺序如何保证（start/*update/*end 的顺序约定）？

**C. 工具抽象**

- `prompt_snippet` 与 `prompt_guidelines` 的注入位置和时机？
- 工具结果是否必须结构化？pi 如何处理工具抛异常？
- 动态工具注册破坏缓存前缀这条，源码路径是否如地图所述？

**D. 缓存**

- 三个断点的位置是否准确？有无第四处？
- `PI_CACHE_RETENTION=long` 时 ttl 的具体取值与 provider 差异？
- OpenAI 侧 `prompt_cache_key` 取自什么？
- 破坏前缀的操作清单是否完整？
- 压缩请求是否确实用新 session id 且关闭 cache 写入？

**E. 压缩（重点）**

- 触发公式与默认参数值是否准确？
- valid cut point 的 role 集合是否完整？（地图写了 user/assistant/custom_message， 
  但我在别处看到 pi 文档还提到 BashExecution，请核实）
- split turn 的判定与双摘要合并逻辑是否与源码一致？
- 重复压缩时摘要区间从 `firstKeptEntryId` 起算——这条的准确语义是什么？
- `firstKeptEntryId` 找不到时的 fallback 行为？

**F. Skills**

- 发现规则：`SKILL.md` 存在时是否真的不再递归子目录？根目录裸 `.md` 文件的处理？
- `.agents/skills/` 与 `.pi/skills/` 的规则差异？
- frontmatter 校验哪些字段？校验失败是报错还是静默跳过？
- pi 是否真的没有 Skill tool（靠 read 工具加载）？

**G. Session**

- JSONL 的 entry 类型全集？CompactionEntry / BranchSummaryEntry 的结构？
- 会话树分支与 navigate 的语义？

## 输出要求

1. 逐条判定表（A–G 全部覆盖）
2. 纠正项与缺失项单独汇总，给出可直接替换的修正文本
3. 若发现地图中有**原则性错误**（而非细节偏差），明确指出并说明理由
4. 不要复述地图中已正确的内容，只报差异

```
---

两点提醒：

**E 项是我最不放心的**。我在两处资料里看到过不一致的说法——一处说 valid cut point 只含 user/assistant/custom_message，另一处提到还含 BashExecution。这类边界条件正是照抄时最容易埋雷的地方，务必让 agent 拿源码判定。

**D 项的"破坏前缀清单"同理**。我在社区资料里看到的那份清单未必完整，源码里的 `refreshTools` 链路才是准绳。

验证完把结果发我，我据此修正地图，再动手写代码。
```