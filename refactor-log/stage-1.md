# 阶段 1：最小 Schema 与表单写入

| 子任务 | 情况 | 遗留问题 |
| --- | --- | --- |
| 01：口径与数据基座 | 已完成；以新版 `001_initial.sql` 建立 7 张业务表、约束、索引和 24 项动作种子；新增业务时间模块并在启动时执行迁移、冻结时区；聚焦测试 20 项和受影响 Ruff 通过 | 动作种子测试未逐项检查器械列；训练总组数上限未覆盖 UPDATE；API 时区注入留到表单 API 子任务；旧 `setting_repo.py`、RIR 和旧业务路径待后续删除 |
| 02：基础 Domain | 已完成；重写 `domain/actions`（新 exercises 列、13 项模式词表、写入前「动作存在且负重口径匹配」校验）与 `domain/profile`（7 字段三态 JSON、删 `context_version`、整份覆盖更新、禁用动作引用校验；列表 `known` 不得为空，明确为空只用 `denied`）；新建 `domain/body_metrics`（体重/体脂 CRUD，20–400kg／0–100%，空值不补 0）与只读 `domain/plans`（schema+repo+只读 service，§11 的只读服务已交付；active/draft/历史/日程，`structured_content` 不透明 JSON）；删旧 `domain/plan/` 与 15 个旧测试，新增 4 个测试文件；聚焦 69 项通过、新代码 Ruff 通过；reviewer PASS with notes（无 P0/P1），父会话核对无禁止清单改动 | 画像读取路径不复校验值域（已决定不加）；保留的 10 项红旗词表暂不测试；三个旧测试引用已删的 `test_stage3_plan_reads`，归 06；删旧 plan 后 api/app/runtime/records/stats 断链为预期过渡态，归 03/06 |
