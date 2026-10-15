# 动作检索增强 T3：Milvus Standalone 运行环境

本报告记录 T3 的实际部署方法、固定版本、连接约定与真实验证证据。任务边界、数据与业务契约见 [检索契约](action-retrieval-contract.md)、[实现顺序](action-retrieval-implementation-order.md)、[设计方案](action-retrieval-plan.md)。

验收结论：T3 运行环境与探针安全验收通过。真实服务健康、重启数据保留、BM25/COSINE/hybrid_search/WeightedRanker/过滤/analyzer 能力通过，8 个环境验证步骤退出码均为 0。安全验证确认非法配置在连接前拒绝、清理仅作用于两个固定探针名、同前缀隔离集合及记录保持完整；合法流程退出码为 0，拒绝场景按预期退出 1。构建正式动作索引、Embedding API、检索业务、精确匹配优先属于 T5 及后续任务。

## 1. 本任务改动文件

| 文件 | 状态 | 用途 |
| --- | --- | --- |
| `deploy/milvus/docker-compose.yml` | 新增 | Milvus Standalone 独立 Compose 项目（Milvus + etcd + MinIO），固定镜像版本、本机端口、named volume 持久化 |
| `backend/pyproject.toml` | 修改 | 依赖清单新增 `pymilvus>=3.0.2,<3.1` |
| `backend/uv.lock` | 修改 | 由 uv 解析并锁定 PyMilvus 3.0.2 及传递依赖 |
| `backend/test/check_milvus_environment.py` | 新增 | 真实 Milvus 环境与能力验证脚本（版本/健康、analyzer、字段索引、检索加权、过滤、持久化、清理） |
| `backend/test/check_milvus_probe_safety.py` | 新增 | 真实验证同前缀隔离集合和记录在 cleanup 后保留，拒绝覆盖既有隔离集合 |
| `optimization/action-retrieval-milvus-report.md` | 新增 | 本报告，由主会话维护 |

`.gitignore` 未修改：持久化使用受控 named volume，验证证据写入已忽略的 `tmp/milvus-t3/`，不需要新增规则。未触碰 T2 数据、统一文本编码、业务服务、工具注册、HTTP、前端、Trace 及既有业务测试支持。

## 2. 真实版本

| 组件 | 版本 | 来源 |
| --- | --- | --- |
| 操作系统 | Microsoft Windows 11 专业版（build 26200），AMD64 | `Win32_OperatingSystem` |
| 容器环境 | Docker Desktop，WSL 2 后端；WSL 默认版本 2，发行版 Ubuntu(v2)、docker-desktop(v2) | `wsl --status`、`wsl -l -v` |
| Docker CLI/daemon | 29.7.2（Server linux/amd64） | `docker version`、`docker info` |
| Docker Compose | v5.5.1 | `docker compose version` |
| Python | 3.13.13（uv 管理） | `uv run --project backend python --version` |
| uv | 0.11.28 | `uv --version` |
| Milvus | `milvusdb/milvus:v3.0.2`（镜像 ID `5f13bf88e110`，597MB） | `docker compose images` |
| etcd | `quay.io/coreos/etcd:v3.5.25`（`52f17f7e56e4`，24.4MB） | 同上 |
| MinIO（对象存储） | `milvusdb/minio:RELEASE.2024-12-18T13-15-44Z`（`1dce27c494a1`，62.6MB） | 同上 |
| 消息队列 | Woodpecker（v3.0.x 内置，随 Milvus，无独立容器） | 官方 Compose / 启动日志 |
| PyMilvus | 3.0.2 | `pymilvus.__version__` |
| PyMilvus 传递依赖 | grpcio 1.84.0、protobuf 7.36.2、pandas 3.0.6、numpy 2.5.4、orjson 3.13.0、requests 2.34.2、cachetools 7.2.1、python-dotenv 1.2.4、urllib3 2.8.0、charset-normalizer 3.5.2、python-dateutil 2.9.0.post0、six 1.17.0、tzdata 2026.5 | `uv add` 安装输出 |

服务端自报版本：`3.0.2`（`MilvusClient.get_server_version()`）。

## 3. 官方依据与版本兼容证据

已完整阅读的官方资料（经 agent-reach 的网页阅读通道读取）：

- Standalone Compose 部署：<https://milvus.io/docs/install_standalone-docker-compose.md>
- 硬件/软件要求：<https://milvus.io/docs/prerequisite-docker.md>
- BM25 全文检索：<https://milvus.io/docs/full-text-search.md>
- analyzer 概览：<https://milvus.io/docs/analyzer-overview.md>
- 中文 analyzer：<https://milvus.io/docs/chinese-analyzer.md>
- hybrid_search（多向量混合检索）：<https://milvus.io/docs/multi-vector-search.md>
- WeightedRanker：<https://milvus.io/docs/weighted-ranker.md>
- 官方 v3.0.2 release Compose 原件：<https://github.com/milvus-io/milvus/releases/download/v3.0.2/milvus-standalone-docker-compose.yaml>
- PyMilvus 元数据：<https://pypi.org/pypi/pymilvus/json>

版本兼容证据：

- 官方 Standalone 文档默认部署版本为 v3.0.2；release Compose 声明的 `milvusdb/milvus:v3.0.2`、`quay.io/coreos/etcd:v3.5.25`、`milvusdb/minio:RELEASE.2024-12-18T13-15-44Z` 与本地实际拉取一致。
- PyPI 元数据：`pymilvus` 最新 3.0.2，`requires_python >=3.9`，满足后端 `requires-python >=3.13`；与 Milvus v3.0.2 同批次发布。
- `uv add` 在本机 Python 3.13.13 上成功解析并安装 pymilvus 3.0.2（含 grpcio 1.84.0 等 cp313 轮子），无降级 Python。
- 实际连接成功：`get_server_version()` 返回 3.0.2，基础 collection 建删与主键查询通过。
- v3.0.x 默认消息队列为内置 Woodpecker，Compose 无需额外消息队列容器，与官方文档一致。
- 本任务实际引用的 Compose 与 SDK 调用（`Function(BM25)`、`SPARSE_INVERTED_INDEX`+`BM25`、`AUTOINDEX`+`COSINE`、`run_analyzer`、`hybrid_search`、`WeightedRanker`）均按 v3.0.x 文档实现。

## 4. 启动、就绪、停止、重启与安全清理

工作目录统一为仓库根 `D:\repository\fit-agent`；Compose 项目名 `fit-agent-milvus`（文件顶层 `name:` 固定，与调用目录无关）。

启动：

```powershell
docker compose -f deploy/milvus/docker-compose.yml up -d
```

就绪检查（需等待 `standalone` 显示 `healthy`，首次启动约数十秒至数分钟）：

```powershell
docker compose -f deploy/milvus/docker-compose.yml ps
docker compose -f deploy/milvus/docker-compose.yml exec -T standalone curl -fsS http://localhost:9091/healthz
```

停止服务（保留容器与持久化数据）：

```powershell
docker compose -f deploy/milvus/docker-compose.yml stop
```

重启服务：

```powershell
docker compose -f deploy/milvus/docker-compose.yml start
```

重建容器（保留持久化数据）：`down` 删除容器与网络，named volume 保留，随后 `up -d` 复用原数据：

```powershell
docker compose -f deploy/milvus/docker-compose.yml down
docker compose -f deploy/milvus/docker-compose.yml up -d
```

删除持久化数据（破坏性，常规流程禁止执行；关闭 Compose 时不要加 `-v`）：

```powershell
docker compose -f deploy/milvus/docker-compose.yml down -v
docker volume rm fit-agent-milvus-milvus-data fit-agent-milvus-etcd-data fit-agent-milvus-minio-data
```

安全清理测试对象（仅删除 `fit_agent_t3_probe` 与 `fit_agent_t3_probe_basic` 两个精确名称，保留其他 collection 与持久化目录）：

```powershell
# cd backend
uv run python -m test.check_milvus_environment cleanup
```

端口或项目名冲突时 `up` 直接失败，不自动改端口、不终止其他容器、不删除既有容器。本任务未使用 `down -v`，未删除任何 volume。

## 5. 连接、鉴权、超时、持久化与端口

- 连接地址：主机后端连接 `http://127.0.0.1:19530`。容器内后端加入 Milvus 所在 Docker 网络后，可通过服务名 `standalone` 连接 `http://standalone:19530`。当前 T3 未验证 Fit-Agent 整体容器化与跨 Compose 网络接入。
- 鉴权实际状态：Milvus `authorizationEnabled=false`（容器内 `/milvus/configs/milvus.yaml` 实测），服务未启用认证；客户端连接不带 token（`token=无`）。未宣称已启用鉴权。部署仅本机可达，构成访问边界。
- 请求超时：脚本约定 `FIT_AGENT_MILVUS_TIMEOUT=10`（秒），实际写入客户端 `MilvusClient(timeout=10)`。所有脚本操作均带该超时。
- 持久化：受控 named volume
  - `fit-agent-milvus-milvus-data` → `/var/lib/milvus`
  - `fit-agent-milvus-etcd-data` → `/etcd`
  - `fit-agent-milvus-minio-data` → `/minio_data`
  常规 `down` 不删除数据；只有 `down -v` 或显式 `docker volume rm` 才删除。
- 暴露端口：仅 `127.0.0.1:19530`（后端连接用）。etcd 2379/2380、MinIO 9000/9001、管理/健康接口 9091 均只在 Compose 内部网络可达，未映射到主机。
- 凭据：MinIO 使用镜像内置默认凭据且仅内部可达；未提交任何真实密钥，未回显到日志。Milvus 未启用鉴权，无凭据。
- 其他：`restart: unless-stopped` 使容器随 Docker 引擎恢复；`security_opt: seccomp:unconfined` 与官方一致（Milvus 运行需要）。

## 6. 真实验证结果

能力脚本：`backend/test/check_milvus_environment.py`（子命令 `run`/`persistence`/`cleanup`）。测试 collection `fit_agent_t3_probe`（T3 归属），基础功能用 `fit_agent_t3_probe_basic`（脚本内自建自删）。dense 使用 1024 维正交单位向量，用于验证 COSINE 与融合语义，非百炼输出，也不作为动作语义质量证据。

A. 版本与健康：服务端版本 3.0.2；客户端连接成功；基础 collection 建删、插入与主键查询通过；容器三者均 `healthy`；`/healthz` 返回 `OK`。

B. analyzer（`{"type": "chinese"}`，真实 `run_analyzer` 输出）：

| 输入 | 真实 token |
| --- | --- |
| `杠铃卧推` | `['杠铃', '卧', '推']` |
| `坐姿哑铃弯举，锻炼肱二头肌` | `['坐姿', '哑铃', '弯举', '锻炼', '肱', '二头', '二头肌']` |
| `barbell bench press` | `['barbell', 'bench', 'press']` |
| `Dumbbell 哑铃卧推 3/4` | `['Dumbbell', '哑铃', '卧', '推', '3', '4']` |

要点：中文 analyzer 保留英文大小写（`Dumbbell` 未转小写），`cnalphanumonly` 过滤后 `3/4` 被切分为 `3`、`4`。此配置可用，正式 analyzer 质量选择归后续开发集评测。

C. 字段与索引：主键 `id` 为 VARCHAR（保留前导零）；文本字段 `enable_analyzer=True` 且配置中文 analyzer；`Function(BM25)` 由 `text` 生成 `sparse`（SPARSE_FLOAT_VECTOR）；`dense` 为 1024 维 FLOAT_VECTOR；标量字段 `equipment`、`body_part`。索引：`sparse` 为 `SPARSE_INVERTED_INDEX` + `BM25`（`inverted_index_algo=DAAT_MAXSCORE`、`bm25_k1=1.2`、`bm25_b=0.75`）；`dense` 为 `AUTOINDEX` + `COSINE`。插入后 `flush` 并 `load_collection`，以 Strong 一致性读取。

D. 检索与加权（请求顺序固定 `[BM25, dense]`，`WeightedRanker(α, 1−α)`，默认 `norm_score=True`）：

| 用例 | 真实结果（id, score） |
| --- | --- |
| BM25 `哑铃` | `0004: 0.726154`，`0002: 0.663010`（命中正好两个含“哑铃”的记录） |
| COSINE dense（e0 单位向量） | `0001: 1.0`，`0002/0003/0004: 0.0` |
| hybrid 权重 `[1.0, 0.0]` | `0004: 0.399838`，`0002: 0.372720`，`0001: 0.0`，`0003: 0.0` |
| hybrid 权重 `[0.0, 1.0]` | `0001: 1.0`，`0002/0003/0004: 0.5` |
| hybrid 权重 `[0.5, 0.5]` | `0001: 0.5`，`0004: 0.449919`，`0002: 0.436360`，`0003: 0.25` |

固定断言：BM25 命中集合恰为 `{0002, 0004}` 且分数非零；dense 首条为 `0001` 且分数为 `1.0`、其余为 `0.0`；权重 `[1.0,0.0]` 时首条属于 `{0002,0004}` 且 `0002` 排在 `0001` 之前；权重 `[0.0,1.0]` 时首条为 `0001`；权重 `[0.5,0.5]` 时 `0001` 与 `{0002,0004}` 均出现在结果中。字符串主键 `0001`–`0004` 前导零完整返回。

内置归一化的实测映射：BM25 路径 `0.726154 → 0.399838` 符合 `arctan(x)/(π/2)`；COSINE 路径 `1.0 → 1.0`、`0.0 → 0.5` 符合 `(1+cos)/2`。该映射为 T8 选择 α、召回深度与无答案门槛的直接依据：正交探针的原始 COSINE 为 0，归一化得分为 0.5；该探针分数不能推定所有真实非相关动作的得分。真实语义向量的无答案门槛由 T8 结合查询证据校准。

E. 过滤：BM25 与 dense 请求携带同一表达式，返回项全部满足条件。

| 表达式 | 真实结果 |
| --- | --- |
| `equipment == "dumbbell" and id not in ["0002"]` | `[('0004', 0.449919)]` |
| `body_part == "back" and equipment == "dumbbell"` | `[('0004', 0.699919)]` |
| `equipment == "kettlebell"` | `[]` |

F. 持久化：写入 4 条记录后，对该 Compose 项目执行 `down` 再 `up -d`，等待 `healthy`。重启后 `fit_agent_t3_probe` 仍在；主键 `0001`–`0004`（含前导零）、文本、`equipment`、`body_part` 与写入值逐字一致；BM25 `哑铃` 仍命中 `{0002, 0004}`，COSINE dense 首条仍为 `0001`。持久化路径经 `docker volume ls` 与重启后读取共同确认。

G. 清理：`cleanup` 仅删除 `fit_agent_t3_probe`，未删除其他 collection、业务数据库、Trace 数据或持久化目录。当前无遗留测试 collection。

## 7. 测试执行归属与证据

验证实际运行由通用 subagent 执行；主会话仅依据其回报的命令、退出码与输出判定。subagent 命令（工作目录）与结果：

| 步骤 | 命令 | 退出码 | 证据 |
| --- | --- | --- | --- |
| 启动 | `docker compose -f deploy/milvus/docker-compose.yml up -d`（仓库根） | 0 | `tmp/milvus-t3/01-up.txt` |
| 健康 | `docker compose ... ps` + `exec -T standalone curl -fsS http://localhost:9091/healthz` | 0 | `tmp/milvus-t3/02-health.txt` |
| 版本/镜像/卷/鉴权 | `docker compose ... images`、`docker volume ls`、`docker exec ... grep authorizationEnabled` | 0 | `tmp/milvus-t3/03-versions-volumes-auth.txt` |
| 能力 | `uv run python -m test.check_milvus_environment run`（`backend`） | 0 | `tmp/milvus-t3/04-check-run.txt` |
| 重启 | `docker compose ... down` 后 `up -d` 并等待健康 | 0 | `tmp/milvus-t3/05-restart.txt` |
| 持久化 | `uv run python -m test.check_milvus_environment persistence`（`backend`） | 0 | `tmp/milvus-t3/06-check-persistence.txt` |
| 清理 | `uv run python -m test.check_milvus_environment cleanup`（`backend`） | 0 | `tmp/milvus-t3/07-check-cleanup.txt` |
| 收尾 | `docker compose ... ps`、`docker volume ls` | 0 | `tmp/milvus-t3/08-final-ps.txt` |

证据目录 `tmp/milvus-t3/` 位于 `.gitignore` 的 `tmp/` 忽略范围内，不进入版本库。subagent 记录了两处取证编码偏差（`01-up.txt` 由 PowerShell 混合编码重建为 UTF-8；`persistence` 以 `PYTHONIOENCODING=utf-8` 只读重跑一次），两次退出码均为 0，属证据捕获层面，非环境或能力问题。

### 7.1 探针安全验证

`PROBE_COLLECTION` 与 `BASIC_COLLECTION` 固定为 `fit_agent_t3_probe`、`fit_agent_t3_probe_basic`。模块导入期校验 `FIT_AGENT_MILVUS_TEST_COLLECTION`：允许未设置或等于固定探针名，其余值明确退出 1，先于连接与数据库操作。`cleanup` 仅删除两个精确名称。

以下验证由 subagent 实际运行，主会话已读取源码与保存证据，本轮未重新执行集成测试。工作目录为 `backend`；证据均位于 `tmp/milvus-t3/`。

| 场景 | 命令或入口 | 预期及实际退出码 | 证据 |
| --- | --- | --- | --- |
| 非法业务集合名 | `uv run python -m test.check_milvus_environment` 的 run/persistence/cleanup/all/默认入口 | 五个入口均 1，名称校验拒绝 | `11-reject-business.txt` |
| 空字符串与同前缀配置 | run / cleanup | 均 1，名称校验拒绝 | `12-reject-empty.txt`、`13-reject-same-prefix.txt` |
| 不可达 URI 加非法名称 | run | 1，仅名称校验消息，连接前失败 | `14-reject-before-connect.txt` |
| 固定名称与未设置配置 | run | 均 0，能力回归通过 | `15-run-env-fixed.txt`、`16-run-env-unset.txt` |
| 保留卷重启 | Compose down / up -d | 均 0，健康恢复 | `17-compose-down.txt`、`18-compose-up.txt` |
| 重启持久化 | persistence | 0，主键、文本与查询保持 | `19-check-persistence.txt` |
| 既有隔离集合保护 | `uv run python -m test.check_milvus_probe_safety` | 1，拒绝覆盖；后续查询标记记录退出 0 | `20-safety-exists-fail.txt` 的完整成功验证段 |
| 同前缀集合与记录保留 | 同上，隔离集合不存在 | 0，真实 cleanup 后两条记录保留，随后仅删除本次创建的 guard | `23-safety-full.txt` |
| Ruff | 两个测试文件 | 0，All checks passed | `10-ruff.txt`、`22-ruff-recheck.txt` |
| 收尾清理与状态 | cleanup、Docker 状态、集合及卷检查 | 均 0；三容器健康、集合数 0、三个卷保留 | `24-final-cleanup.txt`、`25-final-state.txt` |

安全脚本的隔离集合名为 `fit_agent_t3_probe_safety_guard`，含两条真实记录。schema 使用 4 维向量及 FLAT/COSINE 索引满足真实服务要求；安全断言核对集合及记录保留。测试证据保留完整执行记录，验收依据上述达到预期的运行段。

## 8. T5/T6 可直接使用的约定

- 版本：Milvus `v3.0.2` 与 PyMilvus `3.0.2` 锁定；etcd `v3.5.25`、MinIO `RELEASE.2024-12-18T13-15-44Z` 固定标签。
- 连接约定（环境变量名，本任务验证脚本与后续接入共用）：
  - `FIT_AGENT_MILVUS_URI`，主机默认 `http://127.0.0.1:19530`；容器后端加入 Milvus 所在 Docker 网络后使用 `http://standalone:19530`。
  - `FIT_AGENT_MILVUS_TOKEN`，当前无需鉴权，留空。
  - `FIT_AGENT_MILVUS_TIMEOUT`，默认 `10`（秒）。
- 能力边界（已真实验证）：中文/中英混合/英文 analyzer；`Function(BM25)` 生成 sparse；`SPARSE_INVERTED_INDEX`+`BM25` 稀疏索引；1024 维 `AUTOINDEX`+`COSINE` dense 索引；字符串主键保留前导零；`hybrid_search` 两路 `[BM25, dense]` + `WeightedRanker(α, 1−α)` 内置归一化；相同结构化过滤表达式在两路生效，含空结果；`down`/`up` 后数据复用。
- 未验证边界：正式动作 schema 的字段容量与索引参数、目录向量规模、Embedding API 接入、精确匹配优先、线上检索替换，均由 T5 及后续任务固定。
- 验证脚本：`backend/test/check_milvus_environment.py`，`run` 建集合与检索、`persistence` 重启后校验、`cleanup` 清理探针集合。T5 可直接复用连接与断言基线。

## 9. 未执行项、阻塞、遗留与风险

- 未执行：正式动作索引构建、Embedding API 调用、检索业务与工具集成、精确匹配优先、Fit-Agent 整体容器化、Trace 扩展、前端改动。不属 T3 范围。
- 验收阻塞：无。固定探针归属、导入期配置拒绝、精确清理、同前缀记录保留及既有隔离集合保护均有真实验证证据。
- 遗留测试对象：收尾证据显示集合数为 0，主探针、basic 及 safety_guard 均无残留。三个 named volume 为部署持久化对象，属预期保留。
- 资源风险：Docker Desktop 引擎内存约 9.7 GiB（Milvus Standalone 最低 8 GiB，推荐 16 GiB），当前满足但余量偏紧；Docker 引擎由本任务启动，当前保持运行；Milvus 三个容器保持 `healthy` 运行，策略 `unless-stopped`。删除数据需显式 `down -v` 或 `docker volume rm`。
- 安全边界：Milvus 未启用鉴权，仅 `127.0.0.1:19530` 本机可达；MinIO 使用镜像默认凭据且仅内部可达。

## 10. 需协调同步到公共契约的配置与参数

以下为 T5/T6/T8 与协调任务写入 `optimization/action-retrieval-contract.md` 的物理细节：

- 服务镜像与版本：Milvus `v3.0.2`、PyMilvus `3.0.2`、etcd `v3.5.25`、MinIO `RELEASE.2024-12-18T13-15-44Z`；消息队列为内置 Woodpecker，无独立容器。
- 连接与配置项：`FIT_AGENT_MILVUS_URI`、`FIT_AGENT_MILVUS_TOKEN`、`FIT_AGENT_MILVUS_TIMEOUT`；默认无鉴权；持久化使用 named volume；仅暴露 `127.0.0.1:19530`。
- 物理字段与索引（验证用 schema，正式容量归 T5）：VARCHAR 主键保留前导零；文本字段 `enable_analyzer=True` + `analyzer_params={"type": "chinese"}`；`Function(BM25)` 由文本生成 sparse；`SPARSE_INVERTED_INDEX`+`BM25`（`inverted_index_algo=DAAT_MAXSCORE`、`bm25_k1=1.2`、`bm25_b=0.75`）；`dense` 1024 维 `AUTOINDEX`+`COSINE`；`equipment`/`body_part` 标量字段。
- 融合请求：顺序 `[BM25, dense]`，`WeightedRanker(α, 1−α)` 且 `norm_score=True`（默认）。
- 内置归一化实测映射（供 T8 调参）：BM25 路径 `arctan(x)/(π/2)`，COSINE 路径 `(1+cos)/2`；COSINE 为 0 的正交探针归一化得分为 0.5；真实非相关动作的得分由实际向量决定，无答案门槛使用真实查询评测。
- analyzer 实测特征（供 T2 编码规则核对）：中文 analyzer 不转小写，保留英文原样；`cnalphanumonly` 会切分含符号数字串（`3/4 → 3`、`4`）。

精确匹配优先仍为可选设计，本次不实现。
