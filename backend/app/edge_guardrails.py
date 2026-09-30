"""Guardrails de BORDA usando o pacote comum (`guardrails` do _shared), opt-in por `GUARDRAILS_EDGE=1`.

Não substitui `app/guardrails.py` (denylist + classificador + escopo), que é o guardrail de
DOMÍNIO deste PoV. Aqui é só a borda do processo:

  * `mask_log` — nenhum log estruturado sai com PII, nem no caminho de erro. O turno já mascara
    a mensagem (`app/security.py:mask_pii`), mas o log carrega campos que ninguém revisou.
  * `validate_response` — a resposta que vai ao cliente é validada contra o schema antes de sair.
    `max_repairs=0` de propósito: reparo por LLM é opt-in explícito no _shared e custaria token
    no caminho de erro, que é justamente onde não se quer mais uma chamada ao provedor.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("multi-agent-poc.edge")


def enabled() -> bool:
    return os.getenv("GUARDRAILS_EDGE", "").strip().lower() in {"1", "true", "yes", "on"}


def mask_log(fields: dict) -> dict:
    """Mascara PII em todo valor de texto de um evento de log."""
    if not enabled():
        return fields
    try:
        from guardrails import mask_pii
    except ImportError:
        return fields
    return {key: (mask_pii(value).text if isinstance(value, str) else value)
            for key, value in fields.items()}


def validate_response(model_cls, response) -> tuple[bool, str]:
    """Valida a resposta serializada contra o próprio schema. Nunca derruba o turno."""
    if not enabled():
        return True, "skipped"
    try:
        from guardrails import validate_output
    except ImportError:
        return True, "unavailable"
    result = validate_output(model_cls, response.model_dump_json(), max_repairs=0)
    if not result.ok:
        log.warning("resposta fora do schema: %s", result.reason)
    return bool(result.ok), result.reason
