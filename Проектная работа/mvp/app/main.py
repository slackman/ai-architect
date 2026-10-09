"""HTTP API и веб-интерфейс для загрузки, поиска и просмотра документов."""

import threading
import time
import uuid
from urllib.parse import quote
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    FastAPI,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    BackgroundTasks,
)
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from .config import Settings
from .repository import Repository, Principal
from .models import ModelGateway
from .indexes import Indexes
from .ingestion import Ingestion, detect_type
from .workflow import QueryWorkflow
from .telemetry import build_tracing
from .storage import ObjectStorage
from .uploads import DocumentUploadService


class QueryInput(BaseModel):
    """Вопрос пользователя и режим поиска источников для ответа."""

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="graph", pattern="^(graph|vector)$")


def create_app(settings=None, *, repo=None, indexes=None, gateway=None, storage=None):
    """Собрать приложение и его зависимости; подмены используются в тестах."""
    settings = settings or Settings()
    repo = repo or Repository(settings.data_dir, settings.database_url)
    storage = storage or ObjectStorage(settings)
    if settings.accountant_token == settings.reader_token:
        raise ValueError(
            "Configure two distinct personal tokens using scripts/init_env.py"
        )
    repo.seed_user(
        Principal("accountant", "demo", ("shared", "finance"), True),
        settings.accountant_token,
    )
    repo.seed_user(Principal("reader", "demo", ("shared",)), settings.reader_token)
    indexes = indexes or Indexes(settings)
    gateway = gateway or ModelGateway(settings)
    tracing = build_tracing(settings.otel_traces_endpoint)
    tracer = tracing.get_tracer("accmind.workflow")
    ingestion = Ingestion(repo, indexes, gateway, settings, storage)
    workflow = QueryWorkflow(repo, indexes, gateway, settings, tracer=tracer)
    queries = threading.BoundedSemaphore(2)
    attempts = defaultdict(deque)
    rate_lock = threading.Lock()
    registry = CollectorRegistry()
    count = Counter(
        "accmind_requests_total",
        "Completed workflow requests",
        ["operation", "outcome"],
        registry=registry,
    )
    duration = Histogram(
        "accmind_query_seconds", "Query workflow duration", registry=registry
    )
    stage_duration = Histogram(
        "accmind_workflow_stage_seconds",
        "Duration of each query workflow stage",
        ["stage"],
        registry=registry,
    )
    documents_processed = Counter(
        "accmind_documents_processed_total",
        "Completed document processing",
        ["outcome"],
        registry=registry,
    )

    @asynccontextmanager
    async def lifespan(app):
        """При остановке приложения закрыть индексы и провайдер трассировки."""
        yield
        indexes.close()
        tracing.shutdown()

    app = FastAPI(title="AccMind MVP", version="0.1.0", lifespan=lifespan)
    app.state.repo = repo
    bearer = HTTPBearer(auto_error=False)

    def principal(auth: HTTPAuthorizationCredentials | None = Depends(bearer)):
        """Определить пользователя по персональному Bearer-токену."""
        user = (
            repo.authenticate(auth.credentials)
            if auth and auth.scheme.lower() == "bearer"
            else None
        )
        if not user:
            raise HTTPException(401, "Требуется действующий персональный токен.")
        return user

    def limit(user):
        """Ограничить частоту загрузок и вопросов для одного пользователя."""
        with rate_lock:
            q = attempts[user.id]
            now = time.monotonic()
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= settings.requests_per_minute:
                raise HTTPException(429, "Лимит запросов. Подождите минуту.")
            q.append(now)

    def authorized_document(doc_id, user):
        """Вернуть документ только если пользователю разрешён доступ к нему."""
        doc = repo.document(doc_id, user)
        if not doc:
            raise HTTPException(404, "Документ не найден.")
        return doc

    upload_service = DocumentUploadService(
        settings, repo, storage, ingestion, documents_processed, limit
    )

    @app.get("/api/v1/me")
    def me(user=Depends(principal)):
        """Вернуть профиль и недостающие настройки обработки для загрузчика."""
        return {
            "id": user.id,
            "groups": user.groups,
            "can_upload": user.can_upload,
            "missing_configuration": (
                settings.missing_models() if user.can_upload else []
            ),
        }

    @app.get("/api/v1/documents")
    def documents(
        limit: int = 50,
        offset: int = 0,
        package_id: str | None = None,
        unbatched: bool = False,
        user=Depends(principal),
    ):
        """Выдать страницу документов, доступных пользователю."""
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "Некорректная пагинация.")
        return repo.list_documents(user, limit, offset, package_id, unbatched)

    @app.get("/api/v1/packages")
    def packages(limit: int = 20, offset: int = 0, user=Depends(principal)):
        """Выдать страницу доступных пакетов документов."""
        if not 1 <= limit <= 50 or offset < 0:
            raise HTTPException(422, "Некорректная пагинация.")
        return repo.list_packages(user, limit, offset)

    @app.post("/api/v1/packages", status_code=202)
    async def upload_package(
        background: BackgroundTasks,
        files: list[UploadFile] = File(...),
        name: str = Form(""),
        group: str = Form("shared"),
        user=Depends(principal),
    ):
        """Принять несколько файлов как один пакет загрузки."""
        return await upload_service.accept(files, group, name, user, background)

    @app.post("/api/v1/documents", status_code=202)
    async def upload(
        background: BackgroundTasks,
        file: UploadFile = File(...),
        group: str = Form("shared"),
        user=Depends(principal),
    ):
        """Принять один файл и вернуть его статус или ссылку на дубликат."""
        result = await upload_service.accept([file], group, "", user, background)
        if not result["package_id"]:
            duplicate = result["duplicates"][0]
            doc = repo.document(duplicate["document_id"], user)
            return {
                "id": duplicate["document_id"],
                "status": doc["status"],
                "duplicate": True,
                "package_id": doc.get("package_id"),
            }
        doc = result["accepted"][0]
        return {
            "id": doc["id"],
            "filename": doc["filename"],
            "status": "accepted",
            "duplicate": False,
            "package_id": result["package_id"],
        }

    @app.post("/api/v1/documents/{doc_id}/retry", status_code=202)
    def retry(doc_id: str, background: BackgroundTasks, user=Depends(principal)):
        """Повторить обработку доступного документа со статусом ошибки."""
        doc = authorized_document(doc_id, user)
        return upload_service.schedule_retry(doc_id, doc, user, background)

    @app.get("/api/v1/jobs/{doc_id}")
    def job(doc_id: str, user=Depends(principal)):
        """Вернуть текущий статус обработки документа."""
        doc = authorized_document(doc_id, user)
        return {k: doc[k] for k in ("id", "status", "error")}

    @app.get("/api/v1/documents/{doc_id}")
    def detail(doc_id: str, user=Depends(principal)):
        """Вернуть сведения о документе после проверки доступа."""
        return authorized_document(doc_id, user)

    @app.get("/api/v1/documents/{doc_id}/source")
    def source(doc_id: str, user=Depends(principal)):
        """Отдать оригинал документа без разрешения браузеру кэшировать его."""
        doc = authorized_document(doc_id, user)
        try:
            data = storage.get(doc_id)
        except Exception:
            raise HTTPException(404, "Источник недоступен.")
        kind = detect_type(data[:16])
        safe = quote(doc["filename"])
        return Response(
            data,
            media_type=kind,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{safe}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/chunks/{chunk_id}")
    def chunk(chunk_id: str, user=Depends(principal)):
        """Вернуть доступный фрагмент текста, на который ссылается ответ."""
        found = repo.evidence([chunk_id], user)
        if not found:
            raise HTTPException(404, "Источник не найден.")
        return found[0]

    @app.post("/api/v1/queries")
    def query(payload: QueryInput, user=Depends(principal)):
        """Выполнить поиск и ответ, записать историю, аудит и метрики."""
        limit(user)
        if not queries.acquire(blocking=False):
            raise HTTPException(429, "Обработка вопросов занята.")
        trace_id = str(uuid.uuid4())
        start = time.perf_counter()
        try:
            with tracer.start_as_current_span("query", record_exception=False) as span:
                trace_id = format(span.get_span_context().trace_id, "032x")
                result = workflow.invoke(user, payload.question, payload.mode)
            result["trace_id"] = trace_id
            for stage in result["stages"]:
                stage_duration.labels(stage["name"]).observe(stage["ms"] / 1000)
            repo.save_query(trace_id, user, payload.question, result)
            repo.audit(user, "query", trace_id, result["status"])
            count.labels("query", result["status"]).inc()
            return result
        except Exception:
            repo.audit(user, "query", trace_id, "failed")
            count.labels("query", "failed").inc()
            raise HTTPException(
                503,
                {
                    "code": "query_failed",
                    "message": "Проверьте доступность баз, модель и настройки API.",
                    "trace_id": trace_id,
                },
            )
        finally:
            duration.observe(time.perf_counter() - start)
            queries.release()

    @app.get("/api/v1/history")
    def history(user=Depends(principal)):
        """Вернуть историю вопросов текущего пользователя."""
        return repo.history(user)

    @app.get("/api/v1/health/live")
    def live():
        """Подтвердить, что процесс API отвечает на запросы."""
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    def ready(user=Depends(principal)):
        """Проверить хранилища и наличие настроек моделей для администратора."""
        if not user.can_upload:
            raise HTTPException(403, "Недостаточно прав.")
        try:
            indexes.health()
            repo.health()
            storage.health()
        except Exception:
            raise HTTPException(503, "Хранилища недоступны.")
        if settings.missing_models():
            raise HTTPException(503, "Модель не настроена.")
        return {
            "status": "ready",
            "note": "Проверка соединения с AI требует реального запроса.",
        }

    @app.get("/metrics")
    def metrics(user=Depends(principal)):
        """Отдать метрики Prometheus только пользователю с правом загрузки."""
        if not user.can_upload:
            raise HTTPException(403, "Недостаточно прав.")
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def headers(request, call_next):
        """Добавить к ответу защитные заголовки и запрет кэша для API."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "frame-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount(
        "/",
        StaticFiles(directory=Path(__file__).parent / "static", html=True),
        name="ui",
    )
    return app
