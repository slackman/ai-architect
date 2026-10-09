"""Перестроить связи уже загруженных документов без вызова моделей."""

import json

from app.config import Settings
from app.indexes import Indexes
from app.ingestion import contract_key, normalize, party_inns
from app.repository import Repository


def relink_existing_documents(repo, indexes):
    """Восстановить ключи из проверенных полей и сохранённого текста страниц."""
    updated_chunks = []
    document_count = 0

    with repo.db() as db:
        documents = db.execute(
            "SELECT id,tenant,fields,acl FROM documents WHERE status='ready'"
        ).fetchall()
        for document in documents:
            chunks = db.execute(
                "SELECT id,document_id,tenant,page,text,acl FROM chunks "
                "WHERE document_id=? ORDER BY page,id",
                (document['id'],),
            ).fetchall()
            if not chunks:
                continue

            fields = json.loads(document['fields'])
            fields['party_inns'] = party_inns(
                [{'text': chunk['text']} for chunk in chunks]
            )
            key = contract_key(fields)
            number = fields.get('contract_number') or {}
            db.execute(
                'UPDATE documents SET fields=? WHERE id=?',
                (json.dumps(fields, ensure_ascii=False), document['id']),
            )

            for chunk in chunks:
                contains_reference = (
                    bool(key)
                    and bool(number.get('quote'))
                    and number.get('page') == chunk['page']
                    and normalize(number.get('quote', '')) in normalize(chunk['text'])
                )
                chunk_key = key if contains_reference else ''
                db.execute(
                    'UPDATE chunks SET contract_key=? WHERE id=?',
                    (chunk_key, chunk['id']),
                )
                updated_chunks.append({
                    'id': chunk['id'],
                    'document_id': chunk['document_id'],
                    'tenant': chunk['tenant'],
                    'page': chunk['page'],
                    'acl': json.loads(chunk['acl']),
                    'contract_key': chunk_key,
                })
            document_count += 1

    indexes.relink(updated_chunks)
    return document_count, len(updated_chunks)


def main():
    """Подключиться к хранилищам приложения и сообщить объём обновления."""
    settings = Settings()
    repo = Repository(settings.data_dir, settings.database_url)
    indexes = Indexes(settings)
    try:
        documents, chunks = relink_existing_documents(repo, indexes)
    finally:
        indexes.close()
    print(f'Обновлены связи: документов {documents}, фрагментов {chunks}.')


if __name__ == '__main__':
    main()
