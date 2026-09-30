"""Smoke test contra a API no ar. Uso: python tests/smoke.py [URL]."""

import random
import sys

import httpx


BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8031"

# A checagem de handoff SÓ é válida num turno que rodou de verdade: num cache HIT a resposta
# é replay e nenhum handoff novo é persistido. Como o próprio smoke deixa a mensagem no
# cache (TTL de 60 min), rodar duas vezes seguidas fazia a segunda falhar em "handoff
# persistido" — falso negativo, não regressão. Um nonce não resolveria: duas frases que
# diferem por um token têm embedding praticamente idêntico e continuam batendo no cache.
# Por isso as variantes falam de PRODUTOS diferentes: são pontos distintos no espaço
# vetorial, então uma variante ainda não usada nesta janela dá MISS de verdade.
HANDOFF_PROBES = [
    "meu fone chegou com defeito, quero um parecido mais barato",
    "meu teclado chegou com defeito, quero um parecido mais barato",
    "meu monitor chegou com defeito, quero um parecido mais barato",
    "minha webcam chegou com defeito, quero uma parecida mais barata",
    "meu mouse chegou com defeito, quero um parecido mais barato",
    "meu carregador chegou com defeito, quero um parecido mais barato",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"✓ {message}")


def token(client: httpx.Client, customer: str) -> str:
    response = client.post("/api/auth/token", json={"customer_key": customer})
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=130) as client:
        health = client.get("/api/health")
        check(health.status_code == 200 and health.json()["status"] == "ok", "health ok")

        ana = token(client, "ana")
        headers = {"Authorization": f"Bearer {ana}"}
        simple = client.post("/api/chat", headers=headers, json={"message": "onde está meu pedido PED-1001?"})
        simple.raise_for_status()
        body = simple.json()
        check(body["active_agent"] == "order_agent", "pedido roteado sem LLM")
        check("PED-1001" in body["response"] and "enviado" in body["response"], "pedido usa dado real do seed")

        chain = None
        for probe in random.sample(HANDOFF_PROBES, len(HANDOFF_PROBES)):
            handoff = client.post("/api/chat", headers=headers, json={"message": probe})
            handoff.raise_for_status()
            chain = handoff.json()
            if not chain.get("cache_hit"):
                break
        # Todas as variantes em cache é sinal real (cache não expirando/escopo errado), não ruído.
        check(chain is not None and not chain.get("cache_hit"),
              "turno de handoff rodou sem cache (variante inédita encontrada)")
        check(chain["active_agent"] == "product_agent", "handoff suporte → produtos")
        audit = client.get("/api/handoffs", headers=headers, params={"conversation_id": chain["conversation_id"]})
        check(len(audit.json()) == 1 and audit.json()[0]["to_agent"] == "product_agent", "handoff persistido")

        bruno = token(client, "bruno")
        foreign = client.post("/api/chat", headers={"Authorization": f"Bearer {bruno}"}, json={"message": "onde está meu pedido PED-1001?"})
        foreign.raise_for_status()
        check("Não encontrei" in foreign.json()["response"], "isolamento entre clientes")

        repeated = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "onde está meu pedido PED-1001?",
                "conversation_id": body["conversation_id"],
            },
        )
        repeated.raise_for_status()
        check(
            repeated.json()["cache_hit"] is True
            and repeated.json()["cache_source"] == "curto_prazo",
            "cache personalizado reutilizado somente na mesma conversa",
        )

        new_session = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "onde está meu pedido PED-1001?"},
        )
        new_session.raise_for_status()
        check(
            new_session.json()["cache_hit"] is False,
            "cache personalizado não vaza para uma conversa nova",
        )

        metrics = client.get("/api/metrics", headers=headers)
        check(metrics.status_code == 200 and "counters" in metrics.json(), "métricas disponíveis")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"✗ smoke falhou: {exc}", file=sys.stderr)
        raise SystemExit(1)
