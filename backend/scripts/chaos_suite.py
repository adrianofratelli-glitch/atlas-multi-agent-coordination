"""Bateria de caos: o PoV tentando se quebrar sozinho.

Cada cenário injeta UMA falha controlada no caminho real (`app/chaos.py`) e verifica uma
afirmação concreta sobre o que "resiliente" significa ali. Sem assertion o cenário não prova
nada, então cada um carrega a sua no campo `assertion`.

    cd backend && CHAOS=1 ../.venv/bin/python scripts/chaos_suite.py            # bateria toda
    cd backend && CHAOS=1 ../.venv/bin/python scripts/chaos_suite.py agent_hang # um cenário

Os mesmos cenários rodam como regressão permanente em `tests/test_chaos.py` (CHAOS=1).
Tudo offline (DEMO_MODE + cliente Anthropic falso), exceto `crash_resume`, que precisa de
Atlas real e se declara `skipped` quando não há cluster.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chaos, resilience, web_search  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import DataStore  # noqa: E402
from app.llm import LLMGateway  # noqa: E402
from app.orchestration import OrchestrationService  # noqa: E402
from seed import seed  # noqa: E402

ANA = {"customer_key": "ana", "area": "varejo", "name": "Ana", "plan": "premium"}


# ---------------------------------------------------------------- mundo de teste


class _FakeUsage:
    input_tokens = 40
    output_tokens = 20
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeBlock:
    type = "text"
    text = "Resposta sintética do provedor falso."


class _FakeResponse:
    usage = _FakeUsage()
    content = [_FakeBlock()]
    stop_reason = "end_turn"


class _FakeMessages:
    async def create(self, **_kwargs):
        return _FakeResponse()


class _FakeAnthropic:
    """Provedor falso: deixa o cenário decidir a falha em vez de depender da rede."""

    messages = _FakeMessages()


@contextlib.contextmanager
def env(**values):
    """Aplica variáveis de ambiente só durante o cenário."""
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    chaos.reset()
    resilience.reset_circuits()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        chaos.reset()
        resilience.reset_circuits()


async def world(*, with_llm: bool = False):
    settings = Settings(demo_mode=not with_llm, mongodb_uri="",
                        anthropic_api_key="chave-de-teste-nao-usada" if with_llm else "")
    store = DataStore(settings)
    await store.connect()
    await seed(store, create_indexes=False)
    llm = LLMGateway(settings)
    if with_llm:
        llm.anthropic_client = _FakeAnthropic()
        llm.client = llm.anthropic_client
    return store, OrchestrationService(store, llm, global_budget=20000)


# ---------------------------------------------------------------- resultado


@dataclass
class Verdict:
    name: str
    assertion: str
    passed: bool
    detail: str = ""
    elapsed_s: float = 0.0
    skipped: bool = False
    findings: list[str] = field(default_factory=list)

    def line(self) -> str:
        status = "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL")
        return f"[{status}] {self.name} ({self.elapsed_s:.2f}s) — {self.detail}"


def _degraded(response) -> bool:
    return "não consegui concluir a etapa" in response.response.lower()


# ---------------------------------------------------------------- cenários


async def scenario_tool_timeout() -> Verdict:
    """Uma tool (consulta ao Mongo) para de responder no meio do turno."""
    assertion = ("turno termina em < 5s com resposta de degradação explícita, sem exceção; "
                 "a timeline registra o agente degradado")
    with env(CHAOS=1, CHAOS_SCENARIO="timeout", CHAOS_TARGET="tool:orders", CHAOS_DELAY=20,
             AGENT_TIMEOUT_SECONDS=1, TOOL_TIMEOUT_SECONDS=1):
        _, service = await world()
        started = perf_counter()
        response = await service.run_turn("onde está meu pedido?", ANA, None)
        elapsed = perf_counter() - started
    degraded_events = [event for event in response.timeline if "degradado" in event.title.lower()]
    ok = _degraded(response) and elapsed < 5 and bool(degraded_events)
    return Verdict("tool_timeout", assertion, ok,
                   f"elapsed={elapsed:.2f}s degradado={_degraded(response)} eventos={len(degraded_events)}",
                   elapsed)


async def scenario_agent_hang() -> Verdict:
    """Um agente trava e nunca retorna — o teto do supervisor tem de interromper de verdade."""
    assertion = ("o supervisor interrompe em ~AGENT_TIMEOUT_SECONDS (< 5s) mesmo com o agente "
                 "pendurado por 60s, e responde com estado explícito")
    with env(CHAOS=1, CHAOS_SCENARIO="hang", CHAOS_TARGET="agent:", CHAOS_DELAY=60,
             AGENT_TIMEOUT_SECONDS=1):
        _, service = await world()
        started = perf_counter()
        response = await service.run_turn("onde está meu pedido?", ANA, None)
        elapsed = perf_counter() - started
    ok = _degraded(response) and elapsed < 5
    return Verdict("agent_hang", assertion, ok,
                   f"elapsed={elapsed:.2f}s degradado={_degraded(response)}", elapsed)


async def _llm_failure(phase: str, status: int) -> tuple[object, float]:
    with env(CHAOS=1, CHAOS_SCENARIO="status", CHAOS_STATUS=status, CHAOS_TARGET="llm:",
             CHAOS_PHASE=phase, AGENT_TIMEOUT_SECONDS=30):
        _, service = await world(with_llm=True)
        started = perf_counter()
        response = await service.run_turn("onde está meu pedido?", ANA, None)
        return response, perf_counter() - started


async def scenario_llm_429_before_first_token() -> Verdict:
    """O provedor devolve 429 antes do primeiro token, em toda tentativa."""
    assertion = ("o turno responde mesmo assim (template determinístico), o retry tenta 3x por "
                 "modelo (e troca de modelo quando o agente tem fallback_model distinto), "
                 "e nenhuma exceção escapa")
    response, elapsed = await _llm_failure("before_first_token", 429)
    attempts = [call for call in response.llm_calls if call.get("status") == "error"]
    fallbacks = [call for call in attempts if call.get("fallback")]
    ok = bool(response.response) and len(attempts) >= 3 and elapsed < 30
    return Verdict("llm_429_before_first_token", assertion, ok,
                   f"tentativas_com_erro={len(attempts)} fallback={len(fallbacks)} resposta={bool(response.response)}",
                   elapsed)


async def scenario_llm_500_mid_stream() -> Verdict:
    """A resposta já saiu do provedor e a conexão cai antes de o turno usá-la."""
    assertion = ("falha depois do provedor responder não vira resposta pela metade: o turno cai "
                 "no template e o custo da chamada perdida fica registrado em llm_calls")
    response, elapsed = await _llm_failure("mid_stream", 500)
    errors = [call for call in response.llm_calls if call.get("status") == "error"]
    ok = bool(response.response) and bool(errors) and "sintética do provedor falso" not in response.response
    return Verdict("llm_500_mid_stream", assertion, ok,
                   f"erros_registrados={len(errors)} resposta_do_template={bool(response.response)}", elapsed)


async def scenario_llm_error_between_handoffs() -> Verdict:
    """O provedor cai ENTRE dois agentes, com o handoff já gravado."""
    assertion = ("handoff não é duplicado, o turno termina com estado explícito e o cliente não "
                 "recebe meia resposta em silêncio")
    with env(CHAOS=1, CHAOS_SCENARIO="status", CHAOS_STATUS=503, CHAOS_TARGET="handoff:",
             CHAOS_PHASE="between_handoffs", AGENT_TIMEOUT_SECONDS=30):
        store, service = await world()
        started = perf_counter()
        response = await service.run_turn("quero trocar o produto do meu pedido e saber a entrega", ANA, None)
        elapsed = perf_counter() - started
        handoffs = await store.find_many("agent_handoffs", {"conversation_id": response.conversation_id})
    pairs = [(item["from_agent"], item["to_agent"]) for item in handoffs]
    ok = len(pairs) == len(set(pairs)) and _degraded(response)
    return Verdict("llm_error_between_handoffs", assertion, ok,
                   f"handoffs={pairs} degradado={_degraded(response)}", elapsed)


async def scenario_tool_malformed_payload() -> Verdict:
    """A tool responde, mas com payload vazio/inesperado (índice fora do ar, driver estranho)."""
    assertion = ("nenhuma exceção escapa e o cliente recebe uma resposta honesta de 'sem dado', "
                 "nunca um dado inventado nem um 500")
    with env(CHAOS=1, CHAOS_SCENARIO="malformed", CHAOS_TARGET="tool:orders"):
        _, service = await world()
        started = perf_counter()
        response = await service.run_turn("onde está meu pedido?", ANA, None)
        elapsed = perf_counter() - started
    honest = "não encontrei" in response.response.lower()
    ok = bool(response.response) and honest
    return Verdict("tool_malformed_payload", assertion, ok,
                   f"resposta_honesta={honest} resposta={response.response[:70]!r}", elapsed)


async def scenario_tool_circuit_breaker() -> Verdict:
    """Tool falhando sem parar: o breaker abre e para de pagar a falha."""
    assertion = ("depois de 4 falhas consecutivas a tool entra em circuito aberto e as chamadas "
                 "seguintes são curto-circuitadas (ToolOpenCircuit), sem derrubar o turno")
    with env(CHAOS=1, CHAOS_SCENARIO="status", CHAOS_STATUS=500, CHAOS_TARGET="tool:orders",
             AGENT_TIMEOUT_SECONDS=30):
        store, service = await world()
        started = perf_counter()
        for _ in range(5):
            await service.run_turn("onde está meu pedido?", ANA, None)
        elapsed = perf_counter() - started
        state = resilience.circuit_state()
        opened = any(name.startswith("orders") and info["open"] for name, info in state.items())
        assert store is not None
    return Verdict("tool_circuit_breaker", assertion, opened,
                   f"estado={ {k: v for k, v in state.items() if k.startswith('orders')} }", elapsed)


async def scenario_loop_guard() -> Verdict:
    """Supervisor girando: mesmo agente e mesma intenção além do limite."""
    assertion = ("o supervisor corta a cadeia e escala para humano com motivo explícito "
                 "(timeline com reason=loop_guard), em vez de gastar o budget girando")
    with env(LOOP_GUARD_REPEATS=0, AGENT_TIMEOUT_SECONDS=30, CHAOS=None):
        _, service = await world()
        started = perf_counter()
        response = await service.run_turn("onde está meu pedido?", ANA, None)
        elapsed = perf_counter() - started
    tripped = [event for event in response.timeline if (event.reason or "") == "loop_guard"]
    ok = bool(tripped) and "atendente humano assume" in response.response
    return Verdict("loop_guard", assertion, ok,
                   f"eventos_loop_guard={len(tripped)} escalou={'atendente humano' in response.response}",
                   elapsed)


async def scenario_legacy_500_flag() -> Verdict:
    """Quem precisa do comportamento antigo ainda o tem — e ele é MESMO o antigo."""
    assertion = ("com SUPERVISOR_LEGACY_500=1 a falha do agente volta a subir como exceção "
                 "(vira 500 no handler global), provando que o default novo é o que degrada")
    with env(CHAOS=1, CHAOS_SCENARIO="status", CHAOS_STATUS=500, CHAOS_TARGET="tool:orders",
             SUPERVISOR_LEGACY_500=1, AGENT_TIMEOUT_SECONDS=30):
        _, service = await world()
        started = perf_counter()
        raised = ""
        try:
            await service.run_turn("onde está meu pedido?", ANA, None)
        except Exception as exc:  # noqa: BLE001 — é exatamente o que o modo legado faz
            raised = type(exc).__name__
        elapsed = perf_counter() - started
    return Verdict("legacy_500_flag", assertion, raised == "ChaosProviderError",
                   f"excecao_propagada={raised or 'nenhuma'}", elapsed)


async def scenario_concurrent_same_conversation() -> Verdict:
    """5 requests simultâneos na MESMA conversation_id."""
    assertion = ("nenhum turno se perde nem duplica: a conversa guarda 2 mensagens por turno, "
                 "existe UM documento de conversa e nenhuma exceção escapa")
    with env(AGENT_TIMEOUT_SECONDS=30, CHAOS=None):
        store, service = await world()
        first = await service.run_turn("onde está meu pedido?", ANA, None)
        conversation_id = first.conversation_id
        started = perf_counter()
        results = await asyncio.gather(*[
            service.run_turn(f"e o pedido {index}, já saiu?", ANA, conversation_id)
            for index in range(5)
        ], return_exceptions=True)
        elapsed = perf_counter() - started
        documents = await store.find_many("agent_conversations", {"conversation_id": conversation_id})
    errors = [item for item in results if isinstance(item, BaseException)]
    turns = len(documents[0]["turns"]) if documents else 0
    # 1 turno inicial + 5 concorrentes = 12 mensagens (2 por turno), limitadas ao teto de 20.
    ok = not errors and len(documents) == 1 and turns == 12
    return Verdict("concurrent_same_conversation", assertion, ok,
                   f"erros={[type(e).__name__ for e in errors]} documentos={len(documents)} mensagens={turns}",
                   elapsed)


async def scenario_crash_resume() -> Verdict:
    """SIGKILL no meio de uma conversa com estado persistido."""
    assertion = ("depois do SIGKILL e do restart, GET /api/conversations/latest devolve a conversa "
                 "com o turno anterior — nada de conversa fantasma nem estado meio gravado")
    from scripts.crash_resume import run_crash_resume

    started = perf_counter()
    result = await run_crash_resume()
    return Verdict("crash_resume", assertion, result["passed"], result["detail"],
                   perf_counter() - started, skipped=result.get("skipped", False))


@contextlib.contextmanager
def web_on():
    """Liga a busca web no mundo em memória (o registro real falha fechado em DEMO_MODE) com um provedor falso
    que conta as chamadas — o caos é injetado na fronteira `call_tool("web_search")`, não no provedor."""
    calls: list[str] = []

    async def provider(query, _cfg):
        calls.append(query)
        return {"results": [{"title": "Notícia", "url": "https://example.com/n", "content": "sem atrasos"}]}

    async def config(_store, area):
        return {**web_search.DEFAULTS, "enabled": True} if area == "varejo" else None

    saved = (web_search.PROVIDER_CALL, web_search.get_config)
    web_search.PROVIDER_CALL, web_search.get_config = provider, config
    try:
        yield calls
    finally:
        web_search.PROVIDER_CALL, web_search.get_config = saved


WEB_TURN = "qual a transportadora do PED-1001?"   # PED-1001: em trânsito, previsão vencida → busca a transportadora


def _answered_from_mongo(response) -> bool:
    return "Correios Express" in response.response and "venceu" in response.response


def _web_events(response) -> list:
    return [event for event in response.timeline if event.op == "external"]


async def scenario_web_timeout() -> Verdict:
    """O provedor de busca web para de responder."""
    assertion = ("turno termina em < 5s respondendo com o documento do MongoDB (transportadora, previsão vencida); "
                 "o evento web registra a falha e nenhuma fonte é inventada")
    with env(CHAOS=1, CHAOS_SCENARIO="timeout", CHAOS_TARGET="tool:web_search", CHAOS_DELAY=20,
             TOOL_TIMEOUT_SECONDS=1, AGENT_TIMEOUT_SECONDS=10), web_on():
        _, service = await world()
        started = perf_counter()
        response = await service.run_turn(WEB_TURN, ANA, None)
        elapsed = perf_counter() - started
    events = _web_events(response)
    ok = (elapsed < 5 and _answered_from_mongo(response) and bool(events)
          and events[0].result["error"] and not events[0].result["sources"])
    return Verdict("web_timeout", assertion, bool(ok),
                   f"elapsed={elapsed:.2f}s mongo={_answered_from_mongo(response)} "
                   f"erro={events[0].result['error'] if events else None}", elapsed)


async def scenario_web_circuit_breaker() -> Verdict:
    """Provedor de busca falhando sem parar: o breaker abre e para de chamar."""
    assertion = ("depois de 4 falhas consecutivas o web_search entra em circuito aberto, as chamadas seguintes nem "
                 "chegam ao provedor e todo turno continua respondendo com o MongoDB")
    with env(CHAOS=1, CHAOS_SCENARIO="status", CHAOS_STATUS=503, CHAOS_TARGET="tool:web_search"), web_on() as calls:
        _, service = await world()
        started = perf_counter()
        responses = [await service.run_turn(WEB_TURN, ANA, None) for _ in range(6)]
        elapsed = perf_counter() - started
        opened = resilience.circuit_state().get("web_search", {}).get("open", False)
    last = _web_events(responses[-1])
    ok = (opened and calls == [] and all(_answered_from_mongo(r) for r in responses)
          and last and last[0].result["error"] == "ToolOpenCircuit")
    return Verdict("web_circuit_breaker", assertion, bool(ok),
                   f"aberto={opened} chamadas_ao_provedor={len(calls)} "
                   f"último_erro={last[0].result['error'] if last else None}", elapsed)


async def scenario_web_malformed_payload() -> Verdict:
    """O provedor responde, mas com payload fora do formato."""
    assertion = ("o payload ruim é recusado pelo parser (nunca repassado ao modelo), o turno responde com o MongoDB "
                 "e nada é gravado no cache de resultado")
    with env(CHAOS=1, CHAOS_SCENARIO="malformed", CHAOS_TARGET="tool:web_search"), web_on():
        store, service = await world()
        started = perf_counter()
        response = await service.run_turn(WEB_TURN, ANA, None)
        elapsed = perf_counter() - started
        cached = await store.find_many("web_search_cache", {})
    events = _web_events(response)
    ok = _answered_from_mongo(response) and bool(events) and events[0].result["error"] and cached == []
    return Verdict("web_malformed_payload", assertion, bool(ok),
                   f"mongo={_answered_from_mongo(response)} erro={events[0].result['error'] if events else None} "
                   f"cache={len(cached)}", elapsed)


# ---------------------------------------------------------------- KB (2026-10): $rankFusion/$rerank/sinônimos sob falha, LIVE no *_test


class _ServerSays(Exception):
    """Recusa do servidor com `code`, como o OperationFailure do driver."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


class _MaxTime(Exception):
    code = 50


def _kb_wrap(store, fail):
    """Embrulha o aggregate REAL: `fail(pipeline)` devolve a exceção a levantar (ou None)."""
    real_aggregate, real_collection = store.aggregate, store._collection

    async def aggregate(name, pipeline, **kw):
        exc = fail(pipeline)
        if exc:
            raise exc
        return await real_aggregate(name, pipeline, **kw)

    def collection(name, brain=False):
        coll = real_collection(name, brain)
        if name != "kb_articles":
            return coll

        class _C:
            async def aggregate(self, pipeline, *a, **k):
                exc = fail(pipeline)
                if exc:
                    raise exc
                return await coll.aggregate(pipeline, *a, **k)
        return _C()
    store.aggregate, store._collection = aggregate, collection


def _is_fusion(p):
    return "$rankFusion" in p[0]


def _has_synonyms(p):
    return "synonyms" in str(p)


async def _kb_live(name, assertion, fail, expect, *, agent_doc=None, then=None):
    if os.getenv("LIVE", "").strip() not in {"1", "true", "yes"} and os.getenv("CHAOS_KB_LIVE") != "1":
        return Verdict(name, assertion, False, "LIVE=1 ausente — cenário não roda contra Atlas", skipped=True)
    from app import retrieval
    from app.agents import _local_rank
    from scripts.isolation import open_test_store

    store, _ = await open_test_store(what=f"chaos_suite {name}", seed_if_empty=False, wait_indexes=False)
    started = perf_counter()
    try:
        retrieval._capabilities.reset()
        _kb_wrap(store, fail)
        found = await retrieval.search_kb_explained(store, "fone sem áudio", agent_doc=agent_doc,
                                                    local_rank=_local_rank)
        # resiliente = caminho de fallback esperado e o artigo certo ainda entre os 4 que vão ao
        # agente (no fallback a ORDEM pode piorar: a perna vetorial sozinha põe KB-002 na frente)
        ok = found.strategy in expect and "KB-001" in [a["article_id"] for a in found.articles]
        detail = f"estratégia={found.strategy} topo={found.articles[0]['article_id'] if found.articles else None} fallbacks={found.fallbacks}"
        if then is not None:
            store.aggregate, store._collection = DataStore.aggregate.__get__(store), DataStore._collection.__get__(store)
            again = await retrieval.search_kb_explained(store, "fone sem áudio", agent_doc=agent_doc,
                                                        local_rank=_local_rank)
            ok = ok and again.strategy == then
            detail += f" | sem falha depois: {again.strategy}"
        return Verdict(name, assertion, ok, detail, perf_counter() - started)
    finally:
        await store.close()


async def scenario_kb_synonyms_unavailable() -> Verdict:
    return await _kb_live("kb_synonyms_unavailable",
                          "mapeamento de sinônimos recusado → $rankFusion sem sinônimos/fuzzy, artigo certo mantido",
                          lambda p: _ServerSays("synonym mapping 'kb_sinonimos' not found", 8)
                          if _is_fusion(p) and _has_synonyms(p) else None, {"rankFusion_basico"})


async def scenario_kb_fusion_rejected() -> Verdict:
    return await _kb_live("kb_fusion_rejected",
                          "$rankFusion recusado → duas pernas em paralelo + RRF na aplicação; recusa lembrada",
                          lambda p: _ServerSays("Unrecognized pipeline stage name: '$rankFusion'", 40324)
                          if _is_fusion(p) else None, {"rrf_aplicacao"}, then="rrf_aplicacao")


async def scenario_kb_lexical_building() -> Verdict:
    return await _kb_live("kb_lexical_building",
                          "índice lexical em BUILDING (fusão e $search falham) → só a perna vetorial",
                          lambda p: _ServerSays("search index kb_lexical_v1 is not READY", 8)
                          if _is_fusion(p) or "$search" in p[0] else None, {"vetor"})


async def scenario_kb_rerank_rejected() -> Verdict:
    return await _kb_live("kb_rerank_rejected",
                          "$rerank recusado → no-op: fusão sem rerank e evento 'rerank indisponível'",
                          lambda p: _ServerSays("Native Reranking is not enabled", 2)
                          if any("$rerank" in st for st in p) else None, {"rerank_indisponivel"},
                          agent_doc={"retrieval": {"rerank": {"enabled": True, "model": "rerank-2.5-lite"}}})


async def scenario_kb_max_time() -> Verdict:
    return await _kb_live("kb_max_time",
                          "maxTimeMS estourado na fusão → fallback neste turno SEM marcar o $rankFusion como indisponível",
                          lambda p: _MaxTime("operation exceeded time limit") if _is_fusion(p) else None,
                          {"rrf_aplicacao"}, then="rankFusion")


SCENARIOS = {
    "tool_timeout": scenario_tool_timeout,
    "agent_hang": scenario_agent_hang,
    "llm_429_before_first_token": scenario_llm_429_before_first_token,
    "llm_500_mid_stream": scenario_llm_500_mid_stream,
    "llm_error_between_handoffs": scenario_llm_error_between_handoffs,
    "tool_malformed_payload": scenario_tool_malformed_payload,
    "tool_circuit_breaker": scenario_tool_circuit_breaker,
    "loop_guard": scenario_loop_guard,
    "legacy_500_flag": scenario_legacy_500_flag,
    "concurrent_same_conversation": scenario_concurrent_same_conversation,
    "crash_resume": scenario_crash_resume,
    "web_timeout": scenario_web_timeout,
    "web_circuit_breaker": scenario_web_circuit_breaker,
    "web_malformed_payload": scenario_web_malformed_payload,
    "kb_synonyms_unavailable": scenario_kb_synonyms_unavailable,
    "kb_fusion_rejected": scenario_kb_fusion_rejected,
    "kb_lexical_building": scenario_kb_lexical_building,
    "kb_rerank_rejected": scenario_kb_rerank_rejected,
    "kb_max_time": scenario_kb_max_time,
}


async def main(names: list[str]) -> int:
    chosen = names or list(SCENARIOS)
    verdicts = []
    for name in chosen:
        try:
            verdicts.append(await SCENARIOS[name]())
        except Exception as exc:  # noqa: BLE001 — um cenário que explode É o achado
            verdicts.append(Verdict(name, "cenário não deveria levantar exceção", False,
                                    f"EXCEÇÃO {type(exc).__name__}: {exc}"))
    print()
    for verdict in verdicts:
        print(verdict.line())
        print(f"       assertion: {verdict.assertion}")
    failed = [item for item in verdicts if not item.passed and not item.skipped]
    print(f"\n{len(verdicts) - len(failed)}/{len(verdicts)} cenários resilientes"
          f"{'' if not failed else ' — falhas: ' + ', '.join(item.name for item in failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
