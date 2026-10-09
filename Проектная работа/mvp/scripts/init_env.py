"""Создать файловые Compose secrets и перенести в них значения из старого .env."""

import os
import re
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_FIELDS = {
    'ACCOUNTANT_TOKEN': 'accountant_token',
    'READER_TOKEN': 'reader_token',
    'POSTGRES_PASSWORD': 'postgres_password',
    'DATABASE_URL': 'database_url',
    'MINIO_ACCESS_KEY': 's3_access_key',
    'MINIO_SECRET_KEY': 's3_secret_key',
    'NEO4J_PASSWORD': 'neo4j_password',
    'MODEL_API_KEY': 'model_api_key',
    'EMBEDDING_API_KEY': 'embedding_api_key',
}
GENERATED = set(SECRET_FIELDS) - {'DATABASE_URL', 'MODEL_API_KEY', 'EMBEDDING_API_KEY'}
LEGACY_ONLY = {'REDIS_PASSWORD', 'S3_ACCESS_KEY', 'S3_SECRET_KEY'}
ASSIGNMENT = re.compile(r'^([A-Z][A-Z0-9_]*)=')


def _write_secret(path: Path, value: str) -> None:
    """Создать секрет один раз; файл доступен непривилегированному процессу в контейнере."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as target:
            target.write(value + '\n' if value else '')
        path.chmod(0o644)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def initialize(root: Path = ROOT) -> bool:
    """Не меняя существующие секреты, перенести старые значения и очистить .env."""
    env_path = root / '.env'
    source = env_path.read_text() if env_path.exists() else (root / '.env.example').read_text()
    lines = source.splitlines(keepends=True)
    old = {}
    for line in lines:
        match = ASSIGNMENT.match(line)
        if match:
            value = line.split('=', 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            old[match.group(1)] = value

    directory = root / 'secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('secrets must be a local directory')
    directory.chmod(0o700)

    changed = False
    for env_name, filename in SECRET_FIELDS.items():
        path = directory / filename
        value = old.get(env_name, '')
        if filename == 's3_access_key' and not value:
            value = old.get('S3_ACCESS_KEY', '')
        if filename == 's3_secret_key' and not value:
            value = old.get('S3_SECRET_KEY', '')
        if '${' in value:
            value = ''
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'{filename} must be a regular file')
            if value and path.read_text().strip() != value:
                raise ValueError(f'{filename} conflicts with .env; resolve before migration')
            continue
        if not value and env_name in GENERATED:
            value = secrets.token_urlsafe(36)
        _write_secret(path, value)
        changed = True

    auth_path = directory / 'neo4j_auth'
    auth = 'neo4j/' + (directory / 'neo4j_password').read_text().strip()
    if auth_path.exists():
        if auth_path.is_symlink() or not auth_path.is_file() or auth_path.read_text().strip() != auth:
            raise ValueError('neo4j_auth conflicts with neo4j_password')
    else:
        _write_secret(auth_path, auth)
        changed = True

    cleaned = ''.join(
        line for line in lines
        if not ((match := ASSIGNMENT.match(line)) and match.group(1) in SECRET_FIELDS.keys() | LEGACY_ONLY)
    )
    if not env_path.exists() or cleaned != source:
        temporary = env_path.with_name('.env.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as target:
            target.write(cleaned)
        os.replace(temporary, env_path)
        env_path.chmod(0o600)
        changed = True
    return changed


if __name__ == '__main__':
    if initialize():
        print('Created/migrated mvp/secrets and removed secret values from mvp/.env.')
    else:
        print('mvp/secrets already initialized; nothing changed.')
