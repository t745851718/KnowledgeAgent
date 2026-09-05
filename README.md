# KnowledgeAgent

一个面向个人知识库的文档问答（RAG）项目。用户可以上传 PDF、DOC、DOCX 或 Markdown 文档，系统完成文档解析、文本切分、双路向量化、混合检索、重排，并通过流式或非流式接口返回带引用的回答。

## 项目特点

- 支持 PDF、DOC、DOCX、Markdown 文档上传
- 使用 MinerU 提取正文、标题、列表、表格和图片说明
- 使用 dense + sparse 双路向量进行混合检索
- 使用 Zilliz/Milvus 保存文本块和向量
- 使用 MongoDB 保存文档、会话、消息和回答引用
- 支持 SSE 流式输出回答和引用
- 支持 `request_id` 幂等，降低客户端重试造成重复回答的风险
- 前后端分离：Express Web 层代理 FastAPI 内部服务

## 系统架构

```text
Browser
  │
  ▼
Express Web :3001
  │  /api/v1
  ▼
FastAPI Server :8000
  ├── MinerU              文档解析
  ├── Bailian / DashScope  向量化、重排、对话生成
  ├── Zilliz / Milvus      dense+sparse 混合检索
  ├── MongoDB              文档与会话数据
  └── Local / Zilliz Volume 原文件存储
```

### 文档入库流程

```text
上传文件
  → 本地保存与创建文档记录
  → MinerU 解析（Markdown 直接读取）
  → 页面文本恢复与文本切分
  → dense 向量 + sparse 向量
  → 写入 Zilliz Collection
  → 文档状态变为 indexed
```

### 图片处理说明

图片不会直接生成 sparse 向量。MinerU 先为图片或图表生成 `image_caption` / `chart_caption`，项目将这些说明转换为文本；随后 sparse 模型对这段文本生成稀疏向量。因此图片相关的关键词检索依赖图片说明文本。

## 技术栈

- Python 3.11+
- FastAPI、Uvicorn
- Express、原生 JavaScript、CSS
- MongoDB / MongoDB Atlas
- Zilliz Cloud / Milvus
- MinerU
- 阿里云百炼 DashScope
- `qwen3-vl-embedding`：dense embedding
- `qwen3.7-text-embedding`：sparse embedding
- `qwen3.7-text-rerank`：结果重排
- `deepseek-v4-flash`：回答生成

## 环境要求

- Python 3.11 或更高版本
- Node.js 18+（建议使用 LTS）
- `uv`
- 可访问的 MongoDB
- Zilliz Cloud 或本地 Milvus
- MinerU API Key
- 百炼/DashScope API Key

## 快速开始

### 1. 安装 Python 依赖

```bash
uv sync --group dev
```

如果需要将 uv 缓存放到临时目录，可以使用：

```bash
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv sync --group dev
```

### 2. 配置环境变量

在项目根目录创建 `.env`。不要将包含密钥的 `.env` 提交到 GitHub。

```dotenv
MONGODB_URI=mongodb://user:password@localhost:27017/?authSource=admin
MONGODB_DATABASE=knowledge_agent

BAILIAN_BASE_URL=https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
BAILIAN_API_KEY=YOUR_BAILIAN_API_KEY
EMBEDDING_MODEL=qwen3-vl-embedding
SPARSE_EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=1024
RERANK_MODEL=qwen3.7-text-rerank
CHAT_MODEL=deepseek-v4-flash

MINERU_API_KEY=YOUR_MINERU_API_KEY

ZILLIZ_URI=https://YOUR_CLUSTER_ENDPOINT
ZILLIZ_TOKEN=YOUR_ZILLIZ_TOKEN
ZILLIZ_COLLECTION=knowledge_agent_chunks_vl_v1
```

可选的 Zilliz Managed Volume 配置：

```dotenv
ZILLIZ_API_KEY=YOUR_CLOUD_API_KEY
ZILLIZ_VOLUME_NAME=YOUR_VOLUME_NAME
ZILLIZ_CLOUD_ENDPOINT=https://api.cloud.zilliz.com
```

常用可选配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INTERNAL_API_TOKEN` | 空 | FastAPI 内部接口 Bearer Token |
| `MAX_UPLOAD_SIZE` | `209715200` | 上传大小上限，单位为字节 |
| `UPLOAD_DIR` | `data/uploads` | 本地原文件目录 |
| `PROCESSING_DIR` | `data/processing` | 解析产物目录 |
| `CHUNK_SIZE` | `1200` | 文本块大小 |
| `CHUNK_OVERLAP` | `200` | 文本块重叠大小 |
| `RETRIEVAL_LIMIT` | `20` | 初步召回数量 |
| `RERANK_LIMIT` | `6` | 重排后送入 LLM 的片段数量 |
| `WEB_PORT` | `3001` | Express 端口 |
| `SERVER_BASE_URL` | `http://localhost:8000` | Express 转发的 FastAPI 地址 |
| `DEVELOPMENT_OWNER_ID` | `development-user` | 开发模式下的用户 ID |

使用 MongoDB Atlas 的 `mongodb+srv://` 连接串时，运行环境必须能够解析 Atlas 的 SRV/TXT DNS 记录。

### 3. 启动 FastAPI

终端一：

```bash
uv run uvicorn app.server.main:app --host 127.0.0.1 --port 8000 --reload
```

服务地址：

- OpenAPI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/internal/v1/health/ready>

### 4. 启动 Web

终端二：

```bash
npm install
SERVER_BASE_URL=http://127.0.0.1:8000 npm start
```

然后打开 <http://127.0.0.1:3001>。

开发模式：

```bash
npm run dev
```

## API 概览

FastAPI 内部接口完整说明见 [`README/api.md`](README/api.md)。浏览器应通过 Express 的 `/api/v1` 访问。

| 方法 | Web API | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Web 进程健康检查 |
| `POST` | `/api/v1/documents` | 上传文档 |
| `GET` | `/api/v1/documents` | 查询文档 |
| `DELETE` | `/api/v1/documents/:id` | 删除文档 |
| `POST` | `/api/v1/conversations` | 创建会话 |
| `GET` | `/api/v1/conversations/:id/messages` | 查询消息 |
| `POST` | `/api/v1/chat/completions` | RAG 问答，支持 SSE |

上传接口返回 HTTP 202 只代表任务已接受。客户端需要轮询文档详情，直到状态为 `indexed` 或 `failed`。

## 向量检索

每个文本块会生成两种向量：

- `dense_vector`：`qwen3-vl-embedding`，用于语义相似度召回
- `sparse_vector`：`qwen3.7-text-embedding`，用于关键词匹配

Zilliz 将两路召回结果通过 RRF 合并，再使用 `qwen3.7-text-rerank` 重排。文档入库和查询必须使用相同的模型组合及 dense 维度；更换 dense 模型后应使用新 Collection 或执行全量重建。

## 测试

运行 Python 服务测试：

```bash
uv run pytest -q test/server
```

运行 Web 测试：

```bash
npm test
```

测试使用内存仓储和模拟 Provider，不会调用 MinerU、百炼或 Zilliz 的付费接口。

## 目录结构

```text
KnowledgeAgent/
├── app/
│   ├── server/              FastAPI 核心服务
│   │   ├── api/             路由、鉴权、错误处理
│   │   ├── core/            配置与基础工具
│   │   ├── domain/          领域模型
│   │   ├── providers/       MinerU、百炼、Zilliz、存储适配器
│   │   ├── repositories/    MongoDB 与内存仓储
│   │   └── services/        入库、检索、RAG 工作流
│   └── web/                 Express Web 层与前端页面
├── README/                  API、架构及专项文档
├── test/                    Python 与 MinerU 测试
├── data/                    本地上传文件和解析产物
├── pyproject.toml           Python 项目配置
├── package.json             Node.js 项目配置
└── uv.lock                  Python 依赖锁定文件
```

## 安全与生产注意事项

- 不要提交 `.env`、API Key、数据库密码或 Zilliz Token。
- FastAPI 内部接口应只监听本机或内网，不应直接暴露公网。
- 项目尚未接入正式登录系统，开发模式下所有浏览器请求使用同一个 `DEVELOPMENT_OWNER_ID`。
- 入库任务由 FastAPI 进程内异步执行，进程重启会中断未完成任务；生产环境建议接入持久化任务队列。
- 多 worker 部署前，需要将幂等锁、任务状态和会话互斥机制迁移到数据库或分布式锁。
- Managed Volume 的单个文件不能通过当前 SDK 自动删除，必要时需在 Zilliz 控制台清理。

## 更多文档

- [API 接口文档](README/api.md)
- [服务端说明](app/server/README.md)
- [Web 端说明](app/web/README.md)
- [架构说明](README/schema.md)
- [开发进度记录](PROGRESS.md)

## License

项目暂未声明开源许可证。如需公开发布，请补充 `LICENSE` 文件并明确授权范围。
