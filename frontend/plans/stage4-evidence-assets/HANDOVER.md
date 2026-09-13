# Stage 4 进度交接（2026-09-13，协议级结项）

> 给新对话：**Stage 4 已协议级结项**（owner 确认计划 + 浏览器 §7 全剧本走查通过，2026-09-13）。F4-01–F4-07 全部完成。只改 `frontend/`，mock 演示。
> 主证据文档：`plans/stage4-evidence.md`。真实联调归 Stage 6。

## 已完成

| 任务 | 证据 | 结果 |
|------|------|------|
| F4-01 契约/mock 修订模型/种子收口 | `f4-01-probe-out.txt` | f4-01 **30/0** |
| F4-02 对话定位与更正草稿 | `f4-02-probe-out.txt` | f4-02 **31/0** |
| F4-03 作废草稿与确认 | `f4-03-probe-out.txt` | f4-03 **27/0**；owner 走查通过 |
| F4-04 补全 incomplete 转 valid | `f4-04-probe-out.txt` | f4-04 **22/0**；owner 走查通过 |
| F4-05 记录页修订追溯与统计/复盘刷新 | `f4-05-probe-out.txt` | f4-05 **31/0**；owner 走查通过 |
| F4-06 过期草稿拦截、一键重算与幂等 | `f4-06-probe-out.txt` | f4-06 **34/0**；owner 走查通过 |
| F4-07 闭环演示与证据 | `f4-07-evidence-probe-out.txt` + `stage4-evidence.md` | f4-07 **11/0**；owner 走查通过 |

F4 合计 **PASS=186 / FAIL=0**。构建：`tsc -b` 与 `npm run build` 均为 0。

## F4-07 结论（2026-09-13）

按 stage4 §7 做协议级覆盖核对；对未覆盖分支补最小探针 `scripts/f4-07-evidence-probe.mjs`（**零业务代码 diff**）：

- **补口**：empty 种子下更正/作废/预填文案均无定位对象时不生成草稿、不推 cv（此前 f4-01–06 仅 default 种子）。
- **源码断言**：RecordsPage「发起更正」入口与 prefill 模板；CHECKIN 含 更正|数据有误|作废。
- **归档**：`plans/stage4-evidence-assets/` 下 f4-01–07 与 f3/f2 回归输出 + `f4-07-build.log`。
- **边界（仍未验证）**：真实链路/Windows/复盘重新生成（evidence §5–6）。

## 关键实现锚点（新对话直接用）

- 种子身份：`ts-seed-0831` / `0902` / `0907` / `0905`（09-05 缺 reps → incomplete）
- 契约：`TrainingRecord.status` 含 `voided`；`TrainingRevision`；`TrainingVoidPayload` + kind `training_void`；`RecalcRequest {client_request_id}`
- mock：`record_revisions` append-only + `rebuildRecordProjection`；`isVoidedIdentity`；`recordSetToFacts` / `setFactsToRecordSet`
- 更正剧本：`correctionScriptReply`；CHECKIN 含 `更正|数据有误`；预填文案见 `RecordsPage.tsx`
- 作废剧本：`voidScriptReply`；意图 `VOID_INTENT = /作废/` 优先于更正
- 空种子控制面：`POST /api/dev/reset {"seed":"empty"}`（与 default/noplan 同一机制）
- 失败注入：`POST /api/dev/confirm/fail-next`
- dev 桥：`POST /api/dev/drafts/training-void`（非契约业务端点；探针/失败注入仍依赖，暂留并注释）

## 派生规则实现口径（勿静默改回）

`deriveRecordDraftStatus`：组类型齐全且**至少一组**有次数或时长 → `valid`（可含组级 pending）。相对 05 5.2「每组齐全」偏松，属 stage3 种子模型张力；若要严格口径需 owner 另拍。

## 遗留 / 风险

1. 探针 recalc 调用必须带 `{client_request_id}`，否则 400（契约不变）。
2. dev 桥 `POST /api/dev/drafts/training-void` 暂留（探针依赖）；正式对话路径已就绪，后续可评估退役。
3. 子草稿进入终态后再生成（01 1.6 完整语义）本阶段明确不实现、不演示。
4. 真实后端/Agent/Windows 验收/复盘重新生成 → Stage 5–6，不得在本阶段标已验证。

## 建议下一步

Stage 4 已协议级结项 → 按 roadmap 进入 Stage 5（复盘与后续调整）或 Stage 6（真实联调），需 owner 另行点名授权与细拆计划。
