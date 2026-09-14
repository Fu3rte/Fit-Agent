# Stage 6 验收证据（F6-00–F6-09：契约 + 底座 + Provider + 五条业务闭环 + 渐进/安全重启）

> 契约正本：`plans/stage6.md`；传输冻结 `stage6-transport-freeze.md`。
> **证据分层**：协议级可复现 / 真实模型闭环（USD 50 内） / UI=owner 走查 **口述确认、无截图**（F6-03–07 已做；F6-08/09 浏览器待做） / mock≠真实 / 本批未改 `pre-prj/` 正本。
> **基线**：HEAD `729d68a`+本批未提交；Windows；2026-09-13/14；默认模型 **qwen3.6-flash**（见 §3）。

## 1. 结果正本

| 门 | 命令/结论 | 实际 |
|---|---|---|
| backend 全量 pytest | `pytest tests` | **953 passed** EXIT=0 |
| ruff/pyright 业务范围 | check + format --check + pyright | **全绿** |
| 前端类型+构建 | `tsc -b` + `npm run build` | **EXIT=0** |
| 协议探针合计 | f6-02…04、05…07、09 | 15+14+10+11+15+13+**17** PASS；f6-09 **1 SKIP（B8）**；其余 0 FAIL |
| 真实冒烟 | `stage6_real_smoke.py --phase smoke` | PASS ≈**USD 0.00053** |
| f6-04 真实建档 | `f604_profile_loop.py` | **8/8** ≈$0.00179 |
| f6-05 真实计划 | `f605_plan_loop.py` r3 | 核心 PASS；long_term **SKIP**；批总≈$0.037 |
| f6-06 真实训练更正 | `f606_record_loop.py` run3 | **7/7** ≈$0.0517；作废终态 422 复验成立 |
| f6-07 真实复盘 | `f607_review_loop.py` run2 | 核心 **9/9**；s10 **SKIP**；≈$0.0594 |
| f6-08 渐进/红旗/长会话 | `f608_progression_loop.py` run3 | A–D+D8 **PASS**；E 压缩 **SKIP**；接回**跳过**；≈$0.1276 |
| f6-09 重启恢复 | `f609_restart_recovery.py` | **10/10 PASS**（无模型费用） |
| Owner 走查 F6-03–07 | 五项业务闭环浏览器 | **通过（2026-09-14，口述确认，无截图）** |
| Owner 走查 F6-08/09 | 压缩 UI / failureReasonCopy+设置页 | **待做** |

真实模型费用正本累计 ≈ **USD 0.276**（0.037+0.0517+0.0594+0.1276；另冒烟 0.00053、建档 0.00179；各 `--data-dir` 独立账本，不构成新授权；失败 Run 不在正本）。

## 2. 覆盖对照

| 单元 | 验证了什么 | 结果 | 归档 |
|---|---|---|---|
| F6-01 | 作废终态/Provider 路由/静态托管/费用账本 | 953 passed | `stage6.md` F6-01 |
| f6-02 | 真实传输面 | 15/0/0 | `f6-02-probe.txt` |
| f6-03 | Key 投影/跨重启/无 Key 失败 | 14/0 | `f6-03-probe.txt` |
| f6-04 | 空库协议 + 真实建档确认链 | 10/0 + 8/8 | probe + `f604_profile_loop.py` |
| f6-05 | 空库 plan 协议 + 真实 propose→confirm→PPL/日程/guidance | 11/0 + 核心 PASS | probe + `f605_plan_loop.py` |
| f6-06 | 空库记录协议 + 安排/打卡/更正/作废终态 | 15/0 + 7/7 | probe + `f606_record_loop.py` |
| f6-07 | reviews 协议 + 显式复盘/冻结 basis/幂等/stale | 13/0 + 9/9 | probe + `f607_review_loop.py` |
| f6-08 | 无记录不猜重/渐进/红旗双路/多轮/长会话；E 压缩 SKIP；接回跳过 | A–D+D8 PASS | `f608_progression_loop.py` |
| f6-09 | 密钥不回显/失败码/回环 403/重启 interrupted+查询恢复无 SSE 重放 | 17/0/1SKIP + 10/10 | probe + `f609_restart_recovery.py` |
| Owner 走查 | F6-03 五页设置；04 建档；05 计划；06 打卡更正作废；07 复盘 | 口述 PASS | §4 |

实现指针：`routes_settings.py`；`SettingsPage.tsx`；`f6-03`–`f6-09-probe.mjs`；`f604`–`f608_*_loop.py`；`f609_restart_recovery.py`；`stage6_real_smoke.py`。

## 3. 特殊事件（拍板与缺陷链）

| 事件 | 根因 → 决定 → 回归 |
|---|---|
| 429 fixture flake | Windows 假服务端未读 body→RST→默认退避 1s。修 fixture 读 `Content-Length`；全量 953 passed |
| qwen adjustments 序列化 / arrangement 路径 | F6-05 long_term、F6-07 s10 真实模型 `model_request_failed` → **SKIP**，不计 PASS |
| 作废终态真实复验 | F6-06：void rev3 后再 confirm **422**；DB 1 身份 3 修订、草稿不提交 |
| 复盘 SSE 仅 status/heartbeat | 正文经 `GET /api/reviews`；stale=同身份更正触发（新独立身份不触发，未专项注入） |
| **qwen3.7-flash 403 insufficient_quota** | Owner 2026-09-14 拍：**qwen3.6-flash 入目录并设默认**。`models.py`/`fees.py`/`config.py`+4 测试；window 1M 官方核对；max_output/价目按 3.7 保守沿用；定向 80 passed |
| harness.toml 运行中写不生效 | lifespan 启动冻结配置 → F6-08 E **SKIP**。下次须启动前写 toml 或重启进程 |
| 接回三档 | 后端无 tool mode + return 生成器 + 中断检测。Owner 拍：**本批跳过** |
| F6-09 手法 | 夹具 sqlite 插 running Run（非真实模型挂断）；Run DTO **无 message**，以 `error_code` 为准；evil Host/Origin **403**；A4 warning 日志 len=0 **弱信号** |

Owner 本批拍板：F6-01 重跑门；冒烟经设置页；走查 owner 亲手；跳过接回；允许（未生效）降压缩阈值；换 3.6 入目录。

## 4. Owner 浏览器走查（口述，无截图，2026-09-14）

| 项 | 步骤组 | 结论 |
|---|---|---|
| F6-03 | 五页+设置 Key 闭环+无明文+无 Key 失败 | PASS |
| F6-04 | 空库建档→确认前仍空→纠错→profile | PASS |
| F6-05 | 计划对话→确认启用→计划与日程可查 | PASS |
| F6-06 | 安排→打卡→更正→作废终态拒再更正 | PASS |
| F6-07 | /review 冻结数字→建议不自动生效→后续草稿 | PASS |
| F6-08 | 压缩 UI / 长会话浏览器 | **待做** |
| F6-09 | failureReasonCopy、设置页/SSE | **待做** |

依据 `stage6.md` 各 F6-x 工作/验收标准。脚本未覆盖场景不在走查范围，见 §6。

## 5. 复现

```bash
cd backend && .\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check app api config.py main.py runtime storage domain tests
.\.venv\Scripts\python.exe -m ruff format --check app api config.py main.py runtime storage domain tests
cd frontend && .\node_modules\.bin\tsc -b && npm run build
node scripts/f6-02-probe.mjs   # 15/0/0
node scripts/f6-03-probe.mjs   # 14/0
node scripts/f6-04-probe.mjs   # 10/0
node scripts/f6-05-probe.mjs   # 11/0
node scripts/f6-06-probe.mjs   # 15/0
node scripts/f6-07-probe.mjs   # 13/0
node scripts/f6-09-probe.mjs   # 17/0/1 SKIP(B8)
# 真实：先按行注入 MODEL_*（勿 source、勿回显 Key）；默认模型已为 qwen3.6-flash
# cd backend && .\.venv\Scripts\python.exe scripts\stage6_real_smoke.py --data-dir <账本> --phase smoke
# .\.venv\Scripts\python.exe scripts\f604_profile_loop.py --data-dir <账本>
# .\.venv\Scripts\python.exe scripts\f605_plan_loop.py --data-dir <账本>
# .\.venv\Scripts\python.exe scripts\f606_record_loop.py --data-dir <账本>
# .\.venv\Scripts\python.exe scripts\f607_review_loop.py --data-dir <账本>
# .\.venv\Scripts\python.exe scripts\f608_progression_loop.py --data-dir <账本>
# .\.venv\Scripts\python.exe scripts\f609_restart_recovery.py
```

- 入库：本文件、`f6-02`–`07/09-probe.txt`、上述脚本、`stage6_real_smoke.py`。
- 不入库：`*.log`；Key 明文。

## 6. 缺口（红线：不得当成已验证）

1. **走查口述无截图**（F6-03–07）；F6-08 压缩 UI/长会话浏览器、F6-09 文案/设置页/SSE **待做**。
2. **F6-05**：换计划取消旧日程；红旗/限制阻断；long_term **SKIP**。
3. **F6-06**：待补全；无安排独立场景；同日多练；stale→recalc；失败注入；completion；真实 work_sets 差异。
4. **F6-07**：s10 **SKIP**；正文质量走查；真实路径失败注入；新身份不触发 stale 专项。
5. **F6-08**：接回三档**跳过**；E 压缩真实触发 **SKIP**（toml 冻结）；压缩 UI 浏览器；长会话浏览器。
6. **F6-09**：busy/timeout；stale 协议面（B8 SKIP）；failureReasonCopy 走查；pending 重启；有数据后刷新恢复；有 Key 日志压测（A4 弱信号）。
7. **F6-10 Windows 成品验收未做**；**F6-11 结项未做**。
8. **多账本**不构成新付费授权；费用见 §1；失败 Run 花费不在正本。
9. **agent_core 全仓 ruff 残留**未纳入本批门。
10. f6-04 为工具路径 1 次模型调用，非自由对话全剧本。

## 7. 结论

**F6-00–F6-09 脚本层完成**（协议探针可复现；真实闭环 A–D/建档/计划/更正/复盘/渐进红旗长会话/重启恢复均有正本；F6-03–07 含 owner 口述走查）。F6-05–09 各自仍有 §6 未覆盖场景，**不得写整项 PASS**。Stage 6 **未结项**，**不得写「已交付」**。
