"""Извлечение текста, проверка реквизитов и публикация документа в индексах."""

import base64
import hashlib
import io
import re
import uuid
from decimal import Decimal, InvalidOperation
from PIL import Image
import pymupdf


def normalize(value):
    """Привести текст к виду, удобному для сравнения цитат и значений."""
    return re.sub(r"\s+", " ", value).strip().casefold().replace("ё", "е")


def validate_fields(fields, pages):
    """Оставить реквизиты, чьи значение и цитата найдены на указанной странице."""
    by_page = {p["page"]: p["text"] for p in pages}
    result = {"kind": fields.get("kind", "unknown")}
    warnings = []
    for name, fact in fields.items():
        if name == "kind" or not fact:
            continue
        quote = normalize(fact["quote"])
        value = normalize(fact["value"])
        if (
            quote
            and value
            and quote in normalize(by_page.get(fact["page"], ""))
            and value in quote
        ):
            result[name] = fact
        else:
            warnings.append(f"{name}:not_grounded")
    result["warnings"] = warnings
    return result


def contract_key(fields):
    """Связать документы по номеру, дате и подтверждённым ИНН обеих сторон."""
    number = (fields.get("contract_number") or {}).get("value", "")
    date = (fields.get("contract_date") or {}).get("value", "")
    if not number or not date:
        return ""
    parties = fields.get("party_inns") or []
    if len(parties) == 2 and all(
        re.fullmatch(r"\d{10}|\d{12}", inn) for inn in parties
    ):
        return "|".join([normalize(number), normalize(date), *sorted(parties)])
    # Для документов, где виден лишь один ИНН, сохраняем прежний ключ.
    inn = re.sub(r"\s", "", (fields.get("counterparty_inn") or {}).get("value", ""))
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        return ""
    return "|".join([normalize(number), normalize(date), inn])


def party_inns(pages):
    """Найти ИНН обеих сторон в распознанном тексте без выбора роли через LLM."""
    found = {
        inn
        for page in pages
        for inn in re.findall(r"\bИНН\s*[:№]?\s*(\d{10}|\d{12})\b", page["text"], re.I)
    }
    return sorted(found) if len(found) == 2 else []


def parse_amount(value):
    """Преобразовать сумму в Decimal без потери копеек при вычислениях."""
    value = re.sub(r"(?i)руб\.?|₽|\s", "", value).replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def jpeg_url(image):
    """Уменьшить изображение и подготовить его для запроса к vision-модели."""
    image = image.convert("RGB")
    image.thumbnail((1800, 1800))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def detect_type(data):
    """Определить поддерживаемый формат по сигнатуре содержимого."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise ValueError("unsupported_file_type")


def extract_pages(data, gateway, settings):
    """Получить текст каждой страницы: из PDF напрямую или через OCR скана."""
    kind = detect_type(data)
    pages = []
    if kind == "application/pdf":
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not 1 <= len(doc) <= settings.max_pages:
                raise ValueError("pdf_encrypted_or_page_limit")
            for i, page in enumerate(doc):
                # Text PDFs keep their original text; scanned pages use the vision endpoint.
                content = page.get_text().strip()
                if len(content) < 80:
                    scale = min(2.0, 1800 / max(page.rect.width, page.rect.height))
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale), alpha=False
                    )
                    content = gateway.ocr(
                        "data:image/png;base64,"
                        + base64.b64encode(pix.tobytes("png")).decode()
                    )
                if not content.strip():
                    raise ValueError("empty_page")
                pages.append({"page": i + 1, "text": content})
    else:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("image_pixel_limit")
            pages = [{"page": 1, "text": gateway.ocr(jpeg_url(image))}]
    if sum(len(p["text"]) for p in pages) > 60000:
        raise ValueError("document_text_limit")
    return pages


class Ingestion:
    """Обработать оригинал и сделать документ доступным для поиска после индексации."""

    def __init__(self, repo, indexes, gateway, settings, storage):
        """Получить доступ к записям, индексам, моделям и оригиналам файлов."""
        self.repo = repo
        self.indexes = indexes
        self.gateway = gateway
        self.settings = settings
        self.storage = storage

    def process(self, doc_id, user):
        """Прочитать сохранённый файл; при любой ошибке пометить документ failed."""
        try:
            self.repo.status(doc_id, "extracting")
            data = self.storage.get(doc_id)
            pages = extract_pages(data, self.gateway, self.settings)
            self.process_pages(doc_id, user, pages)
        except Exception:
            self.repo.status(doc_id, "failed", "processing_failed_check_configuration")
            self.repo.audit(user, "ingest", doc_id, "failed")

    def process_pages(self, doc_id, user, pages, metadata=None):
        """Проверить поля, сохранить фрагменты и обновить оба поисковых индекса."""
        try:
            self.repo.status(doc_id, "validating")
            fields = validate_fields(self.gateway.extract(pages), pages)
            if metadata:
                fields.update(metadata)
            fields["party_inns"] = party_inns(pages)
            key = contract_key(fields)
            if not key:
                fields["warnings"].append("contract_link_unresolved")
            doc = self.repo.document(doc_id, user)
            chunks = []
            for page in pages:
                for start in range(0, len(page["text"]), 1600):
                    content = page["text"][start : start + 1800]
                    cid = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f'{user.tenant}:{doc_id}:{page["page"]}:{start}',
                        )
                    )
                    fact = fields.get("contract_number")
                    evidence_key = (
                        key
                        if fact
                        and fact["page"] == page["page"]
                        and normalize(fact["quote"]) in normalize(content)
                        else ""
                    )
                    chunks.append(
                        {
                            "id": cid,
                            "document_id": doc_id,
                            "tenant": user.tenant,
                            "page": page["page"],
                            "text": content,
                            "acl": doc["acl"],
                            "contract_key": evidence_key,
                        }
                    )
            self.repo.status(doc_id, "indexing", fields=fields)
            vectors = self.gateway.embed([c["text"] for c in chunks])
            old_ids = self.repo.chunk_ids(doc_id)
            self.indexes.delete(old_ids)
            self.repo.replace_chunks(doc_id, chunks)
            self.indexes.upsert(chunks, vectors)
            self.repo.status(doc_id, "ready")
            self.repo.audit(user, "ingest", doc_id, "ready")
        except Exception:
            self.repo.status(doc_id, "failed", "processing_failed_check_configuration")
            self.repo.audit(user, "ingest", doc_id, "failed")
