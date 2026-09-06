# KnowledgeAgent Admin

管理平台由三个独立模块组成：

- `service.py`：保留最近 200 条入库与 RAG 运行记录，并管理进程级运行时配置。
- `api.py`：提供 `/internal/v1/admin/metrics` 与 `/internal/v1/admin/config`。
- `public/`：由 Express 挂载到 `/admin/` 的管理页面。

入库记录包含 uploading、parsing、splitting、embedding、indexing 等阶段耗时。RAG 记录包含聊天模型、token usage、生成速度、模型消息与输出。记录位于进程内，服务重启后清空；运行时配置同样在重启后恢复为环境变量值。

管理 API 沿用 `INTERNAL_API_TOKEN` 校验。现有 Web 仍是固定开发身份，部署到生产环境前必须为 `/admin/` 增加经过验证的管理员登录与授权层。
