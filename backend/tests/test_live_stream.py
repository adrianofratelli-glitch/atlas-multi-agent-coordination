import asyncio
import json

from app.main import handoff_event_stream


class ConnectedRequest:
    async def is_disconnected(self):
        return False


class OneHandoffStore:
    async def watch_handoffs(self, customer_key):
        yield {"customer_key": customer_key, "from_agent": "support_agent", "to_agent": "product_agent"}


class IdleStore:
    async def watch_handoffs(self, _customer_key):
        await asyncio.Event().wait()
        yield  # pragma: no cover - keeps this function an async generator


async def test_sse_confirms_connection_before_first_handoff_and_then_emits_data():
    stream = handoff_event_stream(ConnectedRequest(), OneHandoffStore(), "ana")
    assert await anext(stream) == ": connected\n\n"

    chunk = await anext(stream)
    assert chunk.startswith("data: ")
    assert json.loads(chunk.removeprefix("data: ").strip())["customer_key"] == "ana"
    await stream.aclose()


async def test_sse_sends_heartbeat_while_change_stream_is_idle():
    stream = handoff_event_stream(ConnectedRequest(), IdleStore(), "ana", heartbeat_seconds=0.001)
    assert await anext(stream) == ": connected\n\n"
    assert await anext(stream) == ": keepalive\n\n"
    await stream.aclose()
