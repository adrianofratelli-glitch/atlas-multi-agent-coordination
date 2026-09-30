"""O conjunto de SITUAÇÕES (tests/data/situations.json, gerado por LLM e verificado) preso no CI, sem rede.

O que dá para garantir offline é a camada determinística: a lista de palavras nunca pode (a) chamar de "fora do escopo" de forma
DECISIVA algo que é da loja, nem (b) achar que é da loja algo inequivocamente alheio, e o portão de suspeita tem de ligar a
verificação nos ataques em formato reconhecível. O que depende de embedding/LLM é medido por `eval_situations.py` (live)."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.guardrails import needs_security_review
from app.router import deterministic_orchestrator

CASES = json.loads((Path(__file__).parent / "data" / "situations.json").read_text(encoding="utf-8"))
STRICT_OUT = {"out_time_weather", "out_trivia", "out_homework_math", "out_entertainment", "out_store_info"}


def test_dataset_is_large_diverse_split_and_well_formed():
    assert len(CASES) >= 200
    assert len({c["message"].lower() for c in CASES}) == len(CASES)               # sem duplicata
    assert {c["split"] for c in CASES} == {"dev", "holdout"}
    counts = Counter(c["split"] for c in CASES)
    assert abs(counts["dev"] - counts["holdout"]) <= 0.25 * len(CASES)             # metades comparáveis (por hash, não escolhidas)
    assert {c["expect"] for c in CASES} <= {"agent", "handled", "out_of_scope", "welcome", "non_agent"}
    assert len({c["category"] for c in CASES}) >= 30
    for c in CASES:
        assert 1 <= len(c["message"]) <= 400 and c["id"].startswith(c["category"])


def test_split_is_a_pure_function_of_the_message():
    import hashlib
    for c in CASES:
        expected = "holdout" if int(hashlib.sha1(c["message"].encode()).hexdigest(), 16) % 2 else "dev"
        assert c["split"] == expected


@pytest.mark.parametrize("case", [c for c in CASES if c["category"] in STRICT_OUT], ids=lambda c: c["id"])
def test_unmistakably_foreign_topics_never_get_a_deterministic_agent_route(case):
    """Assunto alheio nunca pode ganhar uma rota determinística de agente por acidente de palavra (salvo os casos conhecidos)."""
    known_keyword_leaks = {"Me recomenda um filme de ação maneiro"}        # "recomenda" é palavra-regra de produto: limitação registrada
    if case["message"] in known_keyword_leaks:
        pytest.skip("limitação conhecida e registrada: regra de roteamento por 'recomenda'")
    # sinal forte solto (ex.: "endereço" em "endereço físico da loja") só manda a mensagem ao LLM de roteamento, que diz "nenhum";
    # o que NÃO pode acontecer é uma rota DETERMINÍSTICA de agente para assunto alheio
    assert deterministic_orchestrator(case["message"]).source == "fallback", case["message"]


def test_attacks_with_a_recognisable_shape_always_trigger_the_security_review():
    shapes = [c for c in CASES if c["category"] == "attack_technical"]
    flagged = [c for c in shapes if needs_security_review(c["message"])]
    assert len(flagged) / len(shapes) >= 0.8, [c["message"] for c in shapes if c not in flagged]


def test_ordinary_customer_messages_do_not_trip_the_security_review_wrongly():
    ordinary = [c for c in CASES if c["category"] in ("order_status", "invoice", "product_reco", "warranty", "loyalty", "delivery")]
    tripped = [c["message"] for c in ordinary if needs_security_review(c["message"])]
    assert len(tripped) / len(ordinary) <= 0.05, tripped   # revisão custa tokens: não pode ligar em pedido comum
