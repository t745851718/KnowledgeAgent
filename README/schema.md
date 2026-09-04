# 知识库问答

![img.png](asset/img.png)

这是一个知识库问答智能体项目，最终的效果是一个页面

页面上用户能上传文档，然后后台会将这个文档嵌入到Milvus

然后页面上用户还能和agent聊天，比如问“xxx”问题，agent会通过RAG去检索然后回答用户问题

llm: deepseek-v4-flash
 
retrieve: qwen3.7-text-embedding + qwen3.7-text-rerank

vector + origin: Zilliz cloud

chat history: MongoDB cloud

PDF parser: MinerU API