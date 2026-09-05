"""PyMongo 基础用法示例。

运行步骤：
1. 在当前目录启动 MongoDB：docker compose up -d
2. 安装驱动：uv add pymongo（或 pip install pymongo）
3. 在项目根目录执行：uv run python -m tests.mongodb.test_mongo_db

本示例从根 .env 读取连接配置，数据只写入生产数据库名加 _test 的测试数据库，
使用随机示例集合并在结束时删除该集合。
"""

from pprint import pprint
import uuid

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from tests.support import integration_settings


COLLECTION_NAME = f"demo_users_{uuid.uuid4().hex[:12]}"


def insert_examples(users: Collection) -> None:
    """演示插入一条和多条文档。"""
    result = users.insert_one(
        {"name": "张三", "age": 20, "skills": ["Python", "MongoDB"]}
    )
    print("insert_one 生成的 _id:", result.inserted_id)

    result = users.insert_many(
        [
            {"name": "李四", "age": 25, "skills": ["Java", "MongoDB"]},
            {"name": "王五", "age": 30, "skills": ["Python", "FastAPI"]},
        ]
    )
    print("insert_many 生成的 _id:", result.inserted_ids)


def query_examples(users: Collection) -> None:
    """演示精确查询、条件查询、字段投影和排序。"""
    print("\n查询单条文档：")
    pprint(users.find_one({"name": "张三"}))

    print("\n查询年龄不小于 25 岁的用户：")
    cursor = users.find(
        {"age": {"$gte": 25}}, {"_id": 0, "name": 1, "age": 1}
    ).sort("age", ASCENDING)
    for user in cursor:
        pprint(user)


def update_examples(users: Collection) -> None:
    """演示 $set 修改字段和 $addToSet 向数组去重添加元素。"""
    result = users.update_one(
        {"name": "张三"},
        {"$set": {"age": 21}, "$addToSet": {"skills": "FastAPI"}},
    )
    print("\n更新匹配数:", result.matched_count)
    print("实际修改数:", result.modified_count)
    pprint(users.find_one({"name": "张三"}))


def aggregate_examples(users: Collection) -> None:
    """演示按照技能分组并统计人数。"""
    pipeline = [
        {"$unwind": "$skills"},
        {"$group": {"_id": "$skills", "user_count": {"$sum": 1}}},
        {"$sort": {"user_count": -1, "_id": 1}},
    ]

    print("\n每项技能对应的用户数：")
    for item in users.aggregate(pipeline):
        pprint(item)


def index_example(users: Collection) -> None:
    """演示创建唯一索引；重复的 name 将无法插入。"""
    index_name = users.create_index([("name", ASCENDING)], unique=True)
    print("\n创建的索引:", index_name)


def delete_example(users: Collection) -> None:
    """演示按条件删除文档。"""
    result = users.delete_one({"name": "李四"})
    print("\n删除文档数:", result.deleted_count)
    print("集合剩余文档数:", users.count_documents({}))


def main() -> None:
    settings = integration_settings()
    # 使用 with 可以保证程序结束时关闭连接、释放网络资源。
    with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5_000) as client:
        # ping 会立即访问服务器，便于尽早发现地址或账号密码错误。
        client.admin.command("ping")
        print("MongoDB 连接成功")

        users = client[settings.mongodb_database][COLLECTION_NAME]

        # 保证脚本可以重复运行，每次都从相同的初始状态开始。
        users.drop()
        try:
            insert_examples(users)
            query_examples(users)
            update_examples(users)
            aggregate_examples(users)
            index_example(users)
            delete_example(users)
        finally:
            # 示例结束后删除集合，避免测试数据长期残留。
            users.drop()
            print("\n示例集合已清理")


if __name__ == "__main__":
    main()
