# 04 · 表单 API

**包含原节**：9 表单 API

**依赖**：02、03  
**后续**：05

---

## 9. 表单 API

- [ ] 精简 backend/api/deps.py，移除 PydanticAI、Provider 数据库设置和旧 Runtime 依赖
- [ ] 精简或重写 backend/api/dto.py
- [ ] 新增并注册：
  - [ ] routes_profile.py
  - [ ] routes_records.py
  - [ ] routes_plans.py
- [ ] 补充身体指标和动作目录所需接口
- [ ] 表单写入直接调用业务 service，不创建 Agent Run 或通用草稿
- [ ] 画像「明确为空」映射：前端空选择必须落为 `denied`，不得用 `known([])`（02 已定：列表 `known` 不得为空，空即明确为空）
- [ ] 统一将 Pydantic、领域规则和数据库约束错误映射为明确的 4xx
- [ ] 添加 API CRUD、缺字段、非法类型和不存在资源测试
