"""Проверить хранение метаданных в PostgreSQL и оригиналов документов в MinIO."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from minio import Minio


MVP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MVP_DIR))

from app.config import Settings  # noqa: E402


TABLES = ("users", "documents", "chunks", "queries", "events")


def verify_persistence(settings: Settings) -> None:
    """Сверить типы хранилищ, записи PostgreSQL и объекты MinIO."""
    if not settings.database_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Настройка DATABASE_URL должна указывать на PostgreSQL.")
    if settings.object_store_backend != "minio":
        raise SystemExit("Настройка OBJECT_STORE_BACKEND должна иметь значение minio.")

    with psycopg.connect(settings.database_url) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
        document_ids = {
            row[0] for row in connection.execute("SELECT id FROM documents")
        }

    endpoint = urlparse(settings.s3_endpoint)
    client = Minio(
        endpoint.netloc,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
    object_ids = {
        item.object_name for item in client.list_objects(settings.s3_bucket)
    }

    if counts["users"] < 2 or not document_ids or not document_ids <= object_ids:
        raise SystemExit(
            "Данные PostgreSQL и объекты MinIO не совпадают: "
            f"записи={counts}, объектов={len(object_ids)}."
        )

    print(
        "Проверка пройдена: PostgreSQL "
        f"{counts}; бакет MinIO={settings.s3_bucket}, объектов={len(object_ids)}."
    )


def main() -> None:
    """Загрузить настройки приложения и запустить проверку хранилищ."""
    verify_persistence(Settings())


if __name__ == "__main__":
    main()
