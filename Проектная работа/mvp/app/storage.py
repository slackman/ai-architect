"""Хранение исходных файлов в локальном каталоге или совместимом с S3 хранилище."""

from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse


class ObjectStorage:
    """Сохраняет и читает оригиналы документов через выбранное хранилище."""

    def __init__(self, settings):
        """Подключить MinIO или подготовить локальный каталог для файлов."""
        self.backend = settings.object_store_backend
        self.directory = settings.data_dir / "files"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = None
        self.bucket = settings.s3_bucket
        if self.backend == "minio":
            from minio import Minio

            parsed = urlparse(settings.s3_endpoint)
            endpoint = parsed.netloc or parsed.path
            if not endpoint or not settings.s3_access_key or not settings.s3_secret_key:
                raise ValueError("minio_not_configured")
            self.client = Minio(
                endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                secure=(
                    settings.s3_secure
                    if not parsed.scheme
                    else parsed.scheme == "https"
                ),
            )
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        elif self.backend != "local":
            raise ValueError("unsupported_object_store_backend")

    def put(self, key, data, content_type):
        """Сохранить оригинал под ID документа с указанным типом содержимого."""
        if self.client:
            self.client.put_object(
                self.bucket, key, BytesIO(data), len(data), content_type=content_type
            )
        else:
            (self.directory / key).write_bytes(data)

    def get(self, key):
        """Прочитать оригинал; если локального файла нет, поднять FileNotFoundError."""
        if self.client:
            response = self.client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        path = self.directory / key
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key):
        """Проверить наличие оригинала, возвращая False при ошибке хранилища."""
        try:
            if self.client:
                self.client.stat_object(self.bucket, key)
            else:
                if not (self.directory / key).exists():
                    return False
            return True
        except Exception:
            return False

    def health(self):
        """Проверить доступность MinIO или локального каталога."""
        if self.client:
            self.client.list_buckets()
        else:
            if not self.directory.exists():
                raise RuntimeError("local_object_store_unavailable")
