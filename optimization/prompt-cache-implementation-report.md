# 缓存优化实施简报

任务进度、关键实现与验证集中记录于此；方案与契约仅在设计变更时更新。

## 进度

- 任务 1 已完成：类型、强校验、schema 14 持久化、分支读取与公开身份投影；旧会话不回填。
- 任务 2 已完成：资料采集、Snapshot / Delta、追加位置、SSE 通知与前端隐藏节点合并。
- 任务 3 已完成：原位置 user-role 转换、真实分支重放、稳定请求头部与完整预算。
- 任务 4 已完成：压缩新基线、业务引用与生命周期恢复。
- 任务 5 已完成：两条适配器及装配链路已接通，底层流释放、分类用量及命中率均经本地定向复核。
- 任务 6 已完成（既定工程回归范围）：七组场景及执行归属已有记录；多工具与 Steering 组合、确认展示及确认链路通过定向验收，最终复核 5 条命令全部 exit 0。

## 任务 2 关键事实

- `application/session/business_context.py` 负责采集、分支状态重放与增量生成；日期取真实用户节点，消息来源取模型可见集合，提案核验完整分支，无变化不追加。
- `BusinessService.removed_proposal_reason()` 只读核验：`out_of_scope` 为 pending/processing 提案离开有效集合；`deleted` 为快照缺失且无保存记录。
- 资料提交后同步执行位置并发送 `custom_message` 五字段身份通知；完整工具批次及 Steering 消费通知在前。Python/TypeScript 事件、`ReActRound.hidden` 与历史合并已接通。

## 任务 3 关键事实

- `business_context_to_llm()` / `convert_to_llm()` 保留固定 content 与 timestamp；`build_effective_context()` 按原位置投影并映射真实资料节点 ID。
- `transform_context()` 从持久化分支重放，每条资料出现一次；动态业务 system 注入已移除。资料不进入 message_nodes、不作真实用户回合或切点。
- `read_request_state()` 共用只读资料采集；`request_messages()` / `estimate_request()` 计入本次待追加资料，固定文本创建与预算共用 `build_business_context_message()`。无新增依赖、迁移或前端改动。

## 任务 4 关键事实

- 摘要输入与保留预算包含资料节点固定 content；资料节点保持非切点、非真实用户回合。
- `commit_compaction()` 同事务提交检查点与完整 Snapshot 基线，运行末节点及会话叶指向基线；凭据拒绝、失败回滚与同身份幂等均有局部验证。
- 通知顺序为 `compaction_end → custom_message`，资料通知保持五字段白名单；新基线按模型可见集合重建消息来源，待确认提案继续核验完整分支。
- 本次测试适配仅修改 `check_business_context_compaction.py` 与 `check_overflow_recovery.py`；新增连续压缩、压缩后独立按钮确认及越权零提交断言。

## 任务 5 关键事实

- `app/agent/cache_observation.py` 为唯一观测模块，承载最终投影指纹、差异定位、真实调用记录、跨运行/重启比较链、读取汇总及 CLI。
- 普通调用绑定真实 `message_id`，历史摘要与任务前缀摘要分别绑定 `compaction_id`；两条适配器在最终请求投影处调用 `observe()`，流包装层登记异常、中断及缺失用量结局。
- 命中率采用 `cache_read / (input + cache_read + cache_write)`，按真实调用身份去重；持久化指纹、身份和用量，endpoint 清洗及凭据过滤在写入边界生效。
- 生产产物位于 `backend/data/cache-observation/`，测试产物位于 `backend/temp/cache-observation/`，均已 gitignore；读取入口为 `python -m app.agent.cache_observation [--directory D] [--session S]`。
- 公开历史、SSE 白名单、前端配置与数据库结构保持现状。
- `_observed()` 通过 `aclosing(source)` 承担底层流关闭责任，提前关闭、取消和异常退出均完成资源释放，观测结局只写一次。
- `_usage_totals()` 与 `_bucket()` 为 first/append/summary/reset 分别输出调用数、有效/缺失用量数、四类 token 及命中率；保持真实调用去重、组内求和及零分母 null。

## 任务 6 关键事实

- 执行者报告仅调整测试与渲染夹具：`check_steering_consumption.py` 显式装配共享 `RequestBudget`，`check_profile_snapshot_store.py` 跟随 `SCHEMA_VERSION`，`check_business_context_runtime.py` 增加消费、资料位置、父子关系与请求断言；生产代码与契约保持既有范围。
- `scenario_steering_and_batch` 首轮包含 `read-1` 与 `prepare-1` 两个工具调用；13 项事件序列验证两条结果全部提交与通知后才消费 Steering。断言覆盖独立结果身份、最后结果后的消费位置、资料与助手父子链、提案展示绑定、请求身份与业务日期，以及用户输入只合并一次。
- `confirmation-display-check.mjs` 直接导入 `frontend/scripts/confirmation-display-tree.tsx`，夹具装配真实 ChatTranscript、TooltipProvider 与 QueryClientProvider。该源文件未被忽略，当前为未跟踪文件，需随统一 Git 交付纳入；本轮未暂存或提交。
- 负向对照将两工具分配给两次模型请求，整批事件序列与消费位置谓词均不成立；证明范围为组合断言的区分能力，生产实现保持不变。
- 范围外事项独立记录：11 个预算装配检查归请求预算测试维护；独立 Steering 列表按现有 10 字段契约，历史保留附件投影，生产接口本轮未修改。

## 已报告的验证

- 任务 1：压缩持久化迁移检查通过。
- 任务 2 后端：`python -m test.check_business_context_runtime`、`python -m test.check_business_context_store`、`python -m test.check_session_compaction_persistence` 均 PASS、exit 0；runtime 经测试 subagent 复核。5 个后端文件的 `ruff check` 输出 `All checks passed`。
- 任务 2 前端：`node scripts/business-context-stream-check.mjs`、`node scripts/business-context-history-check.mjs` 均 PASS；`npx tsc -b` exit 0。报告未提供 ruff 完整参数及 exit code、两个前端脚本的 exit code。
- 任务 3（测试 subagent）：`python -m test.check_business_context_projection` 及另外 6 项后端模块检查均 PASS；7 个相关 pytest 文件共 51 passed；全部 exit 0。工作目录 `backend`，解释器 `.venv\Scripts\python.exe`。

- 任务 4 执行报告：后端 17 项、前端 4 项全部 exit 0；相关 ruff 与 compileall 通过。包含真实摘要提交，model=`step-3.7-flash`、latency=`17820ms`。
- 任务 4 定向复核（测试 subagent）：后端 `python -m test.check_business_context_compaction`、`python -m test.check_overflow_recovery`，前端 `node scripts/business-context-stream-check.mjs`、`node scripts/business-context-history-check.mjs`、`node scripts/compaction-history-check.mjs`，5 项全部 exit 0。压缩检查首条控制台中文乱码，JSON 产物交叉核验完成。

- 任务 5 执行报告（测试 subagent）：工作目录 `backend`、解释器 `.venv\Scripts\python.exe`；相关 9 个源文件及检查脚本 ruff、`test.check_cache_observation`、`test.check_model_request_dispatch`、`test.check_compaction_budget_counting`、`test.check_message_context`、`test.check_usage`、`test.check_compaction_projection`、`test.check_business_context_projection` 均报告 exit 0。
- 任务 5 本地定向复核（测试 subagent）：工作目录 `backend`、解释器 `.venv\Scripts\python.exe`；`-m test.check_cache_observation`、`-m test.check_model_request_dispatch`、`temp/cache-observation-review/lifecycle_check.py`、`temp/cache-observation-review/summary_check.py`，4 条命令全部 exit 0。
- 生命周期断言：提前关闭、取消及异常路径均完成底层清理，每条路径只记录一次结局；关闭返回前 `source_finally_before_wrapped_aclose_return=True`。
- 分类汇总断言：append 命中率 1、summary 命中率 0，整体为 0.5；first/reset 空分类零分母为 null，缺失用量分类计数有独立覆盖。
- `check_ai_stream` 的完整入口具有远端模型依赖，本轮未执行；用户补充报告的 5 条命令均 exit 0，包含该入口。

- 任务 6 本地定向复核（测试 subagent）：工作目录 `backend`、解释器 `.venv\Scripts\python.exe`；`-m test.check_business_context_runtime`、`-m test.check_steering_consumption`、`-m test.check_profile_snapshot_store`，3 条命令全部 exit 0。
- 任务 6 前端定向复核（测试 subagent）：工作目录 `frontend`；`node scripts/confirmation-display-check.mjs`、`node scripts/business-context-stream-check.mjs`、`node scripts/business-context-history-check.mjs`、`npx --no-install tsc -b`，4 条命令全部 exit 0。
- 独立 Steering 列表限定 GET 探针（测试 subagent）：工作目录 `backend`，同一解释器及 `PYTHONPATH=.` 执行 `temp/task6-review/steering_lists_probe.py`，exit 1；真实本机 HTTP 200，列表项返回 11 字段，比现有断言多 `attachments`，失败于 `check_run_steering_lists.py:199`。原模块含远端运行入口，本轮未执行。
- 原回归日志的 14 个模块段记录 `EXIT=0`；AI context 的 pytest 日志记录 `1 passed`。日志未保存完整命令、cwd、环境及解释器，不能等同于本轮独立重跑。

- 任务 6 最终定向复核（测试 subagent）：后端工作目录 `backend`，使用 `.venv\Scripts\python.exe`，环境 `PYTHONDONTWRITEBYTECODE=1`、`PYTHONIOENCODING=utf-8`；按序执行 `-m test.check_business_context_runtime`、额外设置 `PYTHONPATH=.` 执行 `temp/prompt-cache-review/negative_batch_boundary.py`，2 条命令全部 exit 0。
- 任务 6 最终前端复核（同一测试 subagent）：工作目录 `frontend`，按序执行 `node scripts/confirmation-display-check.mjs`、`npx --no-install tsc -b`、`npm run verify:confirm`，3 条命令全部 exit 0。确认协议、展示和 ledger 检查均实际运行。
- 最终复核原始输出位于 `backend/temp/task6-final-review/` 与 `frontend/temp/task6-final-review/` 的独立运行目录；主会话已核对源码、测试回报及 5 份输出日志，完成既定范围内的验收。

结果依据执行报告、主会话源码核对及测试 subagent 回报。未重跑全部历史回归、P0 套件、远端实时模型、完整 Steering 列表模块及范围外预算检查；浏览器行为、Anthropic 真实端点与真实缓存效果尚未验证，模型能力与远端 TTL 核验暂停。最终业务验收按方案归用户。
