# Stage 1 证据与交接（Linux/WSL2 · 2026-09-09）

> 单一正本：合并原 S1-01～S1-05 证据与 S1-07 交接。每项含平台、代码版本/差异、实际命令、退出码、结果、未覆盖范围（`stage0.md` §6 证据记录格式）。
> Windows 验收清单另见 `S1-windows-checklist.md`（本轮未执行）。本文件不构成结项声明。

## 0. 结论

- Stage 1「动作目录 + 档案 + 安全限制」领域与存储基础（S1-01～S1-07）在 Linux/WSL2 交付并验证。
- **未结项**：方案 A 要求同版本 Windows 全量自动化 + 隔离库人工实测，本轮未执行。
- 全量 **243 passed，退出码 0**。
- S1-02 曾按方案 B 整段回退，2026-09-09 按方案 A（测试断言随 schema 版本维护）重新落地；回退/重开历史见 `stage1.md` §9.3/§9.5。

## 1. 代码版本与工作区（实测）

| 项 | 值 |
|---|---|
| 开工基线 | HEAD `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e` |
| Stage 1 交付 | 见 `git log --oneline -1 -- backend`（整合前为 `d93fec3`） |
| 平台 | Linux WSL2 x86_64；Python 3.13.15；pytest 9.1.1；uv 0.12.3 |
| backend 差异 | 23 条（11 `M` + 12 `??`） |
| 迁移 | `001`–`003`（无 004+） |
| 新增依赖 | 无（`pyproject.toml`/`uv.lock` 未改） |
| 暂存区 | 空 |

## 2. 测试基线（实跑）

```bash
cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
# → 243 passed，退出码 0
```

| 范围 | 用例 |
|---|---|
| Stage 0 回归 | 69 |
| S1-02 结构 | 12 |
| S1-03 动作目录 | 29 |
| S1-04 档案 | 79 |
| S1-05 安全 | 54 |
| **合计** | **243** |

## 3. 迁移与存储（自动化 + 临时库探针）

- 空库 → `user_version=3`；`exercises=24`；`user_profile=(1, None, 0)`。
- Stage 0 库升级：会话/消息/Run/时区/Provider 配置保留，只执行缺失的 002/003。
- 重复启动不重跑迁移、不重置档案/版本/停用状态；失败迁移原子回滚；`user_version=99` 拒绝启动。
- `context_version` 生产代码只读（`domain/profile/repo.py:44`），无写入/递增。
- 种子只 INSERT（003 去注释后 1 条 INSERT、24 行；无 UPDATE/DELETE/CREATE/DROP/ALTER/PRAGMA/`user_profile`）。
- 目录/档案操作不写 `user_profile`；无 HTTP/Agent/CLI 接线（`api/`、`app/`、`runtime/` 零 domain 引用）。

## 4. 子任务验收对照

| 子任务 | 交付 | 验收要点（均已测） |
|---|---|---|
| S1-01 | 基线/契约核对 | 版本、测试基线、可复用入口、12 条未拍依赖清单 |
| S1-02 | `002` 迁移（exercises/user_profile） | 空库可迁移、旧库升级保数据、重复启动不重置、失败回滚、高版本拒绝 |
| S1-03 | `003` 种子 + `domain/actions` | 身份读取、别名多身份保留候选、口径/变式区分、未检查不进推荐、停用保留、种子不改档案/版本 |
| S1-04 | `domain/profile` | 三态事实（unknown≠denied）、`body_weight_kg` 唯一必填、两类限制引用校验、拟补丁纯内存、写入不推进版本 |
| S1-05 | `domain/profile/safety.py` | 模式交集即命中（含多归属）、6 类红旗阻断、三来源独立、清单外需澄清、不自动解除 |
| S1-07 | 本文件 + Windows 清单 | 汇总与交接 |

## 5. 产品种子（24 项，来源逐条核对）

核对依据 = 数据集 `instructions.zh`/`equipment`/`target` 与动作定义一致（非名称相似）。

| # | 中文标准名 | 数据集 | equipment | target | record_type | load_convention | 单侧 | 模式 |
|---|---|---|---|---|---|---|---|---|
| 1 | 杠铃背蹲 | 0043 | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 深蹲 |
| 2 | 杠铃传统硬拉 | 0032 | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 髋铰链 |
| 3 | 杠铃罗马尼亚硬拉 | 0085 | barbell | glutes | reps_weight | barbell_includes_bar_total | 0 | 髋铰链 |
| 4 | 45°腿举 | 0739 | sled | glutes | reps_weight | plate_loaded_total_excluding_empty | 0 | 深蹲 |
| 5 | 保加利亚分腿蹲 | 0410 | dumbbell | quads | reps_weight | dumbbell_per_hand | 1 | 深蹲 |
| 6 | 杠铃平板卧推 | 0025 | barbell | pectorals | reps_weight | barbell_includes_bar_total | 0 | 水平推 |
| 7 | 哑铃平板卧推 | 0289 | dumbbell | pectorals | reps_weight | dumbbell_per_hand | 0 | 水平推 |
| 8 | 哑铃上斜卧推 | 0314 | dumbbell | pectorals | reps_weight | dumbbell_per_hand | 0 | 水平推＋垂直推 |
| 9 | 坐姿哑铃肩推 | 0405 | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 垂直推 |
| 10 | 哑铃侧平举 | 0334 | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 肩孤立 |
| 11 | 哑铃反向飞鸟 | 0383 | dumbbell | delts | reps_weight | dumbbell_per_hand | 0 | 肩孤立 |
| 12 | 杠铃俯身划船 | 0027 | barbell | upper back | reps_weight | barbell_includes_bar_total | 0 | 水平拉 |
| 13 | 坐姿绳索划船 | 0861 | cable | upper back | reps_weight | machine_pin_displayed_value | 0 | 水平拉 |
| 14 | 单臂哑铃划船 | 0292 | dumbbell | upper back | reps_weight | dumbbell_per_hand | 1 | 水平拉 |
| 15 | 高位下拉 | 0198 | cable | lats | reps_weight | machine_pin_displayed_value | 0 | 垂直拉 |
| 16 | 自重引体向上 | 0652 | body weight | lats | reps_bodyweight | — | 0 | 垂直拉 |
| 17 | 坐姿腿弯举 | 0599 | leverage machine | hamstrings | reps_weight | machine_pin_displayed_value | 0 | 膝屈 |
| 18 | 腿屈伸 | 0585 | leverage machine | quads | reps_weight | machine_pin_displayed_value | 0 | 膝伸 |
| 19 | 器械站姿提踵 | 0605 | leverage machine | calves | reps_weight | machine_pin_displayed_value | 0 | 小腿（踝跖屈） |
| 20 | 哑铃弯举 | 0294 | dumbbell | biceps | reps_weight | dumbbell_per_hand | 0 | 肘屈 |
| 21 | 绳索下压 | 0201 | cable | triceps | reps_weight | machine_pin_displayed_value | 0 | 肘伸 |
| 22 | 绳索过顶臂屈伸 | 0194 | cable | triceps | reps_weight | machine_pin_displayed_value | 0 | 肘伸 |
| 23 | 自重双杠臂屈伸 | 0251 | body weight | pectorals | reps_bodyweight | — | 0 | 垂直推＋肘伸 |
| 24 | 悬垂举腿 | 0472 | body weight | abs | reps_bodyweight | — | 0 | 核心 |

- 全部 `recommendable=0`、`active=1`；`source_ref = exercises-dataset:<id>`；`attribution` 逐行保留（媒体声明），文字数据许可为 MIT（见 003 头注释）。
- 「哑铃分腿蹲」2026-09-09 拍板 C 移出清单（数据集无可靠对应），故 24 项。

## 6. Stage 2–4 接入点

| 接缝 | 必须由谁调用 | 说明 |
|---|---|---|
| `ProfileService.write_profile_in_transaction(conn, profile)` | Stage 2 确认事务 | 复用外层事务；不提交、不推进 `context_version` |
| `context_version` 推进 | Stage 2 确认事务 | 一次业务提交仅 +1；Stage 1 只建载体 |
| `ActionCatalogService.resolve` / `recommendation_candidates` | Stage 3 打卡匹配 / 计划候选 | 返回**候选**（多身份保留、可推荐筛选），非匹配结果 |
| `evaluate_safety(...)` / `check_candidate_actions_safety` | Stage 2 确认 / Stage 3 生成与复核 | **局部安全检查**：限制未命中 + 红旗未收集仍 `needs_clarification`；不构成完整安全许可 |
| `preview_patch(...)` | Stage 2 草稿/Diff | 纯内存；其 `action_restrictions` 在补丁触及限制时写 `known`，调用方须另判正式三态 |
| 动作歧义询问 / 整份计划阻断 / 打卡匹配交互 | Stage 3 / Stage 4 Agent | Stage 1 只给确定性基础 |

共享契约导入路径：`domain.actions.rules`（`MODE_VOCABULARY`、`EXERCISE_MODES`、`modes_for`、`validate_modes`、`RECORD_TYPES`、`LOAD_CONVENTIONS`、`is_unilateral`）；`domain.actions.service.ActionCatalogService`；`domain.profile.safety.evaluate_safety`；`domain.profile.schema.profile_from_json`。

## 7. Windows 验收

见 `S1-windows-checklist.md`：同版本自动化 + 隔离库人工（空库/升级/停服重开/回环与 Host/Origin/种子核对）。**本轮未执行**，未填写判定前 Stage 1 不结项。

## 8. 残留风险与未覆盖

- **Windows 未实测**（方案 A 结项门槛）；Stage 0 系统时区切换仍为豁免未验。
- **Stage 2–4 接线未做**：确认事务、草稿/Diff/幂等、计划、记录、统计、HTTP/Agent/前端接口。
- **P2 遗留**：① `validate_patch` 限制分支未查重复项；② `preview_patch().action_restrictions` 见 §6。
- **flake**：`test_app` 进程内用例 1/11 偶发，10 次复跑全绿；留 Windows 轮复现。
- **reviewer 口径**：本轮 reviewer 全部落 fallback 模型（`deepseek/...-flash`，配置的 gpt-5.6-sol 不可用）；结论以父会话独立探针与边界核查为准。
- **未拍**：可推荐检查标准、数值/医学阈值、红旗解除语义、中文口语别名、`leverage machine` 口径复核。
- **媒体**：未导入/下载/建字段。

## 9. 声明

离线验证 ≠ 对话建档/确认事务/训练指导闭环/产品端到端验收；本阶段完成不等于完整开发获批。
