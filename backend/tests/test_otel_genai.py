"""Spans no formato das convenções semânticas GenAI do OpenTelemetry (invoke_agent / execute_tool).

Os nomes e atributos seguem semantic-conventions-genai (status "Development"), para que a trace de
um turno seja lida por Langfuse/Phoenix/qualquer backend OTel sem mapeamento próprio."""

import pytest

# O SDK do OpenTelemetry é opcional (TRACE_SINK desligado por padrão); sem ele, o teste é pulado.
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from app import observability  # noqa: E402
from tests.test_agentic_core import ANA, _conv, _world, msg, text, use  # noqa: E402


async def test_turno_com_tool_calling_emite_spans_genai(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "_tracer", provider.get_tracer("test"))
    _, _, service = await _world({"order_agent": [
        msg(use("get_order", order_id="PED-1001")),
        msg(text("O PED-1001 está a caminho.")),
    ]})
    await service.run_turn("onde está o pedido PED-1001? me conte os detalhes do envio", ANA, _conv())
    spans = {span.name: span for span in exporter.get_finished_spans()}
    agent = spans["invoke_agent order_agent"]
    assert agent.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert agent.attributes["gen_ai.agent.name"] == "order_agent"
    assert agent.attributes["gen_ai.provider.name"] == "anthropic"
    assert agent.attributes["gen_ai.conversation.id"]
    tool = spans["execute_tool get_order"]
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["gen_ai.tool.name"] == "get_order"
    assert tool.attributes["gen_ai.tool.call.id"].startswith("toolu_")
    # execute_tool é filho do invoke_agent: a trace mostra qual agente chamou qual tool.
    assert tool.parent.span_id == agent.context.span_id


def test_uso_do_modelo_vira_atributos_genai():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("chat claude-sonnet-5-5") as span:
        observability.record_llm_usage(span, {"model": "claude-sonnet-5-5", "status": "ok", "input_tokens": 100,
                                              "cache_read_tokens": 900, "cache_write_tokens": 0, "output_tokens": 50})
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 1000 and attrs["gen_ai.usage.output_tokens"] == 50
    assert attrs["gen_ai.response.model"] == "claude-sonnet-5-5" and "error.type" not in attrs
