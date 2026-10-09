"""Постоянные данные MVP и серверные проверки доступа к документам."""

import hashlib
import json
import secrets
import sqlite3
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Principal:
    """Пользователь, организация и группы, установленные сервером по токену."""

    id: str
    tenant: str
    groups: tuple[str, ...]
    can_upload: bool = False


class DatabaseSession:
    """Единый интерфейс SQL-запросов для PostgreSQL и тестовой SQLite."""

    def __init__(self, connection, postgres=False):
        """Сохранить соединение и выбрать формат параметров SQL-запросов."""
        self.connection = connection
        self.postgres = postgres

    def execute(self, sql, params=()):
        """Подставить формат параметров нужной базы и выполнить запрос."""
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.connection.execute(sql, params)


class Repository:
    """Хранит записи приложения; PostgreSQL остаётся источником прав и статусов."""

    def __init__(self, directory: Path, database_url: str = ""):
        """Создать таблицы и пометить прерванную обработку как ошибку при запуске."""
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        (directory / "files").mkdir(exist_ok=True)
        self.path = directory / "metadata.sqlite3"
        self.database_url = database_url
        self.postgres = database_url.startswith(("postgresql://", "postgres://"))
        with self.db() as db:
            schema = """
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, tenant TEXT, token_hash TEXT,
              groups_json TEXT, can_upload INTEGER);
            CREATE TABLE IF NOT EXISTS upload_packages(id TEXT PRIMARY KEY, tenant TEXT, name TEXT,
              created_by TEXT, acl TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, tenant TEXT, filename TEXT,
              sha256 TEXT, acl TEXT, status TEXT, error TEXT, fields TEXT, created TEXT,
              UNIQUE(tenant, sha256));
            CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT, tenant TEXT,
              page INTEGER, text TEXT, acl TEXT, contract_key TEXT);
            CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY, user_id TEXT, tenant TEXT,
              question TEXT, response TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS events(id {event_id}, user_id TEXT, action TEXT,
              resource_id TEXT, outcome TEXT, created TEXT);
            """.format(
                event_id=(
                    "BIGSERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY"
                )
            )
            if self.postgres:
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)
            else:
                db.connection.executescript(schema)
            if self.postgres:
                column = db.execute(
                    """SELECT 1 FROM information_schema.columns
                    WHERE table_schema=current_schema() AND table_name='documents' AND column_name='package_id'"""
                ).fetchone()
            else:
                column = next(
                    (
                        row
                        for row in db.execute("PRAGMA table_info(documents)").fetchall()
                        if row["name"] == "package_id"
                    ),
                    None,
                )
            if not column:
                db.execute("ALTER TABLE documents ADD COLUMN package_id TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_tenant_package_created ON documents(tenant,package_id,created)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_packages_tenant_created ON upload_packages(tenant,created)"
            )
            db.execute(
                "UPDATE documents SET status='failed', error='interrupted' WHERE status!='ready' AND status!='failed'"
            )

    @contextmanager
    def db(self):
        """Открыть соединение, выполнить операции блока в одной транзакции и закрыть его."""
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            con = psycopg.connect(self.database_url, row_factory=dict_row)
        else:
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
        try:
            yield DatabaseSession(con, self.postgres)
            con.commit()
        finally:
            con.close()

    def health(self):
        """Проверить соединение с базой данных простым запросом."""
        with self.db() as db:
            db.execute("SELECT 1").fetchone()

    def seed_user(self, principal, token):
        """Сохранить демонстрационного пользователя и хэш его персонального токена."""
        if len(token) < 32:
            raise ValueError(
                "Each configured user token must contain at least 32 characters"
            )
        with self.db() as db:
            db.execute(
                """INSERT INTO users(id,tenant,token_hash,groups_json,can_upload) VALUES(?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET tenant=excluded.tenant,token_hash=excluded.token_hash,
                       groups_json=excluded.groups_json,can_upload=excluded.can_upload""",
                (
                    principal.id,
                    principal.tenant,
                    hashlib.sha256(token.encode()).hexdigest(),
                    json.dumps(principal.groups),
                    int(principal.can_upload),
                ),
            )

    def authenticate(self, token):
        """Найти пользователя по токену без хранения самого токена в базе."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            rows = db.execute("SELECT * FROM users").fetchall()
        for row in rows:
            if secrets.compare_digest(digest, row["token_hash"]):
                return Principal(
                    row["id"],
                    row["tenant"],
                    tuple(json.loads(row["groups_json"])),
                    bool(row["can_upload"]),
                )
        return None

    def create_document(self, doc_id, user, filename, digest, group, package_id=None):
        """Создать запись документа либо вернуть уже загруженный файл того же tenant."""
        with self.db() as db:
            existing = db.execute(
                "SELECT id,status FROM documents WHERE tenant=? AND sha256=?",
                (user.tenant, digest),
            ).fetchone()
            if existing:
                return dict(existing), False
            db.execute(
                """INSERT INTO documents
                       (id,tenant,filename,sha256,acl,status,error,fields,created,package_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc_id,
                    user.tenant,
                    filename,
                    digest,
                    json.dumps([group]),
                    "accepted",
                    None,
                    "{}",
                    self.now(),
                    package_id,
                ),
            )
        return {"id": doc_id, "status": "accepted", "package_id": package_id}, True

    def create_upload_package(self, user, group, name, documents):
        """Создать пакет и новые документы вместе; дубликаты оставить в прежнем пакете."""
        package_id = str(uuid.uuid4())
        created = []
        duplicates = []
        seen = {}
        with self.db() as db:
            for item in documents:
                digest = item["sha256"]
                if digest in seen:
                    duplicates.append(
                        {"filename": item["filename"], "document_id": seen[digest]}
                    )
                    continue
                existing = db.execute(
                    "SELECT id,tenant,acl,status FROM documents WHERE tenant=? AND sha256=?",
                    (user.tenant, digest),
                ).fetchone()
                if existing:
                    if not self.allowed(existing, user):
                        raise PermissionError("duplicate_document_not_accessible")
                    seen[digest] = existing["id"]
                    duplicates.append(
                        {"filename": item["filename"], "document_id": existing["id"]}
                    )
                    continue
                seen[digest] = item["id"]
                created.append(item)
            if not created:
                return None, [], duplicates
            db.execute(
                "INSERT INTO upload_packages VALUES(?,?,?,?,?,?)",
                (
                    package_id,
                    user.tenant,
                    name,
                    user.id,
                    json.dumps([group]),
                    self.now(),
                ),
            )
            for item in created:
                db.execute(
                    """INSERT INTO documents
                           (id,tenant,filename,sha256,acl,status,error,fields,created,package_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["id"],
                        user.tenant,
                        item["filename"],
                        item["sha256"],
                        json.dumps([group]),
                        "accepted",
                        None,
                        "{}",
                        self.now(),
                        package_id,
                    ),
                )
        return package_id, created, duplicates

    @staticmethod
    def now():
        """Вернуть текущее время UTC для записей базы."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def allowed(row, user):
        """Разрешить запись только пользователю того же tenant с общей группой ACL."""
        return row["tenant"] == user.tenant and bool(
            set(json.loads(row["acl"])) & set(user.groups)
        )

    def document(self, doc_id, user):
        """Вернуть документ только после проверки его организации и группы доступа."""
        with self.db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row is None or not self.allowed(row, user):
            return None
        result = dict(row)
        result["fields"] = json.loads(result["fields"])
        result.pop("sha256", None)
        result["acl"] = json.loads(result["acl"])
        return result

    def _acl_filter(self, column, groups):
        """Собрать SQL-условие пересечения групп пользователя и ACL записи."""
        if not groups:
            return "1=0", []
        marks = ",".join("?" for _ in groups)
        if self.postgres:
            clause = f"EXISTS (SELECT 1 FROM jsonb_array_elements_text({column}::jsonb) AS acl_group(value) WHERE acl_group.value IN ({marks}))"
        else:
            clause = (
                f"EXISTS (SELECT 1 FROM json_each({column}) WHERE value IN ({marks}))"
            )
        return clause, list(groups)

    def _visible_rows(
        self, table, user, limit, offset, package_id=None, unbatched=False
    ):
        """Прочитать страницу доступных документов с фильтром по пакету."""
        acl_sql, acl_params = self._acl_filter("d.acl", user.groups)
        where = ["d.tenant=?", acl_sql]
        params = [user.tenant, *acl_params]
        if package_id is not None:
            where.append("d.package_id=?")
            params.append(package_id)
        elif unbatched:
            where.append("d.package_id IS NULL")
        params.extend([limit, offset])
        with self.db() as db:
            rows = db.execute(
                f"SELECT d.* FROM {table} d WHERE {' AND '.join(where)} ORDER BY d.created DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._document_dict(row) for row in rows]

    @staticmethod
    def _document_dict(row):
        """Превратить строку базы в ответ API без внутреннего хэша файла."""
        result = dict(row)
        result["fields"] = json.loads(result["fields"])
        result.pop("sha256", None)
        result["acl"] = json.loads(result["acl"])
        return result

    def list_documents(
        self, user, limit=50, offset=0, package_id=None, unbatched=False
    ):
        """Вернуть страницу доступных документов с фильтром по пакету."""
        return self._visible_rows(
            "documents", user, limit, offset, package_id, unbatched
        )

    def list_packages(self, user, limit=20, offset=0):
        """Вернуть доступные пакеты, первые документы и их текущие статусы."""
        acl_sql, acl_params = self._acl_filter("p.acl", user.groups)
        with self.db() as db:
            total = db.execute(
                f"SELECT count(*) AS total FROM upload_packages p WHERE p.tenant=? AND {acl_sql}",
                [user.tenant, *acl_params],
            ).fetchone()["total"]
            rows = db.execute(
                f"""SELECT p.* FROM upload_packages p
                               WHERE p.tenant=? AND {acl_sql} ORDER BY p.created DESC LIMIT ? OFFSET ?""",
                [user.tenant, *acl_params, limit, offset],
            ).fetchall()
            package_rows = []
            for row in rows:
                package = dict(row)
                package["acl"] = json.loads(package["acl"])
                docs = db.execute(
                    """SELECT * FROM documents WHERE tenant=? AND package_id=?
                                     ORDER BY created,id LIMIT 20""",
                    (user.tenant, package["id"]),
                ).fetchall()
                visible = [
                    self._document_dict(doc) for doc in docs if self.allowed(doc, user)
                ]
                counts = {
                    status: sum(1 for doc in visible if doc["status"] == status)
                    for status in ("ready", "failed")
                }
                counts["processing"] = len(visible) - counts["ready"] - counts["failed"]
                package_rows.append(
                    {**package, "total": len(visible), **counts, "documents": visible}
                )
        return {"items": package_rows, "total": total, "limit": limit, "offset": offset}

    def status(self, doc_id, stage, error=None, fields=None):
        """Обновить этап обработки, ошибку и при необходимости извлечённые поля."""
        with self.db() as db:
            db.execute(
                "UPDATE documents SET status=?,error=? WHERE id=?",
                (stage, error, doc_id),
            )
            if fields is not None:
                db.execute(
                    "UPDATE documents SET fields=? WHERE id=?",
                    (json.dumps(fields, ensure_ascii=False), doc_id),
                )

    def save_chunks(self, chunks):
        """Записать фрагменты по устойчивым ID, обновляя существующие записи."""
        with self.db() as db:
            for c in chunks:
                db.execute(
                    """INSERT INTO chunks(id,document_id,tenant,page,text,acl,contract_key) VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET document_id=excluded.document_id,tenant=excluded.tenant,
                           page=excluded.page,text=excluded.text,acl=excluded.acl,contract_key=excluded.contract_key""",
                    (
                        c["id"],
                        c["document_id"],
                        c["tenant"],
                        c["page"],
                        c["text"],
                        json.dumps(c["acl"]),
                        c["contract_key"],
                    ),
                )

    def chunk_ids(self, document_id):
        """Получить ID фрагментов документа перед повторной индексацией."""
        with self.db() as db:
            rows = db.execute(
                "SELECT id FROM chunks WHERE document_id=?", (document_id,)
            ).fetchall()
        return [row["id"] for row in rows]

    def replace_chunks(self, document_id, chunks):
        """Заменить фрагменты документа в одной транзакции PostgreSQL/SQLite."""
        with self.db() as db:
            old = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM chunks WHERE document_id=?", (document_id,)
                ).fetchall()
            ]
            db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            for c in chunks:
                db.execute(
                    """INSERT INTO chunks(id,document_id,tenant,page,text,acl,contract_key) VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET document_id=excluded.document_id,tenant=excluded.tenant,
                           page=excluded.page,text=excluded.text,acl=excluded.acl,contract_key=excluded.contract_key""",
                    (
                        c["id"],
                        c["document_id"],
                        c["tenant"],
                        c["page"],
                        c["text"],
                        json.dumps(c["acl"]),
                        c["contract_key"],
                    ),
                )
        return old

    def evidence(self, ids, user):
        """Вернуть только фрагменты готовых документов с действующим доступом."""
        result = []
        with self.db() as db:
            for cid in dict.fromkeys(ids):
                r = db.execute(
                    "SELECT c.*, d.status, d.filename, d.fields, d.acl AS document_acl FROM chunks c "
                    "JOIN documents d ON c.document_id=d.id WHERE c.id=?",
                    (cid,),
                ).fetchone()
                if (
                    r
                    and r["status"] == "ready"
                    and self.allowed(r, user)
                    and set(json.loads(r["document_acl"])) & set(user.groups)
                ):
                    c = dict(r)
                    c["acl"] = json.loads(c["acl"])
                    c["fields"] = json.loads(c["fields"])
                    c.pop("document_acl", None)
                    result.append(c)
        return result

    def exact_chunk_ids(self, question, user, limit=12):
        """Найти явно названный договор или акт до семантического поиска."""
        normalized = re.sub(r"\s+", " ", question).strip().casefold().replace("ё", "е")
        contract_chunks = []
        related_chunks = []
        act_number = re.search(r"\bакт\w*\s*(?:№|номер)?\s*(\d+)\b", normalized)
        explicit_contract = re.search(
            r"\b(?:договор\w*|контракт\w*)\s*(?:№|номер)?\s*\d+",
            normalized,
        )
        with self.db() as db:
            documents = db.execute(
                "SELECT * FROM documents WHERE tenant=? AND status='ready'",
                (user.tenant,),
            ).fetchall()

            # «Акт № 2» задаёт номер акта. Поиск по извлечённому реквизиту
            # ограничивает результаты этим актом и исключает одноимённые
            # документы из других договоров.
            if act_number and not explicit_contract:
                for document in documents:
                    if not self.allowed(document, user):
                        continue
                    fields = json.loads(document["fields"])
                    number = str(
                        (fields.get("document_number") or {}).get("value") or ""
                    ).strip()
                    match = re.match(r"\d+", number)
                    if (
                        fields.get("kind") != "act"
                        or not match
                        or match.group() != act_number.group(1)
                    ):
                        continue
                    rows = db.execute(
                        "SELECT id FROM chunks WHERE document_id=? ORDER BY page,id",
                        (document["id"],),
                    ).fetchall()
                    related_chunks.extend(row["id"] for row in rows)
                if related_chunks:
                    return related_chunks[:limit]

            for document in documents:
                if not self.allowed(document, user):
                    continue
                fields = json.loads(document["fields"])
                number = (
                    str((fields.get("contract_number") or {}).get("value") or "")
                    .strip()
                    .casefold()
                )
                if not number:
                    continue
                escaped = re.escape(number)
                explicit = re.search(
                    rf"(?:договор\w*|контракт\w*)\s*(?:№|номер)?\s*{escaped}(?!\w)",
                    normalized,
                )
                if not explicit:
                    continue
                rows = db.execute(
                    "SELECT id FROM chunks WHERE document_id=? ORDER BY page,id",
                    (document["id"],),
                ).fetchall()
                target = (
                    contract_chunks
                    if fields.get("kind") == "contract"
                    else related_chunks
                )
                target.extend(row["id"] for row in rows)
        # When a contract exists, acts and payments must be discovered through the
        # graph. The fallback supports incomplete corpora without a contract file.
        return (contract_chunks or related_chunks)[:limit]

    def filename_chunk_ids(self, question, user, limit=12):
        """Предпочесть фрагменты явно названного файла общему поиску по смыслу."""
        normalized = re.sub(r"\s+", " ", question).strip().casefold()
        result = []
        with self.db() as db:
            documents = db.execute(
                "SELECT * FROM documents WHERE tenant=? AND status='ready'",
                (user.tenant,),
            ).fetchall()
            for document in documents:
                filename = document["filename"].casefold()
                if filename not in normalized or not self.allowed(document, user):
                    continue
                rows = db.execute(
                    "SELECT id FROM chunks WHERE document_id=? ORDER BY page,id",
                    (document["id"],),
                ).fetchall()
                result.extend(row["id"] for row in rows)
        return result[:limit]

    def history(self, user):
        """Вернуть историю пользователя, скрыв ответы с отозванным доступом к цитатам."""
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM queries WHERE user_id=? AND tenant=? ORDER BY created DESC LIMIT 20",
                (user.id, user.tenant),
            ).fetchall()
        result = []
        for row in rows:
            response = json.loads(row["response"])
            ids = [c["id"] for c in response.get("citations", [])]
            if len(self.evidence(ids, user)) != len(set(ids)):
                response = {
                    "status": "unavailable",
                    "answer": "Доступ к источникам этого ответа изменился.",
                    "citations": [],
                }
            result.append({"question": row["question"], "response": response})
        return list(reversed(result))

    def save_query(self, query_id, user, question, response):
        """Сохранить вопрос и ответ для истории текущего пользователя."""
        with self.db() as db:
            db.execute(
                "INSERT INTO queries VALUES(?,?,?,?,?,?)",
                (
                    query_id,
                    user.id,
                    user.tenant,
                    question,
                    json.dumps(response, ensure_ascii=False),
                    self.now(),
                ),
            )

    def audit(self, user, action, resource, outcome):
        """Записать действие и исход без текста документа в журнал событий."""
        with self.db() as db:
            db.execute(
                "INSERT INTO events(user_id,action,resource_id,outcome,created) VALUES(?,?,?,?,?)",
                (user.id, action, resource, outcome, self.now()),
            )
