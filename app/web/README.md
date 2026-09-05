# KnowledgeAgent Web

Express 提供浏览器页面与 `/api/v1` Web API，并将业务请求转发到 FastAPI 的 `/internal/v1`。

## 启动

```bash
npm install
SERVER_BASE_URL=http://localhost:8000 npm start
```

打开 <http://localhost:3000>。开发时可使用 `npm run dev` 自动重启。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_PORT` | `3000` | Web 服务端口 |
| `SERVER_BASE_URL` | `http://localhost:8000` | FastAPI 服务地址 |
| `INTERNAL_API_TOKEN` | 空 | 内部接口 Bearer Token |
| `DEVELOPMENT_OWNER_ID` | `development-user` | 无登录系统时注入的开发用户 ID |

上线接入登录系统后，应从经过验证的服务端会话取得 owner ID，替换开发用户 ID；不要接受浏览器提交的 `owner_id`。

## 验证

```bash
npm test
```
