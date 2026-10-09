"""Настройки приложения: параметры из окружения, секреты из отдельных файлов."""

from pathlib import Path
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Адреса хранилищ и моделей, ключи и ограничения запросов MVP."""

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), secrets_dir=str(ROOT / "secrets"), extra="ignore"
    )
    data_dir: Path = Path("data")
    database_url: str = ""
    postgres_password: str = ""
    object_store_backend: str = "local"
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "accmind-documents"
    s3_secure: bool = False
    model_base_url: str = "https://openrouter.ai/api/v1"
    model_api_key: str = ""
    text_model: str = ""
    vision_model: str = ""
    model_reasoning_effort: str = "none"
    otel_traces_endpoint: str = ""
    embedding_backend: str = "local"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_cache_dir: Path = Path("data/models")
    embedding_dimensions: int = 384
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "accmind_mvp_384_v1"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    accountant_token: str = ""
    reader_token: str = ""
    max_file_bytes: int = 10 * 1024 * 1024
    max_batch_files: int = 10
    max_batch_bytes: int = 50 * 1024 * 1024
    max_pages: int = 12
    model_timeout: float = 90
    max_output_tokens: int = 1800
    query_top_k: int = 4
    context_chars: int = 18000
    requests_per_minute: int = 10

    @model_validator(mode="after")
    def resolve_database_url(self):
        """Собрать адрес PostgreSQL из секрета, если он не задан явно для тестов."""
        if not self.database_url and self.postgres_password:
            password = quote(self.postgres_password, safe="")
            self.database_url = f"postgresql://accmind:{password}@postgres:5432/accmind"
        return self

    def missing_models(self):
        """Перечислить отсутствующие настройки, без которых модель не вызовется."""
        missing = [
            name
            for name in ("model_api_key", "text_model", "vision_model")
            if not getattr(self, name)
        ]
        if self.embedding_backend == "remote":
            if not self.embedding_model:
                missing.append("embedding_model")
            if not self.embedding_base_url:
                missing.append("embedding_base_url")
            if not (self.embedding_api_key or self.model_api_key):
                missing.append("embedding_api_key")
        return missing
