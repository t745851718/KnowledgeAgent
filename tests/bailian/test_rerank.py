import os

import dashscope
from dotenv import load_dotenv

load_dotenv()

def text_rerank():
    resp = dashscope.TextReRank.call(
        model="qwen3.7-text-rerank",
        base_url=os.getenv("BAILIAN_BASE_URL"),
        api_key=os.getenv("BAILIAN_API_KEY"),
        query="什么是文本排序模型",
        documents=[
            "文本排序模型广泛用于搜索引擎和推荐系统中，它们根据文本相关性对候选文本进行排序",
            "量子计算是计算科学的一个前沿领域",
            "预训练语言模型的发展给文本排序模型带来了新的进展"
        ],
        top_n=10,
        instruct="Given a web search query, retrieve relevant passages that answer the query."
    )
    print(resp)

if __name__ == '__main__':
    text_rerank()