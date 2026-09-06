# LangGraph 工作流

`ingestion.py` 与 `rag.py` 使用已有 LangGraph 依赖。每个 service 构造时编译图，
不同请求仅共享图定义和依赖，各自持有类型化状态。`workflows.py` 是旧导入路径的兼容层。

## 入库

```text
download → parse → split → index_batches → finish
                              │ 每批最多 20 个 chunk
                              └─ dense ─┐
                                 sparse ┴→ write
```

主图负责阶段顺序；`index_batches` 顺序复用小型子图，不把每个 chunk 变为主图循环步，
不依赖随文档长度增长的 recursion_limit，也不保留全文向量。子图在 dense/sparse 两路
都结束后才写入。dense 内纯文本合批、含图块独立融合，每批最多 4 个请求并发，结果
按原 chunk 顺序对齐。图片关联和页码恢复在 `utils/document_chunks.py`。

上传校验/接收、HTTP 202、任务取消和资源清理仍由服务边界处理。`IngestionRun` 仅记录
单次执行的阶段、已写数量和是否尝试写入，供节点异常后的清理使用，不是持久化 checkpoint。
首批部分写入失败也会按 owner/document 尝试清理向量。

## RAG

```text
validate ─ 缓存命中 → 返回已保存答案
   └─ documents → save_user ┬─ dense_query → dense_search ─┐
                            ├─ sparse_query → sparse_search ┼→ weighted_rrf → rerank ─┐
                            └─ history → title_summary ─────────────────────────┴→ prompt

回答图：缓存 → replay
        新请求 → generate（Responses API，可选 web_search）→ save
```

双路查询向量和历史读取并行；默认会话标题在历史读取后、正式回答前使用现有聊天模型生成并持久化，自定义标题跳过该调用。dense、sparse 两路候选按配置权重使用 RRF 合并，无召回跳过 rerank 调用。检索准备仍在发送 SSE 响应头前
完成，因此鉴权、文档就绪和上游检索错误保持普通 HTTP 错误响应。

`complete` 和 `stream` 共用回答图。流式 `generate` 通过 `get_stream_writer` 发出
`delta/citations`，`web_search_enabled=true` 时在 Responses API 生成阶段绑定内置
`web_search`，并把 URL annotations / sources 合并到 citations；`save` 成功后发出 `done`；API 只消费 `stream_mode="custom"`，
不返回内部 state。流建立后失败发送 `error`，不保存部分 assistant。缓存分支复用已存
答案及 usage，不调用模型也不重复保存消息。

`run_sync` 保留同步 SDK 的线程适配，并在取消时等待已开始的调用结束；随后释放图片
文件或关闭生成器，防止后台线程仍使用已删除文件。等待时间受各 SDK 超时配置约束。
`sync_scope` 为图调用跟踪独立的在途 SDK 任务；即使图在一个分支失败后提前返回，
也会等其他分支的 SDK 任务结束再进入入库清理。
请求锁仍由路由持有直到完整 JSON/SSE 调用结束。

## 修改与验证

- 节点返回局部状态更新；并行分支写不同字段，汇合节点使用多来源 `add_edge`。
- 不在每次请求中重新编译图。模型及持久化依赖仍由 Container 注入，测试可替换 Provider。
- 不添加默认自动重试/checkpointer：外部存储没有跨系统事务，需要先设计写入幂等与恢复。
- `tests/test_langgraph_workflows.py` 验证实际并行、融合并发上限、缓存短路、增量流、
  断流不保存、长文档分批、部分写入清理；已有接口与图文测试继续验证公共契约。

```bash
RUN_FULL_FLOW=0 RUN_MINIO_INTEGRATION=0 uv run pytest -q
RUN_FULL_FLOW=1 uv run pytest -q -s --tb=short tests/test_flows/test_real_flow.py
```

图 API 和 custom streaming 用法参见 [官方图文档](https://docs.langchain.com/oss/python/langgraph/graph-api)
及 [官方流式文档](https://docs.langchain.com/oss/python/langgraph/streaming)。
