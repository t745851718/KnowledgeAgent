# KnowledgeAgent 开发指南

本文适用于整个仓库，帮助后续 agent 定位实现、运行验证并保持已有契约。内容依据编写时的源码；修改相关架构、命令或协议后同步更新本文。

## 工作约定

- 使用“当前”这种结论时要先联网确认最新情况后再出结论。涉及外部模型、SDK、云服务的最新能力、限制或版本时，应核对官方资料；不要把仓库配置值当作外部服务可用性的证明。
- 开始任务先检查 `git status --short`，保留用户已有的修改和未跟踪文件。不要擅自回退、提交或推送。
- 实际行为以源码和对应测试为准。`README/doc.md`、`README/schema.md` 含早期设计；`PROGRESS.md` 是历史记录，包含迁移 MinIO 之前的 Managed Volume 方案、联调资源和旧测试数量，不能作为运行配置。
- 不输出或提交 `.env`、密钥、带凭据的连接串、用户上传内容。测试脚本也可能包含环境专用配置，阅读和分享时注意脱敏。
- 只运行任务所需的检查；真实云服务测试会写入数据、调用计费接口并执行清理，应在任务授权范围内显式启用。

## 项目概览

这是个人知识库文档问答（RAG）应用：上传 PDF、DOC、DOCX、MD/Markdown，解析并建立向量索引，再按文档范围问答并返回引用。界面以中文为主。

```text
浏览器：原生 HTML / CSS / JavaScript
  → Express :3001 /api/v1（静态页面、身份注入、代理、响应包装）
    → FastAPI :8000 /internal/v1（文档、会话、入库、RAG）
      ├─ MongoDB：文档元数据、会话、消息、引用和 usage
      ├─ MinIO：原文件对象
      ├─ MinerU：PDF / Word 解析
      ├─ 百炼：dense / sparse embedding、rerank、chat
      └─ Zilliz / Milvus：chunk 文本、元数据、双路向量与混合检索
```

Python 要求 `>=3.11`，用 `uv` 管理依赖；Web 使用 CommonJS、Express 5 和 Node 内置 `fetch`，README 要求 Node 18+。前端没有打包步骤，也没有 React/Vue。入库与 RAG 使用 LangGraph `StateGraph` 编排，图在 service 初始化时编译并复用。

## 代码导航

| 路径 | 职责与修改入口 |
| --- | --- |
| `app/server/main.py` | `create_app(settings=..., container=...)`、FastAPI lifespan、请求 ID 中间件；启动目标 `app.server.main:app` |
| `app/server/container.py` | `Container` / `build_container` 组装依赖，初始化存储和仓储，关闭后台任务和客户端 |
| `app/server/core/config.py` | `Settings`、环境变量、默认值与校验；根目录 `.env` 定位 |
| `app/server/core/ids.py` | 带 `doc_` / `conv_` / `msg_` / `req_` 前缀的 ULID 风格 ID、UTC 时间工具 |
| `app/server/domain/models.py` | Pydantic 请求/响应模型、文档状态、引用、usage |
| `app/server/api/routes.py` | 全部内部路由、owner 校验、分页、RAG 请求锁与 SSE 响应 |
| `app/server/api/errors.py`、`security.py` | `AppError` 与统一异常响应、可选内部 Bearer Token |
| `app/server/services/ingestion.py` | 入库主图、每批 dense/sparse 并行子图、上传接收与失败清理 |
| `app/server/services/rag.py` | RAG 准备图、共享的生成/保存图、幂等缓存分支、SSE 适配 |
| `app/server/services/common.py`、`health.py` | `TaskSupervisor`、可等待取消的同步调用、错误映射、健康探测 |
| `app/server/services/workflows.py` | 兼容旧导入路径，仅重导出，不再承载流程实现 |
| `app/server/services/README.md` | 图结构、并发边界和修改原则 |
| `app/server/repositories/implementations.py` | `Repository` 协议、`MongoRepository` 和 `MemoryRepository`、索引与序列化 |
| `app/server/providers/` | `bailian.py`、`mineru.py`、`zilliz.py`、`storage.py` 外部系统适配；`ProviderError` 定义在 `__init__.py` |
| `app/server/utils/text_splitter.py`、`sse.py` | 带标题/页码的 Markdown 分块、SSE 编码 |
| `app/server/utils/document_chunks.py` | MinerU 图片/文字关联、页码恢复和图文分块 |
| `app/web/server.js` | `createApp(options)`、公共/内部路径映射、可信身份注入、JSON 包装与 SSE 转发 |
| `app/web/public/` | `index.html` 页面结构、`styles.css` 响应式样式、`app.js` 状态管理/上传轮询/聊天/SSE/引用 |
| `tests/server/`、`tests/test_flows/` | Python 服务测试、离线与真实全流程测试 |
| `tests/test_multimodal_ingestion.py` | 可纳入 Git 的图文入库回归：图片关联、路径校验、融合请求、dense/sparse 对齐、临时图片清理 |
| `app/web/test/server.test.js` | Node 内置测试运行器，覆盖代理、身份、响应字段等 |
| `README/api.md` | 公共与内部 API、模型、错误和 SSE 合同；接口修改时同步 |
| `README.md`、`app/server/README.md`、`app/web/README.md` | 项目使用和分层说明；`README/todo.md` 记录后续方向 |

业务分层遵循 `api → services → repositories/providers`。共享模型和配置位于 `domain/core`。避免在路由或 Web 代理里直接实现模型调用、存储或检索流程。

## 核心流程与不变量

### 文档入库与删除

1. `IngestionService.accept` 校验 owner、扩展名、MIME、文件头和大小；支持 `.pdf/.doc/.docx/.md/.markdown`。上传幂等按 `(owner_id, Idempotency-Key)` 查询，有效窗口为 24 小时。
2. `MinioStorage.save_upload` 计算大小和 SHA-256，原文件写入 `documents/{owner_id}/{document_id}/source.<ext>`。`storage_path` 是 MinIO 对象键，不是本地路径。
3. 创建文档记录并登记进程内任务后返回 HTTP 202；202 不代表已完成入库。
4. `_process` 下载原文件至任务专用系统临时目录；Markdown 按 UTF-8 直接读取，PDF/Word 交给 MinerU。优先从 content-list 恢复标题、列表、表格和图表说明，并将 `page_idx` 转为从 1 开始的页码；否则回退到 Markdown。
5. 分块保留 `section/page/chunk_index` 和任务内 `image_paths`。正文分块与图片描述块分别处理，图片仅绑定对应描述块；长描述切分后每块保留该图。默认块大小 1200 字符、重叠 200 字符。每批最多 20 个 chunk，并行处理 dense/sparse 两路，按原顺序对齐后写入 Zilliz。
6. 状态为 `queued → processing → indexed`，错误为 `failed`；阶段为 `uploading/parsing/splitting/embedding/indexing/completed`。成功后进度 100。任务结束时清理临时目录，失败时尽力清理已写入向量。
7. 删除先标记 `deleting`、取消入库，再删除向量、MinIO 对象、文档元数据；失败记录 `DOCUMENT_DELETE_FAILED` 并转为 `failed`。会话删除单独级联删除消息，不删除知识库文档。

不要移除上传大小、文件类型、对象键和 MinerU ZIP 解压路径校验。取消 `asyncio.to_thread` 的等待不保证底层同步调用已经结束，修改取消/删除逻辑时需检查写入竞态。

入库主图为 `download → parse → split → index_batches → finish`。批次节点循环复用 `dense/sparse → write` 子图，每批不超过 20 个 chunk；每批 dense 最多 4 个并发请求，纯文本仍合批，图片按块独立融合。批次顺序写入，避免向量占用随全文增长及长文档触发图递归上限。同步节点通过 `run_sync` 在取消时等待正在执行的 SDK 调用结束，再清理临时文件。

### 检索和问答

- `RagService.prepare` 先校验会话归属及请求幂等，再选择 owner 的已入库文档。`document_ids` 未提供或为空列表都表示全部已入库文档；显式指定的文档必须存在且为 `indexed`。
- 先保存 user 消息，再进行查询向量化、Zilliz 混合检索、百炼重排，构造引用和 system prompt，加入最近 20 条 user/assistant 历史。生成完成后保存 assistant 消息、引用和 usage。
- 准备图在保存 user 后并行执行 dense、sparse 和历史读取；检索等待双路向量，prompt 等待重排与历史。缓存命中直接结束准备图，不调用模型。生成图由流式/非流式共用；SSE 只转发 `custom` 流事件，不向浏览器暴露图状态。
- 默认源码配置：dense 为 `qwen3-vl-embedding`、1024 维；sparse 为 `qwen3.7-text-embedding`；rerank 为 `qwen3.7-text-rerank`；chat 为 `deepseek-v4-flash`。这些是项目默认值，不是对线上模型状态的声明。
- 入库和查询必须保持同一向量模型组合与维度。dense 使用 COSINE、sparse 使用 IP，两路结果由 RRF 合并；默认召回 20 条、重排保留 6 条。默认 Collection 为 `knowledge_agent_chunks_vl_v1`。
- 含图块的 dense 输入是 `[{"text": 块文本}, {"image": 真实图片}]`，每块独立调用 `enable_fusion=true` 返回一个向量；纯文本块仍批量嵌入。图片在 Provider 中转为 Data URI。sparse 仅输入文本，图片使用 MinerU 描述，不传图片。融合参数作用于整个请求，不能把多个 chunk 一起融合。
- `_mineru_chunks` 从 content-list 关联图片；无 content-list 时 `_mineru_markdown_chunks` 从 MinerU Markdown 内联图片恢复关联，页码为空。图片必须存在且解析后的路径位于解析目录内，否则入库失败。直接上传的 Markdown 由 `utils/markdown_chunks.py` 解析内联、引用式与 HTML `<img>` 图片（代码示例除外），将 HTTP(S) URL 或 Base64 Data URI 作为真实图片输入交给模型服务；应用不下载远程图片。单独上传不包含相对路径图片，遇到本地路径会明确失败，需先内嵌图片。描述取 alt、title 或无描述标签，sparse 不接收图片 URL/Base64。`image_paths` 支持本地 Path 或图片输入字符串，不写入 Zilliz；MinerU 临时图片随任务清理。
- 旧版含图文档需重新入库才能获得融合向量，不自动修改已有向量或删除用户数据。
- 更换模型或维度要考虑新 Collection 或全量重建，不能混写不同向量空间。已有 Collection 的检查主要覆盖维度和 sparse 字段，不能自动发现同维度模型更换。
- 检索和向量删除必须保留 owner 过滤；chunk ID 为 `{document_id}_{chunk_index}`。Milvus 字符串限制按 UTF-8 字节：owner 128、content 65535、section 1024；不要仅按字符数判断。

### API、SSE 与幂等

| 公共入口 | 内部入口/行为 |
| --- | --- |
| `POST /api/v1/documents` | `POST /internal/v1/documents/ingestions`，multipart 上传 |
| `POST /api/v1/chat/completions` | `POST /internal/v1/rag/completions`，默认 `stream=true` |
| 其他 `/api/v1/*` | 资源路径保持不变，前缀改为 `/internal/v1` |
| `GET /api/v1/health` | 仅检查 Express 进程 |
| `GET /internal/v1/health/ready` | 检查 MongoDB、MinIO、Zilliz；不调用百炼/MinerU |

- FastAPI 成功响应直接返回资源；Express 将 JSON 成功响应包装为 `{code: "OK", message, data}`，删除成功为 204。错误包含 `code/message/request_id`，可带 `details`。不要给 SSE 加 JSON 外壳。
- SSE 顺序为 `start → delta* → citations → done`；流建立后的失败通过 `error` 事件发送。前端通过 POST `fetch` 和 `ReadableStream` 消费，代理需保持流式传输和禁用缓冲的响应头。
- JSON 非流式 RAG 返回 `{message, usage}`。流式 `done` 在 assistant 持久化后发送；中断或失败不保证保存完整 assistant。
- `X-Request-Id` 用于追踪；RAG body 的 `request_id` 用于回答幂等，缺失时使用服务端请求 ID。二者有不同校验规则，不要混淆。
- 同一 RAG `request_id` 和相同参数可复用已保存答案；会话、问题或文档列表不一致返回 409。实现使用进程内请求锁和 MongoDB 全局唯一稀疏 `request_id` 索引。失败后仅存 user 消息的重试仍可能产生重复 user 消息，不能宣称完整 exactly-once。
- 列表响应为 `{items, next_cursor, has_more}`，`limit` 为 1–100。实现先取记录后按 ID 切片，尚非数据库游标分页。

### 身份和持久化边界

- Express 从服务端配置取得 `DEVELOPMENT_OWNER_ID`（默认 `development-user`），覆盖 JSON 中的 owner 并注入 `X-Owner-Id`；不会信任浏览器传来的身份/Authorization。
- FastAPI 校验可信头与请求 owner 一致。配置 `INTERNAL_API_TOKEN` 后，包括 ready 在内的所有内部接口均要求 Bearer Token。正式登录尚未实现，固定开发 owner 不等于多用户认证。
- Web 响应会过滤内部 owner、存储路径、哈希和上传幂等键；修改字段时检查 `normalizeData` / `sanitizeDocument`，保持内部数据边界。
- MongoDB 使用 `documents/conversations/messages` 集合；持久化 `_id`，对外序列化为 `id`，日期存 UTC datetime、对外为 ISO 8601 `Z` 字符串。无请求 ID 的消息必须省略该字段，避免唯一稀疏索引对显式 null 的冲突。
- 新增仓储能力时同时更新 `Repository` 协议、Mongo 与内存实现，并保持对外序列化行为一致。消息访问须先校验所属会话的 owner。

## 本地启动与配置

在根目录执行：

```bash
uv sync --group dev
npm ci
# 终端 1
uv run uvicorn app.server.main:app --host 127.0.0.1 --port 8000 --reload
# 终端 2
SERVER_BASE_URL=http://127.0.0.1:8000 npm start
```

浏览器访问 `http://127.0.0.1:3001`；FastAPI OpenAPI 位于 `http://127.0.0.1:8000/docs`。Web 自动重启使用 `npm run dev`。若 uv 缓存权限受限，可为命令设置 `UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache`。

- Python `Settings.from_env()` 从仓库根目录加载 `.env`（`PROJECT_ROOT = Path(__file__).resolve().parents[3]`），移动配置文件时检查根目录定位测试。
- Express 不自动读取 `.env`，所需配置应传入 Node 进程环境。尤其两端的 `INTERNAL_API_TOKEN` 必须一致。
- 完整运行需配置 `MONGODB_URI/MONGODB_DATABASE`、`MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY/BUCKET_NAME`、`ZILLIZ_URI/ZILLIZ_TOKEN/ZILLIZ_COLLECTION`、`MINERU_API_KEY`、`BAILIAN_BASE_URL/BAILIAN_API_KEY`（密钥支持 `DASHSCOPE_API_KEY` 回退）。详见根 README 和 `Settings`。
- MinIO 裸 `host:port` 默认 HTTP，可由 `MINIO_SECURE` 指定；带协议 URL 按协议决定。初始化会检查并在缺失时创建 bucket；缺少 MinIO 配置会导致 lifespan 启动失败。
- 缺少 `MONGODB_URI` 时使用内存仓储，但 ready 返回 503；这不代表整个应用进入离线模式，其他 Provider 仍需配置或注入替身。
- `PROBE_PAID_DEPENDENCIES` 只是预留配置，`HealthService` 没有据此执行付费探测。

## 验证方式

常规后端改动优先运行定向离线测试；Web 改动运行 Node 测试：

```bash
RUN_FULL_FLOW=0 RUN_MINIO_INTEGRATION=0 uv run pytest -q
npm test
git diff --check
```

Python 测试通过 `create_app(settings=..., container=...)` 注入内存仓储和 Provider 替身，覆盖入库、鉴权、幂等、RAG/SSE、删除、页码和 MinIO 适配。离线全流程无需单独启动服务；真实服务的通过情况不能从离线结果推断。

需要真实集成验证且任务允许时：

```bash
RUN_MINIO_INTEGRATION=1 uv run pytest -q tests/server/test_api.py -k real_minio
RUN_FULL_FLOW=1 uv run pytest -q -s tests/test_flows/test_real_flow.py
```

真实全流程可用 `FULL_FLOW_DOCUMENT`、`FULL_FLOW_TIMEOUT` 调整输入和等待时间（默认 900 秒），详见 `tests/test_flows/README.md`。`tests/support.py` 从生产配置派生独立资源：MongoDB database 和 Zilliz Collection 加 `_test`，MinIO 实际小写 Bucket 名加 `-test`（Bucket 不支持下划线）。不修改生产 `.env`。首次初始化创建测试资源；测试结束清理本次 owner 的文档、会话、对象与向量，保留测试库、Collection 和 Bucket。

Python 测试已统一到 `tests/`，pytest 的 `testpaths` 仅包含该目录。`conftest.py` 排除各 SDK 实验脚本目录，默认运行不会触发其外部调用；真实 pytest 用例需显式开关。生成的 `tests/**/output/` 和缓存不加入 Git。实验脚本应手动按模块运行（例如 `uv run python -m tests.bailian.test_embed`），不要把实验脚本的演示结果当作集成验证。

## 后续开发注意点

- 同步 SDK 调用通过 `asyncio.to_thread` 进入线程，避免阻塞 FastAPI 事件循环；外部异常转为 `ProviderError`，服务层再映射为 API 错误。
- 入库任务、RAG 请求锁和 Zilliz 文档写入锁都受单进程生命周期限制；没有持久化任务恢复、跨 worker 租约或同会话不同请求的串行化。扩展多 worker 前应专门设计这些能力。
- MongoDB、MinIO 和 Zilliz 之间没有跨系统事务，失败清理是尽力而为；修改入库/删除需检查部分成功场景。
- 前端每 3 秒轮询入库状态；列表未自动遍历后续分页，SSE 未自动重连。模型输出 Markdown 先转义再渲染，引用使用文本节点，保留防 HTML 注入处理。
- LangGraph 已接入，但未配置 checkpointer 或节点自动重试，不具备跨进程恢复。不要直接给上传、插入向量、保存消息节点加重试，否则可能重复写入或重复计费。仓库未提供部署编排或 CI 配置。
- 修改接口时同步检查 `domain/models.py`、`api/routes.py`、`app/web/server.js`、`app/web/public/app.js`、`README/api.md` 及相关测试；修改模型/存储配置时同步检查 `Settings`、容器装配和启动说明。
