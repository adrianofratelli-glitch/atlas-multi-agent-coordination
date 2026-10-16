"""Eventos da timeline ao vivo, sem tocar nos nós do grafo.

`TimelineEvent.model_post_init` chama `publish(evento)`; fora de um `capture(...)` é um no-op
(uma leitura de ContextVar). Dentro dele, cada evento vai para o callback NO MOMENTO em que é
criado — é assim que `POST /api/chat/stream` mostra o turno enquanto ele roda (antes a UI
esperava ~18 s e recebia a timeline inteira de uma vez). A ContextVar acompanha as tasks
criadas pelo turno (`asyncio.gather` copia o contexto), então o fan-out paralelo também publica.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

_sink: ContextVar[Callable[[Any], None] | None] = ContextVar("timeline_sink", default=None)


def publish(event: Any) -> None:
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink(event)
    except Exception:  # noqa: BLE001 — streaming nunca derruba o turno
        pass


@contextmanager
def capture(callback: Callable[[Any], None]):
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)
