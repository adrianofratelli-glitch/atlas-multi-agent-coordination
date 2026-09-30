import logging
from functools import lru_cache
from typing import Any

from .config import Settings, get_settings

logger = logging.getLogger("app.langfuse_client")


class _NoopLangfuse:
    """Sem chave configurada (ou Langfuse fora do ar): instrumentação vira no-op, igual ao padrão de
    fallback do resto do repo (LLMGateway sem api key, guardrail sem client) — DEMO_MODE/CI não
    dependem de credencial externa, e uma demo ao vivo nunca quebra por causa de observability."""

    def trace(self, **kwargs) -> "_NoopTrace":
        return _NoopTrace()


class _NoopTrace:
    def span(self, **kwargs) -> None:
        return None

    def generation(self, **kwargs) -> None:
        return None

    def update(self, **kwargs) -> None:
        return None

    def get_trace_url(self) -> None:
        return None


@lru_cache
def get_langfuse(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    if not settings.langfuse_enabled:
        return _NoopLangfuse()
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        # auth_check roda uma vez (função é @lru_cache) — sem isso um Langfuse fora do ar não
        # impede a criação de traces (o SDK é fire-and-forget): o turno seguiria normal, mas a
        # UI mostraria um link "Ver no Langfuse" que dá 404 no meio de uma demo ao vivo. Aqui a
        # feature inteira vira no-op em vez de visível-e-quebrada.
        if not client.auth_check():
            raise RuntimeError("Langfuse auth_check falhou")
        return client
    except Exception:  # noqa: BLE001 — tracing nunca derruba o turno
        logger.warning("Langfuse indisponível/mal configurado; tracing desligado para este processo")
        return _NoopLangfuse()


def build_turn_trace(
    *,
    conversation_id: str,
    customer_key: str,
    message: str,
    response: str,
    timeline: list,
    active_agent: str,
    usage: dict | None = None,
    llm_calls: list[dict] | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Uma trace por turno cobrindo a timeline INTEIRA — roteamento, decisão de cache, cada hop de
    agente (com handoff) e os guardrails — em vez de traces desconexas por decisão isolada. É o que
    deixa visível no Langfuse exatamente o que a UI já mostra na Timeline: a cadeia de coordenação
    entre os 8 agentes, não só se o cache bateu ou não.

    `message`/`response` chegam aqui já mascarados pelo guardrail de PII (mesma garantia do
    restante do pipeline) — nunca dado cru do cliente.
    """
    if settings is not None and not settings.langfuse_enabled:
        return None
    client = get_langfuse()
    trace = client.trace(
        name="multiagent.turn", session_id=conversation_id, user_id=customer_key,
        input=message, metadata={"active_agent": active_agent, "usage": usage or {}},
    )
    for event in timeline:
        title = getattr(event, "title", None) or event.get("title") if isinstance(event, dict) else event.title
        category = getattr(event, "category", None) if not isinstance(event, dict) else event.get("category")
        agent = getattr(event, "agent", None) if not isinstance(event, dict) else event.get("agent")
        result = getattr(event, "result", None) if not isinstance(event, dict) else event.get("result")
        event_filter = getattr(event, "filter", None) if not isinstance(event, dict) else event.get("filter")
        duration_ms = getattr(event, "duration_ms", 0) if not isinstance(event, dict) else event.get("duration_ms", 0)
        name = f"{category}.{agent}" if agent else category
        replayed = event.get("replayed", False) if isinstance(event, dict) else event.replayed
        trace.span(
            name=name, input=event_filter, output=result,
            metadata={"title": title, "latency_ms": duration_ms, "replayed": replayed},
        )
    # Agent activity is not necessarily an LLM call, particularly on cache replay.
    from datetime import datetime, timedelta
    for call in llm_calls or []:
        if call.get("status") == "circuit_open":
            continue
        start = datetime.fromisoformat(call["started_at"])
        trace.generation(
            name=f"llm.{call['agent']}", model=call["model"],
            start_time=start, end_time=start + timedelta(milliseconds=call["latency_ms"]),
            usage={"input": sum(call.get(k, 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")),
                   "output": call.get("output_tokens", 0)} if call.get("usage_known") else None,
            metadata=call,
        )
    trace.update(output=response)
    try:
        return trace.get_trace_url()
    except Exception:  # noqa: BLE001
        return None
