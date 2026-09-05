# 目标
这是一个知识库问答智能体项目，最终的效果是一个页面

页面上用户能上传文档，然后后台会将这个文档嵌入到Zilliz

然后页面上用户还能和agent聊天，比如问“xxx”问题，agent会通过RAG去检索然后回答用户问题

# 目录结构

## app 程序主代码
- ### web
实现web页面
1. 对接用户的聊天
2. 上传文件的请求

- ### server
1. 实现RAG检索服务接口
2. 实现接收用户上传文件然后解析入库的接口

- ### utils
工具包
1. MinerU解析PDF到MarkDown
2. MongoDB存储用户对话记录
3. chat, embed, rerank <- 对接百炼平台
4. 向量存储对接 Zilliz，原文件存储对接 MinIO
