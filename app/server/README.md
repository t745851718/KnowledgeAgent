# KnowledgeAgent Server

FastAPI Server 实现文档入库、MongoDB 会话记录、Zilliz 混合检索和百炼 RAG 问答。完整接口合同见 [`README/api.md`](../../README/api.md)。

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

依赖方向为 `api → services → repositories/providers`；`domain` 和 `core` 提供共享的数据结构与配置。启动入口保持为 `app.server.main:app`。

## 启动

在项目根目录安装依赖并启动：

```bash
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv sync --group dev
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv run uvicorn app.server.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后可访问：

- OpenAPI UI：<http://127.0.0.1:8000/docs>
- 就绪检查：<http://127.0.0.1:8000/internal/v1/health/ready>

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
```

若需要把原文件同步到 Zilliz Volume，再配置：

```dotenv
ZILLIZ_API_KEY=YOUR_CLOUD_API_KEY
ZILLIZ_VOLUME_NAME=YOUR_VOLUME_NAME
ZILLIZ_CLOUD_ENDPOINT=https://api.cloud.zilliz.com
```

可选配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INTERNAL_API_TOKEN` | 空 | 配置后，所有 `/internal/v1` 请求必须携带 Bearer Token |
| `MAX_UPLOAD_SIZE` | `209715200` | 上传大小上限，单位为字节 |
| `UPLOAD_DIR` | `data/uploads` | 原文件本地持久化目录 |
| `PROCESSING_DIR` | `data/processing` | MinerU 解析结果目录 |
| `CHUNK_SIZE` | `1200` | 文本块字符数 |
| `CHUNK_OVERLAP` | `200` | 相邻文本块重叠字符数 |
| `RETRIEVAL_LIMIT` | `20` | Zilliz 初步召回数量 |
| `RERANK_LIMIT` | `6` | 重排后提供给模型的片段数 |

没有设置 `MONGODB_URI` 时，服务使用进程内仓储便于接口开发，但就绪检查会将 MongoDB 标记为不可用。没有设置 `ZILLIZ_URI` 时，文档无法完成向量入库。

## 向量策略

每个文本块会并行生成两种向量：

- `dense_vector`：由 `qwen3-vl-embedding` 生成，用于语义召回。
- `sparse_vector`：由 `qwen3.7-text-embedding` 生成，用于关键词召回。

Zilliz 使用 RRF 合并两路结果，然后通过 `qwen3.7-text-rerank` 重排。查询采用同一模型组合。

## 测试

```bash
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv run pytest -q test/server
```

测试使用内存仓储和假的外部 Provider，不会产生 MinerU、百炼或 Zilliz 调用费用。

## 运行限制

- 入库任务由进程内异步任务执行。进程异常退出时无法恢复任务，生产部署需要替换为持久化任务队列。
- `VolumeFileManager` 只提供上传能力。官方 SDK 的 `VolumeManager.delete_volume()` 会删除整个 Volume 及其中全部文件，不能用于删除单个文档；Managed Volume 内单个文件或目录只能在 Zilliz 控制台删除。文档 API 因此只自动清理本地原文件和 Collection 向量。
