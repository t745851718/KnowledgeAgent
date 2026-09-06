# KnowledgeAgent Web

Express 提供浏览器单页应用和 `/api/v1` Web API，并将核心业务请求转发到 FastAPI 的 `/internal/v1`。浏览器不直接访问 FastAPI，也不提交可信的 owner ID。

## 已实现功能

- 上传 PDF、DOC、DOCX、Markdown，可多选或拖放。
- 每 3 秒轮询文档状态，展示解析、向量化和入库进度。
- 查看和删除文档。
- 创建、选择、刷新和删除会话；首轮回答前自动生成简短侧栏摘要标题。
- 查询历史消息。
- 使用全部已入库文档或指定文档进行问答。
- 用 `fetch` 消费 POST SSE 流，展示增量回答和带页码引用。

Web API 与内部接口的主要映射：

| Web API | FastAPI API |
| --- | --- |
| `POST /api/v1/documents` | `POST /internal/v1/documents/ingestions` |
| `POST /api/v1/chat/completions` | `POST /internal/v1/rag/completions` |
| 其他 `/api/v1/*` | 保持资源路径，前缀改为 `/internal/v1` |

`GET /api/v1/health` 只检查 Express 进程，不探测 FastAPI、MongoDB 或 Zilliz；核心依赖状态由 FastAPI 的 `/internal/v1/health/ready` 返回。

## 启动

```bash
cp .env.example .env
npm ci
npm start
```

打开 <http://localhost:3001>。开发时可使用 `npm run dev` 自动重启。

完整本地启动需要两个终端：

```bash
# 终端 1
UV_CACHE_DIR=/private/tmp/knowledgeagent-uv-cache uv run uvicorn app.server.main:app --host 127.0.0.1 --port 8000 --reload

# 终端 2
npm start
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_PORT` | `3001` | Web 服务端口 |
| `SERVER_BASE_URL` | `http://localhost:8000` | FastAPI 服务地址 |
| `INTERNAL_API_TOKEN` | 空 | 内部接口 Bearer Token |
| `DEVELOPMENT_OWNER_ID` | `development-user` | 无登录系统时注入的开发用户 ID |

Express 与 FastAPI 共用仓库根目录的 `.env`；显式传入 Node 进程的环境变量优先于文件值。

Express 会为所有代理请求添加 `X-Request-Id` 和可信 `X-Owner-Id`。JSON 请求中的 `owner_id` 会被覆盖为 `DEVELOPMENT_OWNER_ID`；multipart 上传依靠 `X-Owner-Id` 传递身份。若 FastAPI 配置了 `INTERNAL_API_TOKEN`，Web 进程必须配置相同的值。

回答中的 `[[cite:N]]` 是持久化的机器锚点，页面不会显示锚点或检索原文，而是把第 N 条 citation 的关联图片插入对应句段。历史消息重新打开时使用同一映射恢复图片位置。

新会话首次提问时，FastAPI 会先用现有聊天模型生成简短摘要标题并通过 SSE `start` 事件返回；侧栏和页头会在答案开始输出前同步更新。摘要失败时使用问题的精简文本作为兜底，不影响回答。

勾选“联网检索”后，回答模型通过 Responses API 的内置 `web_search` 获取网络信息。页面在回答底部展示去重后的“网络来源”链接；只渲染 HTTP(S) URL，并使用新标签页与 `noopener noreferrer` 打开。关闭开关时模型请求不绑定联网工具。

文档库同时提供普通文件和 Markdown 文件夹上传。文件夹中每个 Markdown 作为独立文档入库，包内图片按 `webkitRelativePath` 保留路径；文档卡片显示成功解析与缺失的图片数。

Express 将 `app/admin/public` 独立挂载到 `/admin/`，管理页通过同一代理访问 `/api/v1/admin/*`。当前页面处于开发身份边界，生产环境需额外管理员认证。

文档和会话响应返回浏览器前会移除 `owner_id`、本地存储路径、Volume 路径、文件哈希和幂等键等内部字段。

上线接入登录系统后，应从经过验证的服务端会话取得 owner ID，替换固定的开发用户 ID；不要接受浏览器提交的 `owner_id`。FastAPI 内部接口应只监听内网或本机地址。

## 验证

```bash
npm test
```

测试覆盖代理路径映射、Web 健康响应、会话转发、错误包装和可信 owner 注入，不调用付费外部服务。

## 真实联调

2026-09-05 使用 `WEB_PORT=3100`、真实 FastAPI 和云端依赖完成联调：PDF 上传后生成 64 个 chunks；非流式和 SSE 流式问答均返回有效回答及 6 条引用；MongoDB 保存 4 条消息；删除后文档、向量、会话和消息均不可再查询。详细记录见 [`app/server/README.md`](../server/README.md)。

## 已知限制

- 还没有正式登录和会话认证，默认所有浏览器访问都映射到同一个开发 owner。
- 上传任务由 FastAPI 进程内执行，Web 刷新不会丢失轮询目标，但 FastAPI 重启会中断尚未完成的任务。
- SSE 断线不会自动重连；客户端重试时应复用同一 `request_id`，避免重复生成。
- 文档和会话列表暂未自动加载后续分页。
