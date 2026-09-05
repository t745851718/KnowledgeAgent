from app.server.core import Settings
from tests.support import integration_settings


def test_integration_resources_are_derived_without_mutating_production():
    production = Settings(
        mongodb_database="KnowledgeMongoDB",
        zilliz_collection="KnowledgeZillizCollection",
        minio_bucket_name="KnowledgeMinIO",
    )
    isolated = integration_settings(production)
    assert isolated.mongodb_database == "KnowledgeMongoDB_test"
    assert isolated.zilliz_collection == "KnowledgeZillizCollection_test"
    assert isolated.minio_bucket_name == "knowledgeminio-test"
    assert production.mongodb_database == "KnowledgeMongoDB"
    assert production.minio_bucket_name == "KnowledgeMinIO"
