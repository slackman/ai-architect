"""Проверки вопроса и ответа модели по явным правилам."""

import re


INJECTION = re.compile(
    r"ignore\s+(all\s+)?previous|игнорируй.{0,30}инструк|system\s+prompt|системн\w*\s+промпт",
    re.I,
)
PII = re.compile(
    r"\b\d{3}-\d{3}-\d{3}\s?\d{2}\b|\b\d{4}\s\d{6}\b|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"
)
UUID_MARKER = re.compile(r"\[([0-9a-fA-F-]{36})\]")


def question_blocked(question: str) -> bool:
    """Блокировать известные шаблоны попыток подменить инструкции приложения."""
    return bool(INJECTION.search(question))


def finalize_answer(
    answer: str, citation_ids: list[str], evidence: list[dict], paths: list[dict]
) -> dict:
    """Оставить только подтверждённые цитаты и скрыть известные форматы PII."""
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S | re.I)
    markers = UUID_MARKER.findall(answer)
    ids = {item for item in citation_ids if isinstance(item, str)}
    ids.update(markers)
    allowed = {item["id"] for item in evidence}
    if not ids or not ids <= allowed:
        return {
            "status": "insufficient_evidence",
            "answer": "Не удалось подтвердить ссылки в ответе. Уточните вопрос.",
            "evidence": [],
            "paths": [],
        }
    answer = re.sub(
        r"\s*Источники:\s*(?:\[[0-9a-fA-F-]{36}\]\s*)+", "", answer, flags=re.I
    )
    answer = UUID_MARKER.sub("", answer)
    cited_ids = [
        item for item in citation_ids if isinstance(item, str) and item in allowed
    ]
    if not cited_ids:
        cited_ids = list(dict.fromkeys(markers))
    cited = set(cited_ids)
    return {
        "status": "answered",
        "answer": PII.sub("[СКРЫТО]", answer),
        "citation_ids": cited_ids,
        "evidence": [item for item in evidence if item["id"] in cited],
        "paths": [
            item for item in paths if item["id"] in cited and item["seed"] in cited
        ],
    }
