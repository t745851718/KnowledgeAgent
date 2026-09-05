"""MinIO Python SDK 基础用法示例。

运行步骤：
1. 在当前目录启动 MinIO：docker compose up -d
2. 安装 SDK：uv add minio（或 pip install minio）
3. 在项目根目录执行：uv run python -m tests.minio.test_minio

示例从根 .env 读取连接配置，写入生产 Bucket 名加 -test 的测试 Bucket。
只清理本次随机对象，保留 Bucket。
"""

from io import BytesIO
import uuid

from minio import Minio
from app.server.providers.storage import MinioStorage
from tests.support import integration_settings


BUCKET_NAME = ""
OBJECT_NAME = f"demo/{uuid.uuid4().hex}/hello.txt"


def create_bucket(client: Minio) -> None:
    """创建 Bucket；重复运行时如果已存在则跳过。"""
    if not client.bucket_exists(BUCKET_NAME):
        client.make_bucket(BUCKET_NAME)
        print("创建 Bucket:", BUCKET_NAME)
    else:
        print("Bucket 已存在:", BUCKET_NAME)


def upload_examples(client: Minio) -> None:
    """演示上传内存中的文本和本地文件。"""
    content = "你好，MinIO！\n这是一段由 Python SDK 上传的文本。"
    result = client.put_object(
        BUCKET_NAME,
        OBJECT_NAME,
        BytesIO(content.encode("utf-8")),
        length=len(content.encode("utf-8")),
        content_type="text/plain; charset=utf-8",
    )
    print("上传对象:", result.object_name, "ETag:", result.etag)


def query_examples(client: Minio) -> None:
    """演示列出对象、读取对象元数据和下载对象内容。"""
    print("\nBucket 中的对象：")
    for item in client.list_objects(BUCKET_NAME, prefix=OBJECT_NAME, recursive=True):
        print(item.object_name, item.size, "bytes")

    stat = client.stat_object(BUCKET_NAME, OBJECT_NAME)
    print("\n对象大小:", stat.size, "bytes")
    print("对象类型:", stat.content_type)

    response = None
    try:
        response = client.get_object(BUCKET_NAME, OBJECT_NAME)
        print("对象内容:")
        print(response.read().decode("utf-8"))
    finally:
        # get_object 返回的响应必须关闭并释放连接。
        if response is not None:
            response.close()
            response.release_conn()


def presigned_url_example(client: Minio) -> None:
    """生成一个临时下载地址，默认有效期为 1 小时。"""
    client.presigned_get_object(BUCKET_NAME, OBJECT_NAME)
    print("\n临时下载地址生成成功（不输出签名）")


def delete_examples(client: Minio) -> None:
    """演示删除对象和空 Bucket。"""
    client.remove_object(BUCKET_NAME, OBJECT_NAME)
    print("\n已删除本次示例对象，保留测试 Bucket")


def main() -> None:
    global BUCKET_NAME
    settings = integration_settings()
    storage = MinioStorage(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket_name=settings.minio_bucket_name,
        secure=settings.minio_secure,
    )
    client = storage.client
    BUCKET_NAME = storage.bucket_name

    try:
        create_bucket(client)
        upload_examples(client)
        query_examples(client)
        presigned_url_example(client)
    finally:
        delete_examples(client)
        print("\n示例数据已清理")


if __name__ == "__main__":
    main()
