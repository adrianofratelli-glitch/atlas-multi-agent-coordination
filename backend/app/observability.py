"""Tracing distribuído opt-in, em cima de `tracing` do _shared (pov-shared 0.1.4).

Default `TRACE_SINK=off`: `setup_tracing()` não importa OpenTelemetry, não cria spans e a
demo roda exatamente como antes. Com um sink ligado, `TRACE_MASK_PII` é FORÇADO a 1 — os
spans automáticos do OpenInference capturam prompt e resposta inteiros, e aqui trafega nome,
pedido e fatura de cliente. (Achado do FinScope; vale mesmo sem PII "conhecida" no dataset.)

Um span por turno, por decisão de roteamento, por agente, por handoff e por tool, todos com
`conversation_id` — é o que permite perguntar "quem travou e onde" (scripts/trace_query.py).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from time import perf_counter

log = logging.getLogger("multi-agent-poc.tracing")

_tracer = None
_sink = "off"


def sink() -> str:
    return _sink


def active() -> bool:
    return _sink != "off"


def setup_tracing(service_name: str = "multiagente-atendimento") -> str:
    """Liga o tracing conforme TRACE_SINK. Fail-open: erro aqui nunca impede o app de subir."""
    global _tracer, _sink
    wanted = os.getenv("TRACE_SINK", "off").strip().lower() or "off"
    if wanted == "off":
        _sink, _tracer = "off", None
        return "off"
    # Sem exceção: sink ligado => conteúdo mascarado. Ler ANTES de init_tracing.
    os.environ["TRACE_MASK_PII"] = "1"
    if wanted == "atlas" and not os.getenv("TRACE_MONGODB_URI") and not os.getenv("MONGODB_URI"):
        # O _shared lê só o ambiente; a URI deste PoV vive no .env carregado pelo pydantic.
        from .config import get_settings

        if get_settings().mongodb_uri:
            os.environ["TRACE_MONGODB_URI"] = get_settings().mongodb_uri
    try:
        from tracing import init_tracing  # pov-shared
        from opentelemetry import trace

        _sink = init_tracing(service_name)
        _tracer = trace.get_tracer(service_name)
    except Exception as exc:  # noqa: BLE001 — observabilidade nunca derruba o serviço
        log.warning("tracing desligado: %s: %s", type(exc).__name__, exc)
        _sink, _tracer = "off", None
    return _sink


class _NoSpan:
    def set_attribute(self, *_args, **_kwargs) -> None:
        return None

    def add_event(self, *_args, **_kwargs) -> None:
        return None

    def record_exception(self, *_args, **_kwargs) -> None:
        return None


_NO_SPAN = _NoSpan()


@contextmanager
def span(name: str, **attributes):
    """Span com latência medida. Vira um no-op quando o tracing está desligado."""
    if _tracer is None:
        yield _NO_SPAN
        return
    started = perf_counter()
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:  # noqa: BLE001 — registra e repassa
            current.record_exception(exc)
            current.set_attribute("error_type", type(exc).__name__)
            raise
        finally:
            current.set_attribute("latency_ms", round((perf_counter() - started) * 1000, 2))


def record_llm_usage(current, record: dict) -> None:
    """Tokens e custo estimado de UMA chamada, no span dela."""
    if current is _NO_SPAN or current is None:
        return
    for key in ("model", "protocol", "provider", "status", "attempt", "fallback"):
        if record.get(key) is not None:
            current.set_attribute(f"llm.{key}", record[key])
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        if isinstance(record.get(key), int):
            current.set_attribute(f"llm.{key}", record[key])
    if isinstance(record.get("estimated_cost_usd"), (int, float)):
        current.set_attribute("llm.estimated_cost_usd", float(record["estimated_cost_usd"]))
