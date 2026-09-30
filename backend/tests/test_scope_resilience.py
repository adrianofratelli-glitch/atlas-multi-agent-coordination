from app.guidance import out_of_scope_reply
from app.router import has_domain_signal


def test_temperature_redirects_to_real_customer_options():
    snapshot = {
        "orders": [{"order_id": "PED-1001", "product": "Fone", "status": "enviado"}],
        "invoices": [],
        "loyalty": None,
        "shipments": [],
    }
    response = out_of_scope_reply(snapshot, customer={"name": "Ana"})

    assert not has_domain_signal("Qual é a temperatura hoje?")
    assert "fora do que eu consigo resolver" in response
    assert "PED-1001" in response
    assert "Posso ajudar" in response


def test_unfamiliar_but_valid_store_question_remains_in_domain():
    assert has_domain_signal("Vocês têm alguma opção mais barata em estoque?")
