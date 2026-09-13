# Stage 6 验收证据（F6-00–F6-04：契约 + 底座门 + Provider + 建档）

> 契约正本指针：`plans/stage6.md`；传输冻结 `stage6-transport-freeze.md`。
> **证据分层**：协议级 / 真实模型闭环（USD 50 内）/ UI=owner 浏览器走查（F6-03、F6-04 **口述确认，无截图**）/ 未改 `pre-prj/` 正本。
> **基线**：HEAD `729d68a`（+ 本批未提交）；Windows；日期 2026-09-13/14。

## 1. 结果正本

| 门 | 命令/结论 | 实际 |
|---|---|---|
| backend 全量 pytest | `backend` `.venv` + `pytest tests` | **953 passed**，EXIT=0（fix 后；修前 1 failed/952 passed） |
| ruff/pyright 业务范围 | check + format --check + pyright | **全绿**（agent_core 全仓残留不在本批范围） |
| 前端类型+构建 | `tsc -b` + `npm run build` | **EXIT=0**（勿用 `npx tsc`，会命中占位包） |
| f6-02 探针 | `node scripts/f6-02-probe.mjs` | **15 PASS / 0 FAIL / 0 SKIP** |
| f6-03 探针 | `node scripts/f6-03-probe.mjs` | **14 PASS / 0 FAIL** |
| 真实冒烟 | `stage6_real_smoke.py --phase smoke`（设置页 PUT api-key） | **PASS**，Run completed，**≈ USD 0.00053**，模型 `qwen3.7-flash` |
| f6-04 探针 | `node scripts/f6-04-probe.mjs` | **10 PASS / 0 FAIL** |
| f6-04 真实建档 | `backend/scripts/f604_profile_loop.py`（1 Run） | **8/8 PASS**，≈ **USD 0.00179**；草稿→revise→confirm→profile |
| Owner 走查 F6-03 | 启动+五页+设置闭环+安全抽样+无 Key 失败 | **通过（2026-09-14，口述确认，无截图）** |
| Owner 走查 F6-04 | 空库建档对话→确认前仍空→纠错→确认→profile | **通过（2026-09-14，口述确认，无截图）** |

## 2. 覆盖对照

| 单元 | 验证了什么 | 结果 | 归档 |
|---|---|---|---|
| F6-01 作废终态/Provider 路由/静态托管/费用账本 | 既有后端实现 + 本批门复验 | 953 passed | 本文件；`stage6.md` F6-01 |
| f6-02 | 真实传输面（sessions/runs/SSE/stats/reviews 等） | 15/0/0 | `stage6-evidence-assets/f6-02-probe.txt` |
| f6-03 探针 | GET 投影无 Key；PUT→true；跨重启；DELETE 幂等；无 Key Run=`model_request_failed`；日志无明文 | 14/0 | `f6-03-probe.txt` |
| f6-03 冒烟 | 同库 Key 进生产 Provider/账本；单轮流式；SSE 无 reasoning 标记 | PASS ≈$0.00053 | 摘要见 §3；脚本 `backend/scripts/stage6_real_smoke.py` |
| Owner 走查 F6-04 | 空库建档对话；确认前档案仍空；纠错确认；`/profile` 更新；对话唯一入口 | PASS 口述 | 本文件 §4 |
| Owner 走查 F6-03 | 静态托管五页；设置页录入/替换/删除/刷新；无明文；无 Key 失败可读 | PASS 口述 | 本文件 §4 |
| f6-04 探针 | 空库 `profile=null`；PUT/POST profile 405；草稿 404；误操作不半写 | 10/0 | `f6-04-probe.txt` |
| f6-04 真实建档 | 对话 Run→`profile_update`；确认前仍 null；revise；confirm 凭据；幂等；409 modified；denied 不伪造 | 8/8 ≈$0.00179 | 本文件 §1/§3；`backend/scripts/f604_profile_loop.py` |

实现指针：`routes_settings.py`；`SettingsPage.tsx`；`f6-03-probe.mjs`；`f6-04-probe.mjs`；`f604_profile_loop.py`。

## 3. 特殊事件

| 事件 | 根因 → 修复 → 回归 |
|---|---|
| 429 Retry-After 测试 flake（期望 7s 实得 1s） | Windows 假服务端 `_ScriptedHandler.do_POST` 未读请求体→关连接 RST→客户端当连接错误默认退避 1s；**非业务回退**。修：先读 `Content-Length` 再应答；断言 `delays==[7.0]` 未动。定向×8 + 全量 953 passed |
| pyright `test_stage3_plan_reads.py:112` | `read_current_plan` 可空；补 `assert on_the_day is not None` |
| 冒烟 env | worker 进程无 `MODEL_*`；从 `.env` 按键名安全注入（不回显）后 PASS |
| 探针防护 | f6-03/f6-04 显式剥离子进程 `MODEL_*` |
| `PUT/POST /api/profile` | 返回 **405**（方法不允许），非 404——断言接受 404/405 |

Owner 拍板（本批）：F6-01 **重跑门**；冒烟 **经设置页路径**；走查 **owner 亲手**。

## 4. Owner 浏览器走查（摘要）

### F6-03（2026-09-14）

| 步骤组 | 结论 |
|---|---|
| 启动静态托管 + 五页导航 | PASS |
| 设置页 Key 录入/替换/删除/刷新 | PASS |
| 界面无明文 Key | PASS |
| 无 Key 对话可理解失败 | PASS |

### F6-04（2026-09-14）

| 步骤组 | 结论 |
|---|---|
| 空库启动与空态 | PASS |
| 对话收集与缺失追问 | PASS |
| 草稿出现且确认前 `/profile` 仍空 | PASS |
| 内联纠错 → 确认 | PASS |
| 确认后 `/profile` 更新 + 对话唯一入口 | PASS |
| 幂等（可选） | 口述通过 |

依据：`stage6.md` F6-03/F6-04；**口述确认（无截图）**。

## 5. 复现

```bash
# backend 门
cd backend && .\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check app api config.py main.py runtime storage domain tests
.\.venv\Scripts\python.exe -m ruff format --check app api config.py main.py runtime storage domain tests

# 前端
cd frontend && .\node_modules\.bin\tsc -b && npm run build
node scripts/f6-02-probe.mjs   # expect 15 PASS 0 FAIL 0 SKIP
node scripts/f6-03-probe.mjs   # expect 14 PASS 0 FAIL
node scripts/f6-04-probe.mjs   # expect 10 PASS 0 FAIL

# 真实（需 MODEL_*；勿打印 Key）
# .\.venv\Scripts\python.exe scripts\stage6_real_smoke.py --data-dir <账本> --phase smoke
# .\.venv\Scripts\python.exe scripts\f604_profile_loop.py   # 建档闭环
```

- 入库：本文件、`f6-02/03/04-probe.txt`、探针脚本、`f604_profile_loop.py`、`stage6_real_smoke.py`。
- 不入库：`*.log`；任何 Key 明文；临时走查/编排文档（已并入本文件）。

## 6. 缺口（红线：不得当成已验证）

1. **F6-05–F6-08 业务闭环未做**（计划/打卡更正/复盘/渐进接回）。
2. **F6-09 安全/失败/重启完整清单未做**。
3. **F6-10 Windows 成品验收未做**。
4. **F6-11 整阶段结项未做**。
5. **走查口述、无截图**（F6-03/F6-04）。
6. **agent_core 全仓 ruff 残留**未纳入本批门。
7. **多账本目录**各起 $50 账本，**不构成新付费授权**。
8. **f6-04 真实闭环为工具路径草稿 Run（1 次模型调用）**，非多轮自由对话全剧本；红旗浏览器分支未单独记。

## 7. 结论

**F6-00–F6-04 完成（协议 + 真实建档 + owner 走查，2026-09-14）**。Stage 6 **未结项**；不得写「已交付」。
