from app.router import cheap_route


RULES = [
    {"intent": "pedido", "keywords": ["pedido", "PED-"], "target_agent": "order_agent", "priority": 100},
    {"intent": "defeito", "keywords": ["defeito"], "target_agent": "support_agent", "priority": 120},
    {"intent": "similar", "keywords": ["parecido"], "target_agent": "product_agent", "priority": 80},
]


def test_cheap_router_resolves_obvious_order_without_llm():
    decision = cheap_route("onde está meu pedido PED-1001?", RULES)
    assert decision is not None
    assert decision.target_agent == "order_agent"
    assert decision.source == "rules"


def test_cheap_router_prioritizes_diagnosis_before_recommendation():
    decision = cheap_route("meu fone tem defeito e quero um parecido", RULES)
    assert decision is not None
    assert decision.target_agent == "support_agent"


def test_router_is_accent_insensitive():
    rules = [{"intent": "billing", "keywords": ["cobrança"], "target_agent": "billing_agent", "priority": 10}]
    assert cheap_route("quero entender a cobranca", rules).target_agent == "billing_agent"
