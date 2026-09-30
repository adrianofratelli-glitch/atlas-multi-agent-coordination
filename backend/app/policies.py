import re
from typing import Any


# Status alcançáveis pelo caminho genérico de escrita.
APPROVED_STATUSES = {
    "processando",
    "enviado",
    "entregue",
    "reembolsado",
}
# Status que exigem uma regra de negócio antes da escrita, e por isso NÃO são alcançáveis
# por `safe_order_update`. Hoje só `troca_solicitada`: uma troca não pode ser efetivada sem
# consultar a cadeia de reposições (defeito de lote não se resolve trocando de novo).
#
# Manter isso fora da lista genérica é o que torna a regra impossível de esquecer: antes,
# a checagem morava dentro dos agentes, e bastava um caminho novo não lembrar dela para a
# quarta reposição do mesmo item defeituoso passar direto. Foi exatamente o que aconteceu —
# o gate estava só no warranty_agent, e "quero trocar o pedido PED-0000" ia para o
# order_agent sem passar por ele. Agora quem tentar pelo caminho comum recebe uma negação
# que diz o que usar, em vez de escrever silenciosamente.
#
# `reembolsado` de propósito NÃO entra aqui: reembolsar um item com defeito recorrente é o
# desfecho desejado, não o que se quer bloquear.
GUARDED_STATUSES = {"troca_solicitada"}
ORDER_RE = re.compile(r"^PED-\d{4,}$")


def safe_order_read_filter(tool_input: dict[str, Any], owner: str) -> dict[str, str]:
    """Reconstrói o filtro inteiro; nenhuma opção fornecida pelo modelo sobrevive."""
    order_id = str(tool_input.get("order_id", "")).upper().strip()
    if not ORDER_RE.fullmatch(order_id):
        raise ValueError("order_id inválido")
    return {"order_id": order_id, "owner_customer_key": owner}


class GuardedStatusError(ValueError):
    """Tentativa de escrever, pelo caminho genérico, um status que exige regra de negócio."""


def safe_order_update(
    tool_input: dict[str, Any], owner: str
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Caminho genérico de escrita de status. Reconstrói filtro e update no servidor."""
    query = safe_order_read_filter(tool_input, owner)
    status = str(tool_input.get("status", "")).lower().strip()
    if status in GUARDED_STATUSES:
        raise GuardedStatusError(
            f"'{status}' não pode ser escrito por safe_order_update: use "
            "app.replacement.apply_replacement, que consulta a cadeia de reposições antes."
        )
    if status not in APPROVED_STATUSES:
        raise ValueError("status não permitido")
    return query, {"$set": {"status": status}}


def guarded_order_update(
    tool_input: dict[str, Any], owner: str
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Escrita de um status guardado. **Uso exclusivo de `app.replacement`** — é ela que
    aplica a regra antes. Chamar isto de qualquer outro lugar contorna o gate."""
    query = safe_order_read_filter(tool_input, owner)
    status = str(tool_input.get("status", "")).lower().strip()
    if status not in GUARDED_STATUSES:
        raise ValueError("status não é guardado; use safe_order_update")
    return query, {"$set": {"status": status}}


def safe_invoice_filter(tool_input: dict[str, Any], owner: str) -> dict[str, str]:
    invoice_id = str(tool_input.get("invoice_id", "")).upper().strip()
    if not re.fullmatch(r"FAT-\d{4,}", invoice_id):
        raise ValueError("invoice_id inválido")
    return {"invoice_id": invoice_id, "owner_customer_key": owner}


def safe_shipment_filter(tool_input: dict[str, Any], owner: str) -> dict[str, str]:
    order_id = str(tool_input.get("order_id", "")).upper().strip()
    if not ORDER_RE.fullmatch(order_id):
        raise ValueError("order_id inválido")
    return {"order_id": order_id, "owner_customer_key": owner}


def public_document(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    return {key: value for key, value in document.items() if key != "_id"}

