# 历史归档：PydanticAI spike（2026-09-06，已完成并通过验收）

> 本文件为第 12 章「历史归档」正本之一，按章节骨架约定迁入 `architecture-archive/spikes/`；内容逐字搬运自根 `PLAN.md`「历史归档：PydanticAI spike」（L315–354），正文文字保持原样，段落软换行与标题层级按归档体例重排，引号按全文中文排版统一为全角弯引号；仅证据路径按当前工作区复核修正（PLAN 撰写时为 `spike/…`，提交 `788c560`，现位于 `test/spike/…`）。本归档不构成任何实现授权；验收结果以结论 + 证据指针记录。

> 2026-09-06 更新：spike 已完成并通过验收（三项缺口 + 真实调用全过，证据见 `test/spike/evidence/real/`、`test/spike/ledger.json`，累计费用 $0.0016553）；B 路线定案为 PydanticAI（见决策表同日行）。**完整开发仍未批准。**

## spike 专属决策

| 决策 | 选项 | 选了 | 为什么 |
|---|---|---|---|
| 首个验证 Provider | 多端点 / 仅 DeepSeek OpenAI 兼容 | 仅 `https://api.deepseek.com`（OpenAI 兼容） | Anthropic 与本地模型暂缓（用户拍板） |
| 验证模型分工 | 全模型全量 | flash 全量回归；pro / vision-exp 文本冒烟并核对返回 model 身份防回落 | 控制范围与费用 |
| spike 费用护栏 | 无 / 严格账本 | max_tokens≤256；重试禁用；串行；按官方价格表逐次记账；$5 自动停、$10 绝对硬顶；请求前预留保守最坏成本；usage 缺失/取消保留预留不算 0；前 10 次累计 0 或量级异常停 | 费用是用户硬约束 |
| spike 跨重启预算累计 | A spike 专用持久账本 / B 单进程结束后人工核账 | A（2026-09-06） | 请求前落盘预留；重启保留未结算金额；账本损坏或并发启动即拒绝，支持安全分批验收 |

## spike 验收口径（用户已认可 + 三处补充）

1. **前缀字节对齐**：捕获实际序列化请求，检查工具 schema 序列化 + 常驻层 + 稳定早期历史，在追加消息、工具往返、桩压缩后前缀内容与顺序（字节级）稳定。不用真实摘要、不读 memory。JSON 完整请求字节等同 ≠ 服务端 token 前缀等同，只作工程近似。
2. **cache 指标穿透**：保留原始 usage；归一 input / cache_read / cache_write / output；缺字段 ≠ 0，miss ≠ write，同一 token 不重复计费。DeepSeek OpenAI 端点无独立 cache_write 计费类目（官方价格表只有 hit/miss input + output），如实记录为“无此计费类目”。
3. **整体 Run 取消**：取消后禁止后续模型/工具调用；取消不产生成功提交；不可中断工具收尾前不释放执行槽。需离线可控工具检查 + 至少一次真实流式取消；客户端断连不证明服务端停止计费。

## spike 阶段验收标准

- 离线：三项缺口各有最小可重复检查，全部通过；费用护栏先于任何真实调用通过全部异常用例。
- 真实调用（凭据就绪后）：flash 全量回归 + pro/vision-exp 冒烟，逐次记账在预算内，产出脱敏证据；未验证项明确标注，不得以部分通过冒充全量通过。

## 验收结果（2026-09-06）

- 离线：73 项测试全绿（含持久账本 22 项异常用例：落盘时机/重启保留/损坏拒绝/并发拒绝）；秘密扫描通过。
- 真实：flash 全量回归（含真实流式取消）+ pro / vision-exp 冒烟共 10 条 wire 账目，累计 $0.0016553（远低于护栏线）；证据脱敏并经独立复核重算（REVIEW_VERDICT: pass）。
- 未验证项（如实标注）：服务端计费是否随客户端取消停止；官方版本别名路径未触发；完整态流式响应 usage 浮出未观测；取消收尾 httpcore2 stderr 噪音（护栏记账不受影响）。
- 提交：`788c560`（仅 spike/ 目录）。

## 证据指针

- 真实调用脱敏证据：`test/spike/evidence/real/`（alias-probe.json、flash-regression.json、pro-smoke.json、stream-complete.json、vision-smoke.json）
- 跨重启持久账本：`test/spike/ledger.json`
- 历史提交：`788c560`（仅 spike/ 目录，现工作区已位于 `test/spike/`）
- 来源：根 `PLAN.md`「历史归档：PydanticAI spike」L315–354（搬运正本）；B 路线定案见根 `PLAN.md`「技术基座」与 v1 L15
