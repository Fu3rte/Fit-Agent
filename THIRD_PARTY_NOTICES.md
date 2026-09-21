# 第三方声明（Third-Party Notices）

Fit-Agent 的动作目录由两部分构成：从既有 27 项种子继承的动作行，以及本计划新增的 83 条动作行。
新增动作行的英文原名（`canonical_name_en`）与来源编号（`source_ref` 里的 `source_id`）来自第三方数据集，
其余目录字段由 Fit-Agent 自行编写。

## 1. exercises-dataset

- 仓库名称：`exercises-dataset`
- 仓库 URL：https://github.com/hasaneyldrm/exercises-dataset
- 固定 commit：`7455efae41b330c265e7cd4b78dfa848e7ce5ebd`
- 版权人：`Copyright (c) 2026 Hasan Emir Yıldırım`
- 许可：MIT（数据集代码与数据行）
- 上游许可与声明文件位置：
  - `LICENSE`（含 `MEDIA EXCEPTION` 段）
  - `NOTICE.md`（媒体署名与使用条款）

### 使用的字段范围（只读引用）

只使用数据行中的两个字段：

- `id` → Fit-Agent `exercises.source_ref` 的 `source_id` 部分（`exercises-dataset@<commit>:<id>`）
- `name` → Fit-Agent `exercises` 的英文原名 `canonical_name_en`（仅修复上游英文名的已知错误拼写：`45в°`→`45°`、`peacher`→`preacher`、`revers`→`reverse`）

## 2. Fit-Agent 自行编写的内容

以下内容**不是**数据集内容，由 Fit-Agent 自行编写，不主张来自上游：

- 中文标准名 `standard_name_zh`（格式：器械 + 姿势/角度/变式 + 核心动作）
- 别名 `aliases`（中文变式名与英文原名）
- 动作模式 `modes`（13 项词表）
- 记录口径 `record_type`、负重口径 `load_convention` 与最小加重单位 `min_load_increment_kg`
- 器械归类 `equipment_variant` 与可推荐标记 `recommendable`
- 通用别名归属（“深蹲”→“杠铃背蹲”、“卧推”→“杠铃平板卧推”、“硬拉”→“杠铃传统硬拉”、
  “划船”→“坐姿绳索划船”、“下拉”→“高位下拉”）

## 3. 未导入的内容

- **图片与 GIF 未导入**：仓库内不含数据集任何缩略图或动画 GIF，也未引用媒体 URL。
- **instructions 未导入**：`instructions`、`instruction_steps`、`target`、`muscle_group`、
  `secondary_muscles`、`media_id` 一律未导入。
- **Gym Visual 媒体未再分发**：数据集 `images/`、`videos/` 的媒体归
  `© Gym visual — https://gymvisual.com/`，Fit-Agent 未复制、未再分发、未引用该媒体，
  故不在本仓库内保留媒体署名要求；上游媒体署名与使用条款见数据集 `NOTICE.md`。
- 既有 27 项种子行沿用的 `attribution` 值（`© Gym visual — https://gymvisual.com/`）与
  `source_ref` 原样保留、未改写；本计划新增的 83 行使用纯数据来源声明
  `exercises-dataset (MIT, © 2026 Hasan Emir Yıldırım)`。

## 4. 许可文本获取方式

上游 MIT 许可全文与媒体声明未复制进本仓库，请按上述固定 commit 从仓库 URL 获取 `LICENSE` 与 `NOTICE.md`：

```bash
git -C <exercises-dataset> show 7455efae41b330c265e7cd4b78dfa848e7ce5ebd:LICENSE
git -C <exercises-dataset> show 7455efae41b330c265e7cd4b78dfa848e7ce5ebd:NOTICE.md
```
