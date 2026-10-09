"""Создание трассировки для этапов обработки запросов."""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    ConsoleSpanExporter,
)


def build_tracing(endpoint: str = ""):
    """Экспортировать спаны в OTLP, если адрес задан; иначе выводить их в логи."""
    provider = TracerProvider(resource=Resource.create({"service.name": "accmind-mvp"}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider
