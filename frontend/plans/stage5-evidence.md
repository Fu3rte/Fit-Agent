# Stage 5 验收证据（F5-01–F5-07）

> 对应 `plans/stage5.md` 第 5 节 F5-01–07 与第 6 节证据格式。
> **F5-07 证据层（2026-09-13）**：协议级第 7 节 11 步覆盖核对 + 探针全量重跑归档 + `npm run build` 零错误。
> **证据分层：**
> 1. **协议级**（mock HTTP / 模块级）：§2–§3 实际执行结果，可复现。
> 2. **UI / 浏览器走查**：**owner 浏览器 §7 全剧本走查通过（2026-09-13）**，见 §3「浏览器」列与 §7 结论。
> 3. mock 通过 ≠ 真实链路通过：未接真实后端/模型/数据库/Windows 验收。
> 4. 范围：只改 `frontend/` 证据与探针；未改 `backend/`、`pre-prj/` 决策正本；不 commit。

## 0. 环境与代码版本

| 项 | 值 |
|---|---|
| 平台 | Windows；Node v22.22.3；TypeScript 5.8.3；vite 7.3.6 |
| 代码版本 | 复跑时 HEAD `ee72a18`；Stage 5 改动**在工作树未提交**（F5-01–06 实现 + 本任务证据归档；另修 `scripts/f3-01-probe.mjs` 正则一处） |
| 证据日期 | 2026-09-13（F5-07 协议级闭环归档） |
| mock 运行 | 探针以 vite 程序化启动（随机端口），不触碰用户 5173 |
| 网络 | 进程内 fetch，无外网、无模型调用 |
| 归档产物 | `plans/stage5-evidence-assets/`：`f5-0{1..6}-probe-out.txt`、`f5-07-regression-out.txt`（f4-01 + f4-05 + f3-02 + f3-01）、`f5-07-build.log` |

主要实现文件（F5-01–06，详见各 worker report）：`src/lib/contract.ts`、`src/lib/api.ts`、`src/mock/server.ts`、`src/mock/plan.ts`、`src/features/review/ReviewPage.tsx`、`scripts/f5-0{1..6}-probe.mjs`。  
F5-07 本任务：**不新增业务代码**；未写 `f5-07-evidence-probe.mjs`（§7 空口均已被 f5-01–06 / f3–f4 既有探针覆盖，不硬造）；**探针修正**：`scripts/f3-01-probe.mjs` 启动 `recomputeStats` 断言正则放宽（F5-01 起 `recomputeStats` 与 `return state` 之间有 basis 回填两行，原 `\\s*\\n\\s*return` 过紧；不改业务语义）。

## 1. 构建

| 命令 | 预期 | 实际 |
|---|---|---|
| `node node_modules/typescript/bin/tsc -b --pretty false` | 零错误 | **EXIT=0**（2026-09-13 F5-07 复跑） |
| `npm run build`（`tsc -b && vite build`） | 零错误 | **EXIT=0**，`✓ built in 5.24s`；仅既有 chunk>500kB 警告；归档 `f5-07-build.log` |

## 2. 协议探针（F5-07 全量重跑归档）

方法：沿用 f2–f4 模式（vite 起服务 + `/api/*` + 模块断言）；无测试框架、无新依赖。下列为 **2026-09-13 F5-07 全量重跑**（exit 均为 0，FAIL 均为 0）。

| 探针 | 覆盖 | 结果 | 归档 |
|---|---|---|---|
| `f5-01-probe.mjs` | 契约 ReviewBasis/ReviewEntry；append-only 存储；冻结快照；保存失败整份不落；stale 翻转；empty 空态；无新业务端点 | **PASS=27 / FAIL=0** | `f5-01-probe-out.txt` |
| `f5-02-probe.mjs` | 显式生成/重新生成；模糊不落库；empty 拒绝不编造；fail-next 不落库可重做；cv/plan 不变 | **PASS=31 / FAIL=0** | `f5-02-probe-out.txt` |
| `f5-03-probe.mjs` | `/review` 最新条+basis；stale 徽章源码；预填文案命中生成路径；无输入控件/无历史列表/无 RIR | **PASS=22 / FAIL=0** | `f5-03-probe-out.txt` |
| `f5-04-probe.mjs` | 复盘后建议不自动建草稿；长期/当次草稿；确认/幂等/回滚；复盘 text 不改写 | **PASS=19 / FAIL=0** | `f5-04-probe-out.txt` |
| `f5-05-probe.mjs` | 显式/≥7 天接回；三档；红旗阻断；病后只转介；回归期 PR 排除 | **PASS=29 / FAIL=0** | `f5-05-probe-out.txt` |
| `f5-06-probe.mjs` | 显式渐进；无记录不猜重；最小增量；长期草稿确认链；empty/非显式不给 | **PASS=16 / FAIL=0** | `f5-06-probe-out.txt` |
| f4-01 + f4-05 + f3-02 + f3-01 回归 | Stage 2–4 关键链路未回归（契约/作废/只读看板/安排事务/打卡） | f4-01 **30/0**；f4-05 **31/0**；f3-02 **32/0**；f3-01 **36/0** | `f5-07-regression-out.txt` |

**F5 合计 PASS=144 / FAIL=0**（27+31+22+19+29+16）。  
**回归合计 PASS=129 / FAIL=0**（30+31+32+36）。

## 3. 第 7 节 11 步覆盖对照

前置均为 mock default 种子（mock 日期 `2026-09-11`）。下表「协议级结果」列一律为协议级结论；「浏览器」列另记 owner 走查（2026-09-13 全剧本通过）。

| §7 步 | 主覆盖探针 | 协议级结果 | 浏览器 |
|---|---|---|---|
| 1 统计只读（W1 2/3、W2 1/3、三桶、PR、data_updated_at；无输入控件） | f5-01 种子基线 + f5-03（basis/stats 对齐、源码无 input）+ f4-05 回归 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 2 显式生成（冻结→追加→最新条+generated_at） | f5-02 §1；f5-03 预填→生成路径 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 3 建议不自动生效（生成后正式计划/日程/cv 不变、无草稿） | f5-04 §1（generate 后 draft=0、cv 不变） | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 4 后续调整（意图→草稿 Diff→确认 cv+1；幂等；fail 回滚） | f5-04 §3（plan v3 cancels=12；fail-next 回滚；重复确认 newly_committed=false） | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 5 依据变更与 stale（正文/generated_at 逐字不变+徽章文案） | f5-01 作废→stale；f5-03 徽章源码+更正后 stale；f4-05 回归 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 6 重新生成（再追加；UI 仍只最新） | f5-02 重新生成条数+1、旧条保留 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 7 空数据（empty：「暂无」；拒绝或空态，不编造） | f5-01/f5-02/f5-03 empty 分支；f5-06 empty 加重不猜重 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 8 失败注入（保存失败不落；重做成功） | f5-01 save fail-next；f5-02 对话路径 fail-next 可重做 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 9 刷新恢复（最新复盘与正式状态经查询恢复） | GET `/api/review` 最新投影 + GET `/api/stats` 现算（f5-01/02/03 运行时读回）；草稿侧沿用 f3-06 协议口径（本阶段未另跑 f3-06） | **协议级已覆盖（查询恢复）/ PASS** | **已走查 / PASS** |
| 10 接回与渐进（三档草稿确认+回归期；渐进确认） | f5-05 + f5-06 | **协议级已覆盖 / PASS** | **已走查 / PASS** |
| 11 构建与归档 | `npm run build` EXIT=0 + 本文件与 assets/ | **协议级已覆盖 / PASS** | n/a |

## 4. F5-01–07 验收对照

| 任务 | 验收要点 | 结论 |
|---|---|---|
| F5-01 | 契约唯一来源；append-only；冻结；失败整份不落；stale 口径 | 协议级 PASS（27/0）+ f4 回归；owner 走查通过 |
| F5-02 | 仅显式生成；正文数字=冻结；重生成不覆盖；空数据拒绝 | 协议级 PASS（31/0）；owner 走查通过 |
| F5-03 | 最新条+basis；徽章；预填；只读无 RIR；无新路由 | 协议级 PASS（22/0）；owner 走查通过 |
| F5-04 | 建议不自动生效；复用草稿事务；幂等/回滚 | 协议级 PASS（19/0）；owner 走查通过 |
| F5-05 | 接回触发/三档/红旗/病后转介/回归期 PR 排除 | 协议级 PASS（29/0）；owner 走查通过 |
| F5-06 | 显式渐进；不猜重；最小增量；既有确认链 | 协议级 PASS（16/0）；owner 走查通过 |
| F5-07 | 第 7 节协议覆盖核对 + 证据归档 + build | 协议级 PASS（全量重跑 + 回归 + build 0）；owner 浏览器 §7 全剧本走查通过；Stage 5 已结项（2026-09-13） |

## 5. mock 边界（不得标为已验证）

以下**明确未验证**，不得写成已通过：

- 真实后端复盘 Run（`POST /api/reviews`）、真实 `ReviewStore` 竞态回滚
- 真实模型/Agent 复盘正文解释质量与数字保真
- 真实数据库、真实 HTTP/SSE、Provider
- Windows 验收（owner 本机验收流）
- 09 章 pass³／裁判校准正式测评
- 交接 F1／F5–F9 端点拆分与形状重构

（owner 浏览器 §7 全剧本走查已于 2026-09-13 通过，不再列入未验证项；mock 通过仍≠真实链路通过。）

## 6. 未覆盖 / 已知边界（摘自 F5-01–06 worker reports）

| 项 | 出处 | 说明 |
|---|---|---|
| 当次安排加重草稿 | f5-06 | 安排 `keep`/`deload` 语义限制；仅交付长期 plan 草稿路径，**未拍** |
| 渐进长期草稿基线 = `buildPplDraft` 重生成 | f5-06 | 非种子原样改 load；确认后动作集可能与种子不完全一致（与 F5-04 同链路） |
| 最低版日程投影名额可能 >7 | f5-05 | 复用 `projectSchedules` 未截断；文案写 3–7 天 |
| 完成率仍计回归期记录 | f5-05 | 仅 PR 排除；若 06 章要求完成率也排除需另拍 |
| 档案级红旗接回二次断言弱 | f5-05 | 消息级红旗已覆盖；档案红旗靠既有 buildPpl/planPayloadError 拦截 |
| load 候选用「最近一条达区间上限的 valid 记录」 | f5-06 | 非字面「最近一条记录」（09-07 deload 未达上限） |
| 当次加重处置未拍 | f5-06 缺口 | 触发条件性待拍：必须另拍语义才能实施 |
| 对话流式分段优化 | f5-02 | 正文一次生成；沿用既有 text.split |
| 子草稿终态后再 recalc（01 1.6 完整语义） | Stage4 遗留 | 本阶段未扩 |

## 7. 结论

F5-01–06 协议级完成（各 worker report）；F5-07 完成协议级证据归档：**F5 合计 PASS=144 / FAIL=0**，回归 f4-01/f4-05/f3-02/f3-01 **PASS=129 / FAIL=0**，`npm run build` **EXIT=0**。  
**Stage 5 已结项（协议级 + owner 走查，2026-09-13）**——owner 浏览器 §7 全剧本走查通过 + 整阶段结项确认。  
真实链路、真实 ReviewStore、真实模型与 Windows 验收不在本证据范围（Stage 6 及后续）。mock 通过不得声称为真实链路通过。
