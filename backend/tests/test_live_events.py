"""B6: timeline ao vivo — TimelineEvent publica ao nascer, só dentro de capture(), inclusive em fan-out."""

import asyncio

from app import live_events
from app.models import TimelineEvent


def _ev(title):
    return TimelineEvent(category="agent", title=title)


def test_no_sink_is_noop():
    _ev("fora de captura")   # não levanta, não publica


def test_capture_receives_events_in_creation_order_and_across_tasks():
    seen = []

    async def worker(name):
        await asyncio.sleep(0)
        _ev(name)

    async def go():
        with live_events.capture(lambda e: seen.append(e.title)):
            _ev("1-roteamento")
            await asyncio.gather(worker("2a-order"), worker("2b-billing"))   # fan-out copia o contexto
            _ev("3-fim")
        _ev("depois")   # fora da captura
    asyncio.run(go())
    assert seen[0] == "1-roteamento" and seen[-1] == "3-fim"
    assert set(seen[1:3]) == {"2a-order", "2b-billing"} and "depois" not in seen


def test_broken_sink_never_breaks_the_turn():
    def boom(_):
        raise RuntimeError("cliente SSE caiu")
    with live_events.capture(boom):
        assert _ev("segue").title == "segue"


def test_concurrent_turns_do_not_mix_streams():
    a, b = [], []

    async def turn(sink, tag):
        with live_events.capture(sink.append):
            for i in range(5):
                _ev(f"{tag}{i}")
                await asyncio.sleep(0)

    async def go():
        await asyncio.gather(turn(a, "a"), turn(b, "b"))
    asyncio.run(go())
    assert [e.title for e in a] == [f"a{i}" for i in range(5)]
    assert [e.title for e in b] == [f"b{i}" for i in range(5)]
