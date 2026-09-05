# 测试

Python 测试统一位于 `tests/`；Web 代理测试保留在 `app/web/test/`。

```bash
# 默认离线回归，真实测试由开关跳过
uv run pytest -q
npm test

# MongoDB、MinIO、MinerU、百炼、Zilliz 的真实含图 PDF 全流程
RUN_FULL_FLOW=1 uv run pytest -q -s --tb=short tests/test_flows/test_real_flow.py
```

目录：`server/` 为服务单元/接口测试，`test_multimodal_ingestion.py` 为图文回归，
`test_flows/` 为全流程，`data/` 为输入样本。`bailian/mineru/mongodb/minio` 为手动 SDK
实验，pytest 不自动收集它们；运行方式如 `uv run python -m tests.mongodb.test_mongo_cloud`。
实验脚本读取环境配置，不在导入时调用模型或连接云服务。

`support.integration_settings()` 从根 `.env` 的生产配置派生测试资源：MongoDB database
和 Zilliz Collection 加 `_test`；MinIO Bucket 转小写后加 `-test`。不需要手动改生产变量。
测试资源保留供复用，仅清理本次随机 owner/对象/会话的数据，禁止清空整个测试库。
详细流程及环境开关见 [全流程说明](test_flows/README.md)。

源码、Compose 模板和输入样本纳入 Git；`**/output/`、缓存、密钥不纳入 Git。
Compose 凭据通过环境变量注入，例如在根目录使用
`docker compose --env-file .env -f tests/minio/docker-compose.yml up -d`。
