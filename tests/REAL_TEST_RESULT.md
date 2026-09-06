# 真实全流程验证记录

验证日期：2026-09-05。使用真实 MongoDB、MinIO、MinerU、百炼和 Zilliz，未使用模型或存储替身。

| 服务 | 保留的测试资源 |
| --- | --- |
| MongoDB database | `KnowledgeMongoDB_test` |
| Zilliz Collection | `KnowledgeZillizCollection_test` |
| MinIO Bucket | `knowledgeminio-test` |

使用 `AttentionIsAllYouNeed.pdf`（2,215,244 字节）完成入库与问答。执行结果：

```text
INDEXED chunks=68 fused_chunks=14
RAG_OK citations=6 messages=4
REAL_FULL_FLOW_PASS cleanup=verified resources=retained
1 passed in 74.08s
```

验证了 MinIO 原文件大小、真实 MinerU 解析、14 次成功图文融合调用、68 条 Zilliz
chunk 与入库计数一致、带 6 条引用的非流式回答、完整 SSE 事件以及 MongoDB
持久化的 2 条 user 和 2 条 assistant 消息。

删除后检查了 MinIO 对象不可读取、MongoDB 文档和消息不存在、Zilliz 查询无该文档
向量，以及任务临时目录清理。测试库、Collection、Bucket 保留供复用，生产配置未改写。

输入样本已统一到 `tests/data/AttentionIsAllYouNeed.pdf`。复现命令：

```bash
RUN_FULL_FLOW=1 uv run pytest -q -s --tb=short tests/test_flows/test_real_flow.py
```

运行前需自行配置根 `.env`。输出有两条第三方弃用警告，不影响本次通过结果。

额外验证：离线回归 `32 passed, 2 skipped`；显式启用独立 MinIO 集成用例后
`1 passed, 10 deselected`，同样写入 `knowledgeminio-test` 并清理本次对象。

## LangGraph 迁移后复验（2026-09-05）

在上述相同测试资源中，用 `tests/data/AttentionIsAllYouNeed.pdf` 再次运行真实全流程：

```text
INDEXED chunks=68 fused_chunks=14
RAG_OK citations=6 messages=4
REAL_FULL_FLOW_PASS cleanup=verified resources=retained
1 passed in 75.60s
```

覆盖 LangGraph 入库主图/并行嵌入子图、RAG 准备图和共用回答图。结果与迁移前一致。
本次约 76 秒，上轮约 74 秒；外部服务耗时存在波动，此记录不是性能基准，也不证明
端到端提速。每批 dense 请求的并发上限、双路/历史并行由离线同步屏障测试单独验证。

迁移最终离线回归：`40 passed, 2 skipped`。新增测试还验证了图分支失败时等待其他
在途 SDK 调用完成后再清理文件，以及 SSE 断流后不保存部分 assistant。

## 功能扩展后复验（2026-09-06）

在加入图文引用渲染、可选网络检索、Markdown 文件夹上传和管理控制台后，再次使用
真实 MongoDB、MinIO、MinerU、百炼和 Zilliz 运行完整流程：

```text
INDEXED chunks=68 fused_chunks=14
RAG_OK citations=6 messages=4
REAL_FULL_FLOW_PASS cleanup=verified resources=retained
1 passed, 2 warnings in 47.47s
```

本次再次验证了真实文件上传与解析、图文融合向量、混合检索与重排、非流式回答、
完整 SSE 事件、消息持久化和跨存储清理。测试数据已清除，隔离的测试数据库、
Collection 和 Bucket 按测试约定保留复用；两条第三方弃用警告不影响验证结果。

加入会话自动摘要后再次执行真实流程，测试会话以“新对话”创建，并断言摘要在正式
回答前由真实聊天模型生成、写回 MongoDB，且非流式响应返回相同标题：

```text
INDEXED chunks=68 fused_chunks=14
TITLE_OK generated=true persisted=true
RAG_OK citations=6 messages=4
REAL_FULL_FLOW_PASS cleanup=verified resources=retained
1 passed, 2 warnings in 62.31s
```

将网络搜索迁移到 ChatOpenAI Responses API 后再次执行真实流程。第二次问答显式开启
`web_search_enabled`，断言 SSE 返回 `source_type=web`，并检查 assistant 消息已持久化
安全的 HTTP(S) 网络来源：

```text
INDEXED chunks=68 fused_chunks=14
TITLE_OK generated=true persisted=true
RAG_OK citations=6 web_sources=19 messages=4
REAL_FULL_FLOW_PASS cleanup=verified resources=retained
1 passed, 2 warnings in 74.64s
```

本次验证覆盖真实 `/responses` 流式生成、内置 `web_search` 调用、19 条联网来源的 SSE
输出和 MongoDB 持久化，以及文档、会话、MinIO 对象和 Zilliz 向量的测试后清理。
