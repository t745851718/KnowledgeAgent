"""Derive isolated integration resources without changing production environment values."""

from dataclasses import replace

from app.server.core import Settings


def integration_settings(production: Settings | None = None) -> Settings:
    source = production if production is not None else Settings.from_env()
    if not source.minio_bucket_name:
        raise ValueError("集成测试需要配置 BUCKET_NAME")
    return replace(
        source,
        mongodb_database=f"{source.mongodb_database}_test",
        zilliz_collection=f"{source.zilliz_collection}_test",
        minio_bucket_name=f"{source.minio_bucket_name.lower()}-test",
    )
