# 前端执行契约（frontend/）

> 配合根目录 `AGENTS.md` 使用；前端决策见本目录 `PLAN-FRONTEND.md`。任何 frontend/ 内的任务开工前先读这两个文件。

- 已拍决策见 `PLAN-FRONTEND.md`，不得更改；要改先问人。
- 遇到未覆盖的决策：列 2–3 个选项和推荐，停下来等拍板，不许自行决定。
- 契约先行（D1A）：数据形状只从契约类型文件引用；UI 与 mock 都以契约为准；后端对齐时改动收敛在契约文件与少数渲染分支。
- mock 阶段不做真实网络调用、不处理真实 API Key；未定项只做"类型 + mock"，不做硬编码业务逻辑。
- 边界：只在 frontend/ 内工作；不读取、不修改 backend/ 与 spike/；前端依赖只装 frontend/ 自己的 node_modules，与 spike/.venv 互不相干。
- 不读取、不讨论 memory 记忆设计文档；业务约束以 pre-prj/PRD.md 与 pre-prj/architecture-decisions.md 为准。
- API Key 不出现在前端源码、mock、日志；前端只接触 `has_api_key` 徽章级别的信息。
- 不提交 git。
- 每次任务完成：只报改了哪些文件、跑了什么、结果如何，不贴代码全文。
- 说"做完了"必须附上跑过的验证证据（如 `npm run build` 输出、dev 演示路径）。
- 实现层细节（组件拆分、文件组织、依赖小版本、字体子集化方式）自行决定，不上升。
