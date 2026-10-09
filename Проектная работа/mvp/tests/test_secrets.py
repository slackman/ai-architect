"""Проверки миграции .env и чтения файловых секретов приложением."""

import os
from pathlib import Path

from app.config import Settings
from scripts.init_env import initialize


def test_migrate_env_secrets_without_changing_values(tmp_path: Path):
    old = (
        "MODEL_BASE_URL=https://provider.example/v1\n"
        "ACCOUNTANT_TOKEN=accountant-old\n"
        "READER_TOKEN=reader-old\n"
        "POSTGRES_PASSWORD=db-old\n"
        "NEO4J_PASSWORD=graph-old\n"
        "MINIO_ACCESS_KEY=storage-old\n"
        "MINIO_SECRET_KEY=storage-password-old\n"
        "MODEL_API_KEY=model-old\n"
        "EMBEDDING_API_KEY=embedding-old\n"
        "DATABASE_URL=postgresql://accmind:${POSTGRES_PASSWORD}@postgres:5432/accmind\n"
        "REDIS_PASSWORD=unused-old\n"
    )
    (tmp_path / ".env").write_text(old)
    assert initialize(tmp_path)
    assert (
        tmp_path / ".env"
    ).read_text() == "MODEL_BASE_URL=https://provider.example/v1\n"
    assert (tmp_path / "secrets/model_api_key").read_text().strip() == "model-old"
    assert (tmp_path / "secrets/neo4j_auth").read_text().strip() == "neo4j/graph-old"
    assert (tmp_path / "secrets/database_url").read_text() == ""
    assert os.stat(tmp_path / "secrets").st_mode & 0o777 == 0o700
    assert os.stat(tmp_path / ".env").st_mode & 0o777 == 0o600
    assert not initialize(tmp_path)


def test_settings_read_secret_files_and_escape_database_password(tmp_path: Path):
    (tmp_path / "postgres_password").write_text("a@b/c:d\n")
    (tmp_path / "accountant_token").write_text("accountant-secret\n")
    settings = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert settings.postgres_password == "a@b/c:d"
    assert (
        settings.database_url
        == "postgresql://accmind:a%40b%2Fc%3Ad@postgres:5432/accmind"
    )
    assert settings.accountant_token == "accountant-secret"


def test_first_install_creates_empty_provider_key_and_unique_tokens(tmp_path: Path):
    (tmp_path / ".env.example").write_text("TEXT_MODEL=example/model\n")
    assert initialize(tmp_path)
    assert (tmp_path / ".env").read_text() == "TEXT_MODEL=example/model\n"
    assert (tmp_path / "secrets/model_api_key").read_text() == ""
    assert (tmp_path / "secrets/accountant_token").read_text().strip()
    assert (tmp_path / "secrets/accountant_token").read_text() != (
        tmp_path / "secrets/reader_token"
    ).read_text()
    assert not initialize(tmp_path)
