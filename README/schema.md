# 技术栈架构

## web
1. Express

## server
FastAPI开放接口
1. RAG:   
langgraph + llm(deepseek-v4-flash) + embed(多模态: qwen3-vl-embedding) + rerank(qwen3.7-text-rerank)

2. 解析文档   
- 接收Word, PDF, md文档, 动态路由，配置不同类型文件不同处理，目前Word，PDF都用MinerU解析为md, 然后md用text_split统一处理
- 由embed转为向量存入zilliz, 原文件写入zilliz-volume

## utils
- MongoDB存储用户对话记录