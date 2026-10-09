"""Управление векторным поиском в Qdrant и связями документов в Neo4j."""

from neo4j import GraphDatabase
from qdrant_client import QdrantClient, models as qm


class Indexes:
    """Сохранять поисковые данные и применять фильтры доступа при поиске."""

    def __init__(self, settings):
        """Создать подключения к Qdrant и Neo4j по настройкам приложения."""
        self.settings = settings
        self.vector = QdrantClient(url=settings.qdrant_url, timeout=20)
        self.graph = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=10,
        )

    def close(self):
        """Закрыть подключения к обоим индексам."""
        self.vector.close()
        self.graph.close()

    def ensure(self):
        """Создать поисковые структуры или проверить размерность векторов."""
        collection = self.settings.qdrant_collection
        if not self.vector.collection_exists(collection):
            self.vector.create_collection(
                collection,
                vectors_config=qm.VectorParams(
                    size=self.settings.embedding_dimensions,
                    distance=qm.Distance.COSINE,
                ),
            )
        else:
            info = self.vector.get_collection(collection)
            dimensions = info.config.params.vectors.size
            if dimensions != self.settings.embedding_dimensions:
                raise ValueError("embedding_dimensions_mismatch")

        with self.graph.session() as session:
            session.run(
                "CREATE CONSTRAINT chunk_key IF NOT EXISTS "
                "FOR (chunk:Chunk) REQUIRE (chunk.tenant, chunk.id) IS UNIQUE"
            ).consume()
            session.run(
                "CREATE CONSTRAINT contract_key IF NOT EXISTS "
                "FOR (contract:Contract) REQUIRE (contract.tenant, contract.key) IS UNIQUE"
            ).consume()

    def health(self):
        """Проверить соединение с Qdrant и Neo4j."""
        self.vector.get_collections()
        self.graph.verify_connectivity()

    def upsert(self, chunks, vectors):
        """Сохранить векторы и графовые связи подтверждённых договоров."""
        self.ensure()
        points = [
            qm.PointStruct(id=chunk["id"], vector=vector, payload=chunk)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self.vector.upsert(
            self.settings.qdrant_collection,
            points=points,
            wait=True,
        )

        with self.graph.session() as session:
            session.run(
                """UNWIND $rows AS row
                MERGE (document:Document {tenant: row.tenant, id: row.document_id})
                MERGE (chunk:Chunk {tenant: row.tenant, id: row.id})
                SET chunk.acl = row.acl, chunk.page = row.page
                MERGE (document)-[:HAS_CHUNK]->(chunk)
                WITH chunk, row WHERE row.contract_key <> ''
                MERGE (contract:Contract {tenant: row.tenant, key: row.contract_key})
                MERGE (chunk)-[reference:REFERS_TO]->(contract)
                SET reference.source_chunk_id = row.id
                """,
                rows=chunks,
            ).consume()

    def delete(self, chunk_ids):
        """Удалить прежние записи индекса перед повторной обработкой."""
        if not chunk_ids:
            return

        collection = self.settings.qdrant_collection
        if self.vector.collection_exists(collection):
            self.vector.delete(
                collection,
                points_selector=qm.PointIdsList(points=chunk_ids),
                wait=True,
            )

        with self.graph.session() as session:
            session.run(
                "MATCH (chunk:Chunk) WHERE chunk.id IN $ids DETACH DELETE chunk",
                ids=chunk_ids,
            ).consume()

    def relink(self, chunks):
        """Обновить связи готовых фрагментов без повторного OCR и эмбеддингов."""
        if not chunks:
            return

        with self.graph.session() as session:
            session.run(
                """UNWIND $rows AS row
                MATCH (chunk:Chunk {tenant: row.tenant, id: row.id})
                      -[reference:REFERS_TO]->(:Contract)
                DELETE reference
                """,
                rows=chunks,
            ).consume()
            session.run(
                """UNWIND $rows AS row
                MERGE (document:Document {tenant: row.tenant, id: row.document_id})
                MERGE (chunk:Chunk {tenant: row.tenant, id: row.id})
                SET chunk.acl = row.acl, chunk.page = row.page
                MERGE (document)-[:HAS_CHUNK]->(chunk)
                WITH chunk, row WHERE row.contract_key <> ''
                MERGE (contract:Contract {tenant: row.tenant, key: row.contract_key})
                MERGE (chunk)-[reference:REFERS_TO]->(contract)
                SET reference.source_chunk_id = row.id
                """,
                rows=chunks,
            ).consume()

        for chunk in chunks:
            self.vector.set_payload(
                self.settings.qdrant_collection,
                payload={"contract_key": chunk["contract_key"]},
                points=[chunk["id"]],
                wait=True,
            )

    def search(self, vector, user, limit):
        """Найти похожие фрагменты только в организации и группах пользователя."""
        result = self.vector.query_points(
            self.settings.qdrant_collection,
            query=vector,
            limit=limit,
            query_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="tenant",
                        match=qm.MatchValue(value=user.tenant),
                    ),
                    qm.FieldCondition(
                        key="acl",
                        match=qm.MatchAny(any=list(user.groups)),
                    ),
                ]
            ),
        )
        return [str(point.id) for point in result.points]

    def expand(self, seed_ids, user, limit=12):
        """Найти связанные фрагменты договора с повторной проверкой ACL."""
        if not seed_ids:
            return []

        with self.graph.session() as session:
            rows = session.run(
                """MATCH (source:Chunk)-[:REFERS_TO]->(contract:Contract)
                <-[:REFERS_TO]-(neighbor:Chunk)
                WHERE source.tenant = $tenant
                  AND neighbor.tenant = $tenant
                  AND contract.tenant = $tenant
                  AND source.id IN $seeds
                  AND NOT neighbor.id IN $seeds
                  AND any(group IN source.acl WHERE group IN $groups)
                  AND any(group IN neighbor.acl WHERE group IN $groups)
                RETURN DISTINCT neighbor.id AS id,
                       source.id AS seed,
                       contract.key AS contract
                LIMIT $limit
                """,
                tenant=user.tenant,
                seeds=seed_ids,
                groups=list(user.groups),
                limit=limit,
            )
            return [dict(row) for row in rows]
