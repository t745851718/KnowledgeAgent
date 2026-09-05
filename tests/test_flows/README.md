# 全流程测试

这组测试直接在 pytest 进程内运行 FastAPI lifespan 和 HTTP 接口，不需要预先启动
Uvicorn 或 Express。

## 离线流程

```bash
uv run pytest -q tests/test_flows/test_offline_flow.py
```

使用内存 MongoDB/MinIO 替身和确定性的 MinerU、百炼、Zilliz 替身，覆盖上传、对象
存储、解析、向量入库、RAG、SSE、删除和临时目录清理，不产生外部调用。

## 真实全流程

先确保根目录 `.env` 中的生产连接配置可用。测试通过 `tests/support.py` 派生资源名称，不改写 `.env`：MongoDB database 和 Zilliz Collection 为 `{生产名称}_test`，MinIO 为 `{生产Bucket小写名称}-test`（Bucket 不允许下划线）。首次运行创建测试资源，完成后保留它们供复用。

本项目配置对应 `KnowledgeMongoDB_test`、`KnowledgeZillizCollection_test` 和 `knowledgeminio-test`。执行：

```bash
RUN_FULL_FLOW=1 uv run pytest -q -s tests/test_flows/test_real_flow.py
```

默认上传 `tests/data/AttentionIsAllYouNeed.pdf`，最长等待 900 秒。可覆盖：

```bash
FULL_FLOW_DOCUMENT=tests/data/example.pdf \
FULL_FLOW_TIMEOUT=600 \
RUN_FULL_FLOW=1 \
uv run pytest -q -s tests/test_flows/test_real_flow.py
```

测试使用随机 owner 和幂等键；成功或失败都会尽力删除文档、会话、MinIO 对象、
MongoDB 元数据和 Zilliz 向量。真实测试会调用计费服务，因此必须显式设置
`RUN_FULL_FLOW=1`。

验证范围包括原文件上传、真实 MinerU 解析、真实图文融合 dense 与文本 sparse、Zilliz 写入数量、检索重排、非流式/SSE 问答、MongoDB 四条消息持久化，以及对象/向量/消息/临时目录清理。默认论文 PDF 必须出现成功的图文融合调用；日志只输出资源名、计数与结果，不输出密钥或全文。
