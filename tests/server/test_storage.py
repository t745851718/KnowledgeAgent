from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.server.providers import ProviderError
from app.server.providers.storage import MinioStorage, _endpoint


class FakeMinioClient:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.list_include_versions: list[bool] = []

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(self, bucket, name, source, length, **kwargs):
        del kwargs
        self.objects[(bucket, name)] = source.read(length)

    def fget_object(self, bucket, name, destination):
        Path(destination).write_bytes(self.objects[(bucket, name)])

    def remove_object(self, bucket, name):
        self.objects.pop((bucket, name), None)

    def get_object(self, bucket, name):
        from io import BytesIO

        class Response(BytesIO):
            def release_conn(self):
                return None

        return Response(self.objects[(bucket, name)])

    def list_objects(self, bucket, prefix, recursive, include_version=False):
        del recursive
        self.list_include_versions.append(include_version)
        return [
            type("Object", (), {"object_name": name, "version_id": None})
            for stored_bucket, name in self.objects
            if stored_bucket == bucket and name.startswith(prefix)
        ]

    def remove_objects(self, bucket, objects):
        for object_ in objects:
            self.objects.pop((bucket, object_.name), None)
        return []


class NonDeletingMinioClient(FakeMinioClient):
    def remove_objects(self, bucket, objects):
        del bucket
        list(objects)
        return []


def test_endpoint_normalization() -> None:
    assert _endpoint("localhost:9000", None) == ("localhost:9000", False)
    assert _endpoint("https://minio.example.com", None) == (
        "minio.example.com",
        True,
    )
    assert _endpoint("minio.example.com", True) == ("minio.example.com", True)


def test_bucket_name_is_normalized_to_lowercase() -> None:
    storage = MinioStorage(
        endpoint=None,
        access_key=None,
        secret_key=None,
        bucket_name="KnowledgeMinIO",
        client=FakeMinioClient(),
    )
    assert storage.bucket_name == "knowledgeminio"


def test_minio_upload_download_and_delete(tmp_path: Path) -> None:
    client = FakeMinioClient()
    storage = MinioStorage(
        endpoint=None,
        access_key=None,
        secret_key=None,
        bucket_name="documents",
        client=client,
    )
    storage.initialize()
    upload = UploadFile(
        filename="guide.md",
        file=BytesIO("# 测试".encode()),
        headers={"content-type": "text/markdown"},
    )
    key, size, digest = asyncio.run(
        storage.save_upload(
            upload,
            owner_id="user_1",
            document_id="doc_1",
            extension=".md",
            content_type="text/markdown",
            max_size=1024,
        )
    )

    assert key == "documents/user_1/doc_1/source.md"
    assert size == len("# 测试".encode())
    assert len(digest) == 64
    target = storage.download(key, tmp_path / "source.md")
    assert target.read_text() == "# 测试"
    assert storage.delete(key) is True
    assert client.objects == {}


def test_minio_removes_all_document_objects_and_verifies_cleanup(tmp_path: Path) -> None:
    client = FakeMinioClient()
    storage = MinioStorage(endpoint=None, access_key=None, secret_key=None, bucket_name="documents", client=client)
    upload = UploadFile(
        filename="guide.md",
        file=BytesIO("# 测试".encode()),
        headers={"content-type": "text/markdown"},
    )
    source_key, _, _ = asyncio.run(storage.save_upload(
        upload,
        owner_id="user_1",
        document_id="doc_1",
        extension=".md",
        content_type="text/markdown",
        max_size=1024,
    ))
    asset = UploadFile(
        filename="asset.png",
        file=BytesIO(b"asset-data"),
        headers={"content-type": "image/png"},
    )
    asset_key, _ = asyncio.run(storage.save_asset(
        asset,
        owner_id="user_1",
        document_id="doc_1",
        relative_path="assets/asset.png",
        max_size=1024,
    ))
    image = tmp_path / "diagram.png"
    image.write_bytes(b"png-data")

    image_key = storage.save_image(image, owner_id="user_1", document_id="doc_1", chunk_index=2, image_index=0)
    data, content_type = storage.read_image(image_key)

    assert source_key == "documents/user_1/doc_1/source.md"
    assert asset_key == "documents/user_1/doc_1/assets/assets/asset.png"
    assert image_key == "documents/user_1/doc_1/images/2_0.png"
    assert data == b"png-data" and content_type == "image/png"
    storage.delete_document_assets(owner_id="user_1", document_id="doc_1")
    assert client.objects == {}
    assert client.list_include_versions == [True, True]


def test_minio_delete_reports_objects_that_remain() -> None:
    client = NonDeletingMinioClient()
    client.objects[("documents", "documents/user_1/doc_1/source.md")] = b"source"
    storage = MinioStorage(
        endpoint=None,
        access_key=None,
        secret_key=None,
        bucket_name="documents",
        client=client,
    )

    with pytest.raises(ProviderError, match="仍残留 1 个对象") as error:
        storage.delete_document_assets(owner_id="user_1", document_id="doc_1")

    assert error.value.retryable is True


def test_minio_stores_markdown_package_assets() -> None:
    client = FakeMinioClient()
    storage = MinioStorage(endpoint=None, access_key=None, secret_key=None, bucket_name="documents", client=client)
    upload = UploadFile(
        filename="diagram.png", file=BytesIO(b"png-data"),
        headers={"content-type": "image/png"},
    )
    key, size = asyncio.run(storage.save_asset(
        upload, owner_id="user_1", document_id="doc_1",
        relative_path="course/assets/diagram.png", max_size=1024,
    ))
    assert key == "documents/user_1/doc_1/assets/course/assets/diagram.png"
    assert size == 8
    with pytest.raises(ValueError, match="相对路径"):
        storage.asset_object_name(owner_id="user_1", document_id="doc_1", relative_path="../secret.png")


def test_minio_rejects_unsafe_object_key(tmp_path: Path) -> None:
    storage = MinioStorage(
        endpoint=None,
        access_key=None,
        secret_key=None,
        bucket_name="documents",
        client=FakeMinioClient(),
    )
    with pytest.raises(ValueError, match="对象键不合法"):
        storage.download("documents/../secret", tmp_path / "secret")


def test_missing_minio_configuration_is_reported() -> None:
    storage = MinioStorage(
        endpoint=None,
        access_key=None,
        secret_key=None,
        bucket_name=None,
    )
    with pytest.raises(ProviderError, match="MINIO_ENDPOINT"):
        storage.initialize()
