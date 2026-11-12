"""ADR-005 no caminho de tool calling: o SERVIDOR busca na web ao ler entrega vencida (não depende do modelo)."""

from app import agents as agents_module, tools as tools_module
from app.models import TimelineEvent
from tests.test_agentic_core import _ctx, _world


async def test_get_shipment_vencido_dispara_busca_do_servidor_e_taint(monkeypatch):
    calls = []

    async def fake_news(store, customer, clean, cfg):
        calls.append(clean["order_id"])
        return "atraso regional", [TimelineEvent(category="web", title="web", op="external",
                                                 result={"sources": ["https://exemplo.org/aviso"]})]
    monkeypatch.setattr(agents_module, "carrier_news", fake_news)
    store, _, _ = await _world()
    ctx = _ctx(store, "cadê o PED-1003?", turn_context={"web_cfg": {"enabled": True}})
    result = await tools_module.execute(ctx, "get_shipment", {"order_id": "PED-1003"})
    assert calls == ["PED-1003"] and ctx.tainted
    assert result.content["noticias_web"]["fontes"] == ["https://exemplo.org/aviso"]
    again = await tools_module.execute(ctx, "search_carrier_news", {"order_id": "PED-1003"})
    assert calls == ["PED-1003"] and again.content["executada"]          # uma busca por turno
    write = await tools_module.execute(ctx, "request_delivery_reschedule", {"order_id": "PED-1003"})
    assert write.is_error                                                # taint: nada escreve depois da web


async def test_entrega_no_prazo_ou_ferramenta_desligada_nao_busca(monkeypatch):
    calls = []

    async def fake_news(*args):
        calls.append(1)
        return "", []
    monkeypatch.setattr(agents_module, "carrier_news", fake_news)
    store, _, _ = await _world()
    await tools_module.execute(_ctx(store, "cadê o PED-1001?", turn_context={"web_cfg": {"enabled": True}}),
                               "get_shipment", {"order_id": "PED-1001"})      # entregue
    await tools_module.execute(_ctx(store, "cadê o PED-1003?"), "get_shipment", {"order_id": "PED-1003"})  # sem cfg
    assert calls == []


async def test_pedido_de_reagendamento_tem_prioridade_e_nao_busca(monkeypatch):
    calls = []

    async def fake_news(*args):
        calls.append(1)
        return "", []
    monkeypatch.setattr(agents_module, "carrier_news", fake_news)
    store, _, _ = await _world()
    ctx = _ctx(store, "quero reagendar a entrega do PED-1003", turn_context={"web_cfg": {"enabled": True}})
    await tools_module.execute(ctx, "get_shipment", {"order_id": "PED-1003"})
    write = await tools_module.execute(ctx, "request_delivery_reschedule", {"order_id": "PED-1003"})
    assert calls == [] and not write.is_error
