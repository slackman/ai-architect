"""Этапы поиска, проверки источников и формирования ответа на вопрос."""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from opentelemetry import trace

from .guardrails import finalize_answer, question_blocked
from .ingestion import contract_key, normalize, parse_amount


CONTRACT_NUMBER_IN_QUESTION = re.compile(
    r"\b(?:договор\w*|контракт\w*)\s*(?:№|номер)?\s*(\d+)\b",
    re.I,
)


def requested_contract_number(question):
    """Вернуть номер, если вопрос однозначно называет один договор."""
    numbers = set(CONTRACT_NUMBER_IN_QUESTION.findall(question))
    return next(iter(numbers)) if len(numbers) == 1 else None


class State(TypedDict, total=False):
    """Данные запроса, передаваемые между этапами графа."""

    user: Any
    question: str
    mode: str
    ids: list[str]
    seeds: list[str]
    evidence: list[dict]
    paths: list[dict]
    calculations: dict
    answer: str
    citation_ids: list[str]
    filename_scoped: bool
    requested_contract_number: str | None
    status: str
    stages: list[dict]


class QueryWorkflow:
    """Выполняет управляемый граф поиска и останавливается без достаточных источников."""

    STAGES = (
        "guard",
        "retrieve",
        "expand",
        "validate",
        "calculate",
        "generate",
        "output",
    )

    def __init__(self, repo, indexes, gateway, settings, tracer=None):
        """Собрать граф обработки вопроса и подключить трассировщик."""
        self.tracer = tracer or trace.get_tracer(__name__)
        self.repo = repo
        self.indexes = indexes
        self.gateway = gateway
        self.settings = settings

        graph = StateGraph(State)
        for name in self.STAGES:
            graph.add_node(name, self.instrument(name, getattr(self, name)))

        graph.set_entry_point("guard")
        graph.add_conditional_edges(
            "guard",
            lambda state: END if state.get("status") == "blocked" else "retrieve",
        )
        graph.add_edge("retrieve", "expand")
        graph.add_edge("expand", "validate")
        graph.add_conditional_edges(
            "validate",
            lambda state: END if state.get("status") else "calculate",
        )
        graph.add_edge("calculate", "generate")
        graph.add_edge("generate", "output")
        graph.add_edge("output", END)
        self.graph = graph.compile()

    def instrument(self, name, function):
        """Записать для этапа время выполнения и трассировочный спан."""

        def run(state):
            """Выполнить этап и добавить его длительность в состояние запроса."""
            started_at = time.perf_counter()
            with self.tracer.start_as_current_span(name, record_exception=False):
                result = function(state)
            duration_ms = round((time.perf_counter() - started_at) * 1000, 1)
            stage = {"name": name, "ms": duration_ms}
            return {**result, "stages": state.get("stages", []) + [stage]}

        return run

    def guard(self, state):
        """Остановить известные попытки подмены инструкций через вопрос."""
        if question_blocked(state["question"]):
            return {
                "status": "blocked",
                "answer": "Запрос заблокирован политикой безопасности.",
            }
        return {}

    def retrieve(self, state):
        """Искать семантически либо сначала находить точный договор."""
        user = state["user"]
        question = state["question"]
        contract_number = requested_contract_number(question)
        file_ids = self.repo.filename_chunk_ids(question, user)
        exact_ids = (
            self.repo.exact_chunk_ids(question, user)
            if state["mode"] == "graph" and not file_ids
            else []
        )

        if file_ids:
            candidate_ids = file_ids
        elif exact_ids:
            candidate_ids = exact_ids
        elif contract_number and state["mode"] == "graph":
            # Явный номер договора ограничивает поиск точным совпадением.
            candidate_ids = []
        else:
            vector = self.gateway.embed([question])[0]
            candidate_ids = self.indexes.search(
                vector,
                user,
                self.settings.query_top_k,
            )

        seeds = [evidence["id"] for evidence in self.repo.evidence(candidate_ids, user)]
        return {
            "ids": seeds,
            "seeds": seeds,
            "filename_scoped": bool(file_ids),
            "requested_contract_number": contract_number,
        }

    def expand(self, state):
        """Дополнить найденные фрагменты связанными частями договора."""
        if state["mode"] == "vector" or state.get("filename_scoped"):
            paths = []
        else:
            paths = self.indexes.expand(state["seeds"], state["user"])

        ids = list(dict.fromkeys(state["ids"] + [path["id"] for path in paths]))
        return {"ids": ids, "paths": paths}

    def validate(self, state):
        """Повторно проверить права и ограничить размер контекста модели."""
        evidence = self.repo.evidence(state["ids"], state["user"])
        requested_number = state.get("requested_contract_number")
        if requested_number:
            evidence = [
                item
                for item in evidence
                if str(
                    (item["fields"].get("contract_number") or {}).get("value") or ""
                ).strip()
                == requested_number
            ]
        selected = []
        remaining_chars = self.settings.context_chars

        for item in evidence:
            if len(item["text"]) <= remaining_chars:
                selected.append(item)
                remaining_chars -= len(item["text"])

        valid_ids = {item["id"] for item in selected}
        paths = [
            path
            for path in state.get("paths", [])
            if path["id"] in valid_ids and path["seed"] in valid_ids
        ]

        if not selected:
            answer = (
                f"По доступным загруженным документам сведения о договоре № {requested_number} не найдены."
                if requested_number
                else "Недостаточно доступных источников для ответа."
            )
            return {
                "evidence": [],
                "paths": [],
                "status": "insufficient_evidence",
                "answer": answer,
            }

        contract_keys = {
            contract_key(item["fields"])
            for item in selected
            if contract_key(item["fields"])
        }
        if len(contract_keys) > 1:
            return {
                "evidence": [],
                "paths": [],
                "status": "insufficient_evidence",
                "answer": "Найдено несколько договоров. Уточните номер, дату и контрагента.",
            }

        return {"evidence": selected, "paths": paths}

    def calculate(self, state):
        """Сложить подтверждённые суммы документов одного определённого договора."""
        contract_keys = {
            contract_key(item["fields"])
            for item in state["evidence"]
            if contract_key(item["fields"])
        }
        if len(contract_keys) != 1:
            return {
                "calculations": {
                    "note": "Агрегирование не выполнено: договор не определён однозначно."
                }
            }

        key = next(iter(contract_keys))
        seen_documents = set()
        totals = {"act": Decimal(0), "payment_order": Decimal(0)}
        counts = {kind: 0 for kind in totals}

        for evidence in state["evidence"]:
            fields = evidence["fields"]
            kind = fields.get("kind")
            amount_fact = fields.get("amount")
            if (
                evidence["document_id"] in seen_documents
                or contract_key(fields) != key
                or kind not in totals
                or not amount_fact
            ):
                continue

            # Учитывать сумму, только если переданная модели цитата её подтверждает.
            quote_is_in_context = amount_fact["page"] == evidence["page"] and normalize(
                amount_fact["quote"]
            ) in normalize(evidence["text"])
            if not quote_is_in_context:
                continue

            amount = parse_amount(amount_fact["value"])
            if amount is None:
                continue

            seen_documents.add(evidence["document_id"])
            totals[kind] += amount
            counts[kind] += 1

        return {
            "calculations": {
                "contract": key,
                "totals": {kind: str(total) for kind, total in totals.items()},
                "counts": counts,
                "scope": (
                    "Суммы рассчитаны по доступным документам. Статус оплаты "
                    "требует сверки со всеми платёжными документами."
                ),
            }
        }

    def generate(self, state):
        """Передать модели разрешённые фрагменты и принять ответ с ID цитат."""
        act_query = re.search(
            r"\bкакие\s+акты\s+относятся\s+к\s+(?:договор|контракт)\w*\s*(?:№|номер)?\s*(\d+)\b",
            state["question"],
            re.I,
        )
        if act_query and state["mode"] == "graph":
            acts = {}
            for item in state["evidence"]:
                fields = item["fields"]
                contract = fields.get("contract_number") or {}
                document = fields.get("document_number") or {}
                document_number = re.match(r"\d+", document.get("value", ""))
                if (
                    fields.get("kind") != "act"
                    or contract.get("value") != act_query.group(1)
                    or not document_number
                    or not contract.get("quote")
                    or not document.get("quote")
                    or contract.get("page") != item["page"]
                    or document.get("page") != item["page"]
                    or normalize(contract.get("quote", ""))
                    not in normalize(item["text"])
                    or normalize(document.get("quote", ""))
                    not in normalize(item["text"])
                ):
                    continue
                acts[item["document_id"]] = (document_number.group(), item["id"])

            if acts:
                ordered = sorted(acts.values(), key=lambda act: int(act[0]))
                numbers = ", ".join(f"№ {number}" for number, _ in ordered)
                return {
                    "answer": (
                        f"По доступным загруженным документам к договору "
                        f"№ {act_query.group(1)} относятся акты {numbers}."
                    ),
                    "citation_ids": [chunk_id for _, chunk_id in ordered],
                }

        sources = [
            {
                key: evidence[key]
                for key in ("id", "document_id", "filename", "page", "text")
            }
            for evidence in state["evidence"]
        ]
        raw = self.gateway.answer(
            state["question"],
            sources,
            state["calculations"],
        )

        try:
            payload = json.loads(raw)
            answer = payload["answer"]
            citations = payload["citation_ids"]
            if not isinstance(answer, str) or not isinstance(citations, list):
                raise ValueError
            return {"answer": answer, "citation_ids": citations}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            answer = raw

        # Старые адаптеры могли возвращать ID фрагментов внутри обычного текста.
        citation_ids = set(re.findall(r"\[([0-9a-fA-F-]{36})\]", answer))
        allowed_ids = {evidence["id"] for evidence in state["evidence"]}
        if citation_ids and citation_ids <= allowed_ids:
            answer = re.sub(
                r"\s*Источники:\s*(?:\[[0-9a-fA-F-]{36}\]\s*)+",
                "",
                answer,
                flags=re.I,
            )
            answer = re.sub(r"\s*\[[0-9a-fA-F-]{36}\]", "", answer)
            return {"answer": answer, "citation_ids": list(citation_ids)}
        return {"answer": answer}

    def output(self, state):
        """Оставить только подтверждённые цитаты и скрыть известные форматы ПДн."""
        return finalize_answer(
            state["answer"],
            state.get("citation_ids", []),
            state["evidence"],
            state.get("paths", []),
        )

    def invoke(self, user, question, mode="graph"):
        """Выполнить граф и проверить доступ к источникам после вызова модели."""
        result = self.graph.invoke(
            {
                "user": user,
                "question": question,
                "mode": mode,
                "stages": [],
            }
        )
        evidence = result.get("evidence", [])
        permitted = self.repo.evidence(
            [item["id"] for item in evidence],
            user,
        )
        if len(permitted) != len(evidence):
            return {
                "status": "unavailable",
                "answer": "Доступ к источникам изменился. Повторите запрос.",
                "citations": [],
                "paths": [],
                "stages": result["stages"],
            }

        return {
            "status": result["status"],
            "answer": result["answer"],
            "citations": [
                {
                    key: evidence_item[key]
                    for key in ("id", "document_id", "filename", "page", "text")
                }
                for evidence_item in evidence
            ],
            "paths": result.get("paths", []),
            "calculations": result.get("calculations", {}),
            "stages": result["stages"],
        }
