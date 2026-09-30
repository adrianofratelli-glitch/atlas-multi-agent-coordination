import pytest

from app.policies import (GuardedStatusError, guarded_order_update, safe_invoice_filter,
                          safe_order_read_filter, safe_order_update)


def test_order_read_rebuilds_filter_with_owner():
    malicious = {"order_id": "PED-1001", "owner_customer_key": "bruno", "$where": "true"}
    assert safe_order_read_filter(malicious, "ana") == {
        "order_id": "PED-1001",
        "owner_customer_key": "ana",
    }


def test_order_write_strips_upsert_and_unapproved_fields():
    query, update = safe_order_update(
        {"order_id": "PED-1001", "status": "reembolsado", "upsert": True, "$inc": {"amount": 99}},
        "ana",
    )
    assert query == {"order_id": "PED-1001", "owner_customer_key": "ana"}
    assert update == {"$set": {"status": "reembolsado"}}


@pytest.mark.parametrize("status", ["cancelado_admin", "fraude", "", "ENTREGUE_AGORA"])
def test_order_write_rejects_unknown_status(status):
    with pytest.raises(ValueError):
        safe_order_update({"order_id": "PED-1001", "status": status}, "ana")


def test_invoice_filter_never_accepts_foreign_owner():
    assert safe_invoice_filter({"invoice_id": "FAT-1001", "owner_customer_key": "bruno"}, "ana") == {
        "invoice_id": "FAT-1001",
        "owner_customer_key": "ana",
    }



def test_troca_nao_e_alcancavel_pelo_caminho_generico_de_escrita():
    """A barreira que impede a regra de ser esquecida.

    Antes, a checagem de defeito recorrente morava dentro dos agentes: bastava um caminho
    novo não lembrar dela para a quarta reposição do mesmo item passar direto — foi o que
    aconteceu com "quero trocar o pedido PED-0000", que ia para o order_agent sem passar
    pelo gate do warranty_agent. Agora o caminho comum recusa, e a mensagem diz o que usar.
    """
    with pytest.raises(GuardedStatusError) as exc:
        safe_order_update({"order_id": "PED-3001", "status": "troca_solicitada"}, "carla")
    assert "apply_replacement" in str(exc.value)


def test_caminho_guardado_aceita_a_troca_e_recusa_o_resto():
    query, update = guarded_order_update({"order_id": "PED-3001", "status": "troca_solicitada"}, "carla")
    assert query == {"order_id": "PED-3001", "owner_customer_key": "carla"}
    assert update == {"$set": {"status": "troca_solicitada"}}

    # O caminho guardado não vira um bypass genérico: só serve para status guardados.
    with pytest.raises(ValueError):
        guarded_order_update({"order_id": "PED-3001", "status": "reembolsado"}, "carla")


def test_reembolso_continua_no_caminho_generico():
    # Reembolsar um item com defeito recorrente é o desfecho desejado, não o bloqueado.
    _, update = safe_order_update({"order_id": "PED-3001", "status": "reembolsado"}, "carla")
    assert update == {"$set": {"status": "reembolsado"}}
