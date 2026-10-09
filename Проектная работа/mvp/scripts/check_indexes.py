"""Проверить Qdrant, Neo4j, ACL групп и изоляцию организаций на тестовых данных.

Скрипт не вызывает языковую модель и удаляет только данные созданной им
тестовой организации. Его запускают в контейнере API.
"""

from __future__ import annotations

import uuid

from qdrant_client import models as qdrant_models

from app.config import Settings
from app.indexes import Indexes
from app.repository import Principal


def make_rows(tenant: str) -> list[dict]:
    """Собрать три фрагмента с разными группами доступа для индексов."""
    rows = []
    for group in ("shared", "shared", "finance"):
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "tenant": tenant,
                "page": 1,
                "text": "Проверка синтетического индекса",
                "acl": [group],
                "contract_key": "test-contract",
            }
        )
    return rows


def verify_indexes(indexes: Indexes, settings: Settings, tenant: str) -> None:
    """Проверить векторный поиск, расширение графа и ограничения доступа."""
    accountant = Principal("test-accountant", tenant, ("shared", "finance"))
    reader = Principal("test-reader", tenant, ("shared",))
    other_tenant = Principal("test-other", "other-tenant", ("shared",))

    rows = make_rows(tenant)
    vector = [1.0] + [0.0] * (settings.embedding_dimensions - 1)
    indexes.upsert(rows, [vector] * len(rows))

    shared_ids = set(indexes.search(vector, reader, limit=10))
    assert shared_ids == {row["id"] for row in rows[:2]}

    reader_neighbors = indexes.expand([rows[0]["id"]], reader)
    assert {row["id"] for row in reader_neighbors} == {rows[1]["id"]}

    accountant_neighbors = indexes.expand([rows[0]["id"]], accountant)
    assert {row["id"] for row in accountant_neighbors} == {
        rows[1]["id"],
        rows[2]["id"],
    }

    assert indexes.search(vector, other_tenant, limit=10) == []


def clean_tenant(indexes: Indexes, settings: Settings, tenant: str) -> None:
    """Удалить тестовой организации записи из Qdrant и Neo4j."""
    if indexes.vector.collection_exists(settings.qdrant_collection):
        indexes.vector.delete(
            settings.qdrant_collection,
            qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="tenant",
                            match=qdrant_models.MatchValue(value=tenant),
                        )
                    ]
                )
            ),
            wait=True,
        )

    with indexes.graph.session() as session:
        session.run(
            "MATCH (node) WHERE node.tenant = $tenant DETACH DELETE node",
            tenant=tenant,
        ).consume()


def main() -> None:
    """Подключиться к реальным индексам, выполнить проверки и удалить тестовые данные."""
    settings = Settings()
    indexes = Indexes(settings)
    tenant = f"integration-{uuid.uuid4().hex}"

    try:
        verify_indexes(indexes, settings, tenant)
        print("Проверка пройдена: Qdrant, Neo4j, ACL групп и изоляция организаций.")
    finally:
        clean_tenant(indexes, settings, tenant)
        indexes.close()


if __name__ == "__main__":
    main()
