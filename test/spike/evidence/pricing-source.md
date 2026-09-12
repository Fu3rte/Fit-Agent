# DeepSeek 官方价格证据（spike 计费依据）

> 2026-09-12 复核注记：官方文档（2026-09-11 抓取）已将 `deepseek-v4-flash` 等列为停用，请求路由到 DeepSeek-V4.1-Flash（现名 `deepseek-flash`）；当前窗口与输出上限改为 1M / 384K。本文件保留 2026-09-06 当日快照原文，不更新数值；真实调用前必须重新核对官方页面（PLAN.md 费用护栏）。

- 来源页面：https://api-docs.deepseek.com/quick_start/pricing （Models & Pricing，官方全文）
- 抓取通道：agent-reach 规定的只读网页阅读通道（Jina Reader：r.jina.ai），未登录、无写操作
- 抓取时间：2026-09-06（本机时区 UTC+8 判断为当日；页面 `Published Time: Fri, 28 Aug 2026 05:44:14 GMT` 为缓存页时间戳）
- 单位：USD per 1M tokens（官方页面原文明确 "The prices listed below are in units of per 1M tokens"）
- 不做平台币价换算，不做任何推测折算；本表只收录官方页面数值

## 模型与版本（官方页面 MODEL VERSION 行）

| 请求 model | 官方 MODEL VERSION |
|---|---|
| deepseek-v4-flash | DeepSeek-V4-Flash-0731 |
| deepseek-v4-pro | DeepSeek-V4-Pro-0813 |
| deepseek-v4-flash-vision-exp | DeepSeek-V4-Flash-Vision-Exp |

响应返回 MODEL VERSION 身份时属于"服务端合法版本别名"，与请求 model 的映射未经验证：
按用户拍板**显式标记未验证并停止**，不静默放行（见 spike_lib/fee_guard.py OFFICIAL_VERSION_ALIASES）。

## 价格表（官方页面逐列，USD per 1M tokens）

| 类目 | 时段 | deepseek-v4-flash | deepseek-v4-pro | deepseek-v4-flash-vision-exp |
|---|---|---|---|---|
| 输入（cache hit） | off-peak | $0.007 | $0.022 | $0.007 |
| 输入（cache hit） | peak | $0.014 | $0.044 | $0.014 |
| 输入（cache miss） | off-peak | $0.22 | $0.66 | $0.22 |
| 输入（cache miss） | peak | $0.44 | $1.32 | $0.44 |
| 输出 | off-peak | $0.66 | $1.98 | $0.66 |
| 输出 | peak | $1.32 | $3.96 | $1.32 |

## 峰谷定义（官方页面脚注 (1) 原文要点）

- "Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC,
  Monday through Friday (all other hours are off-peak)."
- 即：peak = 01:00–04:00 与 06:00–10:00 UTC（周一至周五）；其余（含周末全程）为 off-peak。

## 计费类目边界

- DeepSeek OpenAI 端点（https://api.deepseek.com）无独立 cache_write 计费类目：
  计费 = input(hit) + input(miss) + output，与 spike_lib/fee_guard.py PRICE_TABLE 一致。
- 官方页面原文另有提示："Product prices may vary and DeepSeek reserves the right to adjust them" ——
  真实调用前必须重新核对本页；价格与表中不一致即停止（PLAN.md 费用护栏）。
- 官方页面未提供"保证缓存命中的最低前缀长度"或缓存命中率承诺；命中率推算前提按实测观察，
  不得以本页数值反推。

## 可复核性

- 本文件为脱敏摘要：不含 API Key、不含请求头、不含账本数据。
- 结算与预留口径：
  - 预留 = max(peak, off_peak) × 输入上界（全 miss）+ max(peak, off_peak) × max_output（见 fee_guard.max_rate）
  - 结算 = 按 settle 时刻峰谷时段取价（fee_guard.rate），分项 = hit + miss + output（互斥，不重复计费）
