"""Datas do seed são relativas ao dia em que ele roda: a demo não envelhece.

Com datas fixas (julho/2026), em novembro a logística dizia "116 dias além da data prevista". Estes testes
fixam os VEREDITOS que o roteiro depende, para qualquer dia do seed: previsões vencidas, garantia, faturas,
ordem da cadeia de trocas.
"""

from datetime import date, timedelta

import pytest

from app import clock
from app.seed_data import PRODUCTS, WARRANTY_POLICIES, seed_documents

DAYS = [date(2026, 11, 12), date(2027, 1, 1), date(2028, 2, 29), date(2031, 6, 30)]


def _day(value: str) -> date:
    return date.fromisoformat(value)


@pytest.mark.parametrize("today", DAYS)
def test_no_business_event_happens_in_the_future(today):
    docs = seed_documents(today)
    for order in docs["orders"]:
        dates = [_day(item["at"]) for item in order["timeline"]]
        assert dates == sorted(dates), order["order_id"]          # timeline em ordem
        assert all(day < today for day in dates), order["order_id"]


@pytest.mark.parametrize("today", DAYS)
def test_undelivered_shipments_are_overdue_and_ped_1003_by_three_days(today):
    shipments = {s["order_id"]: s for s in seed_documents(today)["shipments"]}
    pending = {key: s for key, s in shipments.items() if s["current_location"] != "Entregue"}
    assert set(pending) == {"PED-1003", "PED-2001", "PED-3001", "PED-4001"}
    assert all(_day(s["estimated_delivery"]) < today for s in pending.values())   # dispara busca web (ADR-005)
    assert _day(shipments["PED-1003"]["estimated_delivery"]) == today - timedelta(days=3)
    # "cadê meu pedido?" sem id: a mais recente por previsão (sort do logistics_agent) é a atrasada
    ana = [s for s in shipments.values() if s["owner_customer_key"] == "ana"]
    assert max(ana, key=lambda s: s["estimated_delivery"])["order_id"] == "PED-1003"


@pytest.mark.parametrize("today", DAYS)
def test_ped_1001_arrived_and_is_inside_the_kb003_return_window(today):
    """O roteiro diz "chegou com defeito": entregue, e a troca em até 7 dias do KB-003 ainda vale."""
    docs = seed_documents(today)
    order = next(o for o in docs["orders"] if o["order_id"] == "PED-1001")
    shipment = next(s for s in docs["shipments"] if s["order_id"] == "PED-1001")
    assert order["status"] == "entregue" and shipment["current_location"] == "Entregue"
    delivered = _day(next(t["at"] for t in order["timeline"] if t["status"] == "entregue"))
    assert 0 < (today - delivered).days <= 7
    assert _day(shipment["estimated_delivery"]) <= today


@pytest.mark.parametrize("today", DAYS)
def test_every_seeded_order_is_still_under_warranty(today):
    category = {p["name"]: p["category"] for p in PRODUCTS}
    months = {p["category"]: p["months"] for p in WARRANTY_POLICIES}
    for order in seed_documents(today)["orders"]:
        timeline = order["timeline"]
        purchased = _day(next((t["at"] for t in timeline if t["status"] == "criado"), timeline[0]["at"]))
        expires = clock.add_months(purchased, months[category[order["product"]]])
        assert expires >= today, order["order_id"]


@pytest.mark.parametrize("today", DAYS)
def test_open_invoices_fall_due_later_and_the_paid_one_already_did(today):
    invoices = {i["invoice_id"]: i for i in seed_documents(today)["invoices"]}
    for invoice in invoices.values():
        due = _day(invoice["due_date"])
        assert (due > today) if invoice["status"] == "aberta" else (due < today), invoice["invoice_id"]


@pytest.mark.parametrize("today", DAYS)
def test_replacement_chain_links_happen_in_order(today):
    orders = {o["order_id"]: o for o in seed_documents(today)["orders"]}
    for order in orders.values():
        nxt = order.get("replacement_order_id")
        if nxt:
            asked = _day(next(t["at"] for t in order["timeline"] if t["status"] == "troca_solicitada"))
            assert asked <= _day(orders[nxt]["timeline"][0]["at"]), (order["order_id"], nxt)


def test_default_anchor_is_the_turn_clock(monkeypatch):
    frozen = clock.local_now().replace(year=2030, month=3, day=15)
    monkeypatch.setattr(clock, "local_now", lambda: frozen)
    shipments = {s["order_id"]: s for s in seed_documents()["shipments"]}
    assert shipments["PED-1003"]["estimated_delivery"] == "2030-03-12"
