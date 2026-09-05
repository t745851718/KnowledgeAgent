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
