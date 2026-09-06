# KnowledgeAgent Server

FastAPI Server 是 KnowledgeAgent 的核心内部服务，已实现文档入库、MongoDB 会话记录、Zilliz 混合检索和百炼 RAG 问答。完整 HTTP 合同见 [`README/api.md`](../../README/api.md)。

服务只提供 `/internal/v1` 接口，浏览器应通过 `app/web` 的 `/api/v1` 代理访问，不应直接把 FastAPI 暴露到公网。

## 目录结构

```text
app/server/
├── main.py                  # FastAPI 应用入口和生命周期
├── container.py             # Provider、Repository、Service 依赖装配
├── api/                     # HTTP 路由、鉴权、错误响应
├── core/                    # 环境配置、ID 和时间工具
├── domain/                  # Pydantic 领域模型和接口模型
├── repositories/            # MongoDB/内存持久化实现
├── services/                # 文档入库、RAG、健康检查工作流
├── providers/               # 百炼、MinerU、Zilliz、文件存储适配器
└── utils/                   # SSE 编码和 Markdown 分块
```

依赖方向为 `api → services → repositories/providers`；`domain` 和 `core` 提供共享的数据结构与配置。启动入口为 `app.server.main:app`。

入库与 RAG 使用 LangGraph `StateGraph`，在 service 初始化时编译并复用。`services/ingestion.py` 实现入库主图与双路嵌入子图，`services/rag.py` 实现检索准备图和流式/非流式共用的回答图；`workflows.py` 仅保留导入兼容。图结构及修改原则见 [工作流说明](services/README.md)。

## 已实现接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/internal/v1/health/ready` | 检查 MongoDB 和 Zilliz 就绪状态 |
| `POST` | `/internal/v1/documents/ingestions` | 接收文件并启动异步入库 |
| `GET` | `/internal/v1/documents` | 查询 owner 的文档列表 |
| `GET` | `/internal/v1/documents/{id}` | 查询文档和处理进度 |
| `DELETE` | `/internal/v1/documents/{id}` | 删除文档元数据、本地文件、解析产物和向量 |
| `POST` | `/internal/v1/conversations` | 创建会话 |
| `GET` | `/internal/v1/conversations` | 查询会话列表 |
| `GET` | `/internal/v1/conversations/{id}/messages` | 查询会话消息 |
| `DELETE` | `/internal/v1/conversations/{id}` | 删除会话及消息 |
| `POST` | `/internal/v1/rag/completions` | 非流式或 SSE 流式 RAG 问答 |

## 启动

在项目根目录安装依赖并启动：

```bash
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv sync --group dev
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv run uvicorn app.server.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后可访问：

- OpenAPI UI：<http://127.0.0.1:8000/docs>
- 就绪检查：<http://127.0.0.1:8000/internal/v1/health/ready>

如果配置了 `INTERNAL_API_TOKEN`，访问就绪检查和其他内部接口时都必须携带 `Authorization: Bearer <INTERNAL_API_TOKEN>`。

## 必要配置

```dotenv
MONGODB_URI=mongodb://user:password@localhost:27017/?authSource=admin
MONGODB_DATABASE=knowledge_agent

BAILIAN_BASE_URL=https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
BAILIAN_API_KEY=YOUR_API_KEY
EMBEDDING_MODEL=qwen3-vl-embedding
SPARSE_EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=1024
RERANK_MODEL=qwen3.7-text-rerank
CHAT_MODEL=deepseek-v4-flash

MINERU_API_KEY=YOUR_API_KEY

ZILLIZ_URI=https://YOUR_CLUSTER_ENDPOINT
ZILLIZ_TOKEN=YOUR_TOKEN
ZILLIZ_COLLECTION=knowledge_agent_chunks_vl_v1

MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=YOUR_MINIO_ACCESS_KEY
MINIO_SECRET_KEY=YOUR_MINIO_SECRET_KEY
BUCKET_NAME=knowledge-minio
```

可选配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INTERNAL_API_TOKEN` | 空 | 配置后，所有 `/internal/v1` 请求必须携带 Bearer Token |
| `MAX_UPLOAD_SIZE` | `209715200` | 上传大小上限，单位为字节 |
| `MINIO_SECURE` | 按 endpoint 推断 | 裸 `host:port` 是否使用 TLS |
| `CHUNK_SIZE` | `1200` | 文本块字符数 |
| `CHUNK_OVERLAP` | `200` | 相邻文本块重叠字符数 |
| `RETRIEVAL_LIMIT` | `30` | 混合检索返回数量；dense 与 sparse 各提供至少 60 个候选给 RRF |
| `RERANK_LIMIT` | `6` | 重排后提供给模型的片段数 |
| `MINERU_API_BASE` | `https://mineru.net/api/v4` | MinerU API 地址 |
| `MINERU_MODEL` | `vlm` | MinerU 解析模型 |
| `MINERU_POLL_INTERVAL` | `5` | MinerU 轮询间隔，单位为秒 |
| `MINERU_MAX_WAIT` | `900` | MinerU 最长等待时间，单位为秒 |
| `PROBE_PAID_DEPENDENCIES` | `false` | 预留配置；就绪检查目前不调用百炼和 MinerU |

没有设置 `MONGODB_URI` 时，服务使用进程内仓储便于接口开发，但就绪检查会将 MongoDB 标记为不可用并返回 HTTP 503。没有设置 `ZILLIZ_URI` 时，文档无法完成向量入库。

使用 MongoDB Atlas 的 `mongodb+srv://` 地址时，运行主机必须能解析 Atlas SRV/TXT DNS 记录。本机 DNS 不可用会令服务在创建 MongoDB 索引时启动失败；应优先修复主机 DNS，或在部署环境中使用 Atlas 提供的、包含完整 seed list 和 replica set 参数的标准 `mongodb://` 连接串。不要把包含凭据的连接串写入日志或 README。

## 文档入库流程

上传接口在 MinIO 原文件保存成功、MongoDB 文档记录创建完成且进程内任务已登记后返回 HTTP 202。后续处理顺序为：

1. 从 MinIO 对象 `documents/{owner_id}/{document_id}/source.<ext>` 下载到任务专用临时目录。
2. PDF、DOC、DOCX 调用 MinerU；Markdown 直接读取；任务结束后清理临时目录。
3. 优先从 MinerU content-list 恢复页码、标题、列表、表格、图表说明及 `img_path`。连续正文按文本切分，含图块携带自己的图片路径与描述；无 content-list 时从 MinerU Markdown 的内联图片引用恢复关联（页码为空）。
4. 每批最多 20 个 chunk，由 LangGraph 子图并行处理 dense 和 sparse 两路，并在两路完成后写入。dense 每批最多并发 4 个请求；纯文本合批，含图块独立开启 `enable_fusion=true`。sparse 批量输入正文或图片描述文本。
5. 将 chunk 写入 Zilliz，持续更新 `stage` 和 `progress`，成功后进入 `indexed/completed`。

HTTP 202 仅表示任务已接受，客户端必须轮询文档详情，直到状态进入 `indexed` 或 `failed`。

## 向量策略

每个文本块会并行生成两种向量：

- `dense_vector`：由 `qwen3-vl-embedding` 生成；含图块使用真实图片和对应文本融合，纯文本块只输入文本。
- `sparse_vector`：由 `qwen3.7-text-embedding` 生成，只输入文本，图片使用解析描述。

MinerU 图片路径仅限解析目录内的真实文件，越界或缺失时入库失败。直接上传 Markdown 支持 HTTP(S) URL、Base64 与文件夹包内相对图片；普通单文件缺图时保留描述并记录 `total_images/missing_images`，越界或绝对路径仍拒绝。

RAG 分别召回 dense、sparse 候选，使用加权 RRF 合并后通过 `qwen3.7-text-rerank` 重排。聊天 Provider 显式使用 ChatOpenAI Responses API；`web_search_enabled=true` 时在回答生成阶段绑定内置 `web_search` 工具，网络结果不进入向量召回或 rerank。

RAG 查询会校验会话和指定文档均属于请求 owner，将用户消息写入 MongoDB，然后执行混合检索、重排和模型生成。若会话仍使用默认标题，会先用现有聊天模型根据最近对话生成简短侧栏摘要并持久化，失败时退回问题精简文本；自定义标题不变。联网时，Responses 返回的 URL annotations 或 `web_search_call.action.sources` 会转换为安全 HTTP(S) 引用。`stream=true` 时依次发送 `start`、若干 `delta`、`citations`、`done`，其中 `start` 可携带 `conversation_title`；流建立后的错误通过 `error` 事件返回。assistant 消息、引用和 usage 在生成完成后写入 MongoDB。

`app/admin` 提供进程内监控与运行时配置。`/internal/v1/admin/metrics` 记录入库阶段耗时、生成 token/速度、提示词与输出；`/internal/v1/admin/config` 可调整检索/重排 Top K 和聊天模型。数据与覆盖均不持久化。

`request_id` 用于回答幂等：相同请求参数重复提交会返回已保存的 assistant 消息；同一 `request_id` 搭配不同会话、问题或文档范围会返回 HTTP 409 `REQUEST_ID_CONFLICT`。

## 测试

```bash
RUN_FULL_FLOW=0 RUN_MINIO_INTEGRATION=0 UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv run pytest -q
```

测试使用内存仓储和假的外部 Provider，不会产生 MinerU、百炼或 Zilliz 调用费用。

不启动服务的离线及真实全流程测试见
[`tests/test_flows/README.md`](../../tests/test_flows/README.md)。

Web 代理和真实外部依赖联调方式见 [`app/web/README.md`](../web/README.md)。

### 2026-09-05 真实联调记录

以下是迁移 MinIO 之前的历史记录：通过 Express `/api/v1` 上传 2,215,244 字节的 `AttentionIsAllYouNeed.pdf`，真实调用 MongoDB Atlas、Zilliz Cloud、Zilliz Managed Volume、MinerU 和百炼：

- 文档约 50 秒进入 `indexed/completed`，写入 64 个 chunks；Zilliz 查询确认 64 条记录。
- 非流式问答返回 6 条带页码引用，usage 为 1376 tokens。
- SSE 问答完整返回 `start`、`delta`、`citations`、`done`，usage 为 1818 tokens。
- MongoDB 中确认保存两条 user 消息和两条 assistant 消息。
- 删除 API 返回 204；MongoDB 文档、Zilliz 64 条向量、本地原文件和 MinerU 处理目录均已清理，会话及其消息也已删除。

该次联调创建的临时 MongoDB、Collection 数据和本地文件已清理。迁移后的原文件改由 MinIO 管理。

MinIO 迁移后又完成了一轮 Markdown 真实端到端验证：HTTP 上传、MinIO 持久化、百炼向量化、Zilliz 入库、RAG 回答、MinIO 对象删除及 MongoDB/Zilliz 测试数据清理均成功。

## 运行限制

- 入库任务由进程内异步任务执行。进程异常退出时无法恢复任务，生产部署需要替换为持久化任务队列。
- LangGraph 不启用 checkpointer 或节点自动重试；引入恢复/重试前需先设计外部写入幂等。同步 SDK 调用取消时会等待已开始的调用结束，关闭耗时受其超时配置影响。
- 入库幂等锁、RAG `request_id` 锁和 Zilliz 文档锁都包含单进程机制；多 worker 部署需要数据库级幂等记录、租约和会话互斥。
- 文档、会话和消息列表目前先读取记录后在 Python 中按 ID 游标分页，不适合超大数据量。
- 原文件只持久化在 MinIO。解析阶段会使用系统临时目录，正常完成、失败或任务取消都会清理。
