"""Приём пакетов документов и запуск их фоновой обработки."""

import hashlib
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException

from .ingestion import detect_type
from .repository import Repository


class DocumentUploadService:
    """Проверяет файлы, сохраняет их и управляет одним рабочим слотом."""

    def __init__(
        self, settings, repo, storage, ingestion, processed_counter, rate_limit
    ):
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.ingestion = ingestion
        self.processed_counter = processed_counter
        self.rate_limit = rate_limit
        self.workers = threading.BoundedSemaphore(1)

    async def accept(self, files, group, package_name, user, background):
        """Принять пакет и поставить сохранённые файлы в очередь обработки."""
        if not user.can_upload or group not in user.groups:
            raise HTTPException(403, "Загрузка в эту группу запрещена.")
        if not files or len(files) > self.settings.max_batch_files:
            raise HTTPException(
                422,
                f"В одном пакете должно быть от 1 до {self.settings.max_batch_files} файлов.",
            )

        self.rate_limit(user)
        if not self.workers.acquire(blocking=False):
            raise HTTPException(
                409, "Другой пакет документов обрабатывается. Подождите."
            )

        queued = False
        try:
            prepared = await self._prepare(files)
            name = self._package_name(package_name, prepared)
            try:
                package_id, created, duplicates = self.repo.create_upload_package(
                    user,
                    group,
                    name,
                    [
                        {key: item[key] for key in ("id", "filename", "sha256")}
                        for item in prepared
                    ],
                )
            except PermissionError:
                raise HTTPException(
                    409,
                    "Один из повторно загруженных файлов недоступен этому пользователю.",
                )

            if not package_id:
                return {
                    "package_id": None,
                    "name": None,
                    "accepted": [],
                    "duplicates": duplicates,
                }

            created_ids = {item["id"] for item in created}
            accepted = self._store_new_files(prepared, created_ids)
            if accepted:
                self._schedule_package(accepted, user, background)
                queued = True

            return {
                "package_id": package_id,
                "name": name,
                "accepted": [
                    {"id": item["id"], "filename": item["filename"]} for item in created
                ],
                "duplicates": duplicates,
                "storage_failed": len(created) - len(accepted),
            }
        finally:
            for upload_file in files:
                await upload_file.close()
            if not queued:
                self.workers.release()

    async def _prepare(self, files):
        """Прочитать файлы, проверяя размер пакета и тип каждого документа."""
        prepared = []
        total_bytes = 0
        for upload_file in files:
            data = bytearray()
            while part := await upload_file.read(65536):
                data.extend(part)
                total_bytes += len(part)
                if len(data) > self.settings.max_file_bytes:
                    raise HTTPException(
                        413,
                        f'Файл {upload_file.filename or "без имени"} превышает допустимый размер.',
                    )
                if total_bytes > self.settings.max_batch_bytes:
                    raise HTTPException(
                        413, "Общий размер пакета превышает допустимый лимит."
                    )
            if not data:
                raise HTTPException(
                    422, f'Файл {upload_file.filename or "без имени"} пуст.'
                )
            try:
                content_type = detect_type(data)
            except ValueError:
                raise HTTPException(415, "Поддерживаются PDF, PNG, JPEG.")

            filename = Path(
                (upload_file.filename or "document").replace("\\", "/")
            ).name[:180]
            prepared.append(
                {
                    "id": str(uuid.uuid4()),
                    "filename": filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "data": bytes(data),
                    "content_type": content_type,
                }
            )
        return prepared

    @staticmethod
    def _package_name(provided_name, prepared):
        name = (provided_name or "").strip()[:120]
        if name:
            return name
        if len(prepared) == 1:
            return f'Загрузка: {prepared[0]["filename"]}'
        return f'Пакет {Repository.now()[:16].replace("T", " ")} UTC'

    def _store_new_files(self, prepared, created_ids):
        accepted = []
        for item in prepared:
            if item["id"] not in created_ids:
                continue
            try:
                self.storage.put(item["id"], item["data"], item["content_type"])
                accepted.append(item)
            except Exception:
                self.repo.status(item["id"], "failed", "object_storage_failed")
        return accepted

    def _schedule_package(self, accepted, user, background):
        def process_package():
            try:
                for item in accepted:
                    self.process_document(item["id"], user)
            finally:
                self.workers.release()

        background.add_task(process_package)

    def process_document(self, doc_id, user):
        """Обработать документ и записать итог в счётчик метрик."""
        try:
            self.ingestion.process(doc_id, user)
        finally:
            doc = self.repo.document(doc_id, user)
            outcome = "ready" if doc and doc["status"] == "ready" else "failed"
            self.processed_counter.labels(outcome).inc()

    def schedule_retry(self, doc_id, doc, user, background):
        """Повторно запустить документ с ошибкой, когда рабочий слот свободен."""
        if not user.can_upload:
            raise HTTPException(403, "Недостаточно прав.")
        if doc["status"] != "failed":
            raise HTTPException(409, "Повтор доступен только для ошибки.")

        self.rate_limit(user)
        if not self.workers.acquire(blocking=False):
            raise HTTPException(409, "Обработка занята.")
        self.repo.status(doc_id, "accepted")

        def process():
            try:
                self.process_document(doc_id, user)
            finally:
                self.workers.release()

        background.add_task(process)
        return {"id": doc_id, "status": "accepted"}
