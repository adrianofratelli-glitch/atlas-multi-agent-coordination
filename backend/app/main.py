import asyncio
import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import edge_guardrails, observability, resilience
from .budget import BudgetExceeded
from .cascade import CACHE_POLICY
from .config import get_settings
from .database import DataStore, get_store, set_store, utcnow
from .decisions import decision_trail
from .reviews import list_reviews, override_rate, resolve_review
from .llm import LLMGateway
from .metrics import metrics
from .models import AgentUpdate, ChatRequest, ChatResponse, ReviewResolution, TokenRequest
from .demo_reset import reset_customer_memory
from .warmup import WarmupService
from .orchestration import OrchestrationService
from .rate_limit import SlidingWindowLimiter
from .security import current_customer, issue_token, request_identity_key, require_admin


settings = get_settings()
limiter = SlidingWindowLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
logger = logging.getLogger("multi-agent-poc")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def log(event: str, **fields) -> None:
    payload = {"event": event, **edge_guardrails.mask_log(fields)}
    logger.info(json.dumps(payload, default=str) if settings.log_json else f"{event} {fields}")


def validate_runtime_security(runtime_settings) -> None:
    """Fail startup on configurations that would expose demo credentials."""
    if runtime_settings.environment.lower() in {"development", "dev", "local"}:
        return
    if runtime_settings.jwt_secret == "desenvolvimento-inseguro-troque-este-segredo":
        raise RuntimeError("JWT_SECRET inseguro recusado fora de development")
    if runtime_settings.admin_api_key == "admin-demo":
        raise RuntimeError("ADMIN_API_KEY insegura recusada fora de development")
    if len(runtime_settings.jwt_secret) < 32 or len(runtime_settings.admin_api_key) < 24:
        raise RuntimeError("segredos de produção devem ter pelo menos 32/24 caracteres")
    if not runtime_settings.auth_required:
        raise RuntimeError("AUTH_REQUIRED deve permanecer ligado fora de development")
    if runtime_settings.demo_token_issuance_enabled:
        raise RuntimeError("DEMO_TOKEN_ISSUANCE_ENABLED deve estar desligado fora de development")
    if "*" in runtime_settings.cors_origin_list:
        raise RuntimeError("CORS_ORIGINS='*' é recusado fora de development")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_security(settings)
    # Antes de criar qualquer client: os instrumentadores precisam existir na importação deles.
    trace_sink = observability.setup_tracing(settings.app_name)
    store = DataStore(settings)
    await store.connect()
    set_store(store)
    # DEMO_MODE nasce pronto; em Atlas o seed explícito também cria Search indexes.
    if store.memory:
        backend_dir = Path(__file__).resolve().parents[1]
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from seed import seed

        await seed(store, create_indexes=False)
    llm = LLMGateway(settings)
    app.state.orchestrator = OrchestrationService(store, llm, settings.global_turn_token_budget)
    app.state.warmup = WarmupService(store, app.state.orchestrator, has_llm=bool(llm.client) and not settings.demo_mode,
                                     cooldown_minutes=settings.warmup_cooldown_minutes)
    if settings.warmup_on_start:
        app.state.warmup.trigger()  # a demo já nasce aquecida; a UI dispara de novo quando abre (respeita o cooldown)
    log("startup", storage="memory" if store.memory else "mongodb_atlas", trace_sink=trace_sink,
        graceful_degradation=resilience.graceful_degradation(), tool_breaker=resilience.tool_breaker_enabled(),
        edge_guardrails=edge_guardrails.enabled())
    yield
    with suppress(Exception):
        if app.state.warmup._task:
            app.state.warmup._task.cancel()
    await store.close()


app = FastAPI(title="Multi-Agent PoV", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key", "X-Request-Id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log("request_error", request_id=request_id, path=request.url.path)
        raise
    response.headers["X-Request-Id"] = request_id
    log("request", request_id=request_id, path=request.url.path, status=response.status_code, duration_ms=round((perf_counter() - started) * 1000, 2))
    return response


@app.exception_handler(BudgetExceeded)
async def budget_handler(_: Request, exc: BudgetExceeded):
    return JSONResponse(status_code=429, content={"detail": f"Turno encerrado com resposta parcial: {exc}"})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    log("unhandled_error", request_id=request_id, path=request.url.path, error=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Falha interna ao processar o turno. Consulte o request_id nos logs.",
            "request_id": request_id,
        },
    )


@app.post("/api/auth/token")
async def create_token(payload: TokenRequest, store: DataStore = Depends(get_store)):
    if not settings.demo_token_issuance_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "emissão de token demo desabilitada")
    customer = await store.find_one("customers", {"customer_key": payload.customer_key})
    if not customer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "identidade demo não encontrada")
    return {"access_token": issue_token(customer["customer_key"], settings), "token_type": "bearer", "customer": {key: customer[key] for key in ("customer_key", "name", "area", "plan")}}


def _degraded_turn(payload: ChatRequest, customer: dict, reason: str) -> ChatResponse:
    """Degradação graciosa no topo: o turno falhou, mas o cliente recebe estado explícito (200),
    nunca um 500 mudo. É o padrão; SUPERVISOR_LEGACY_500=1 devolve o comportamento antigo."""
    return ChatResponse(
        conversation_id=payload.conversation_id or f"conv-{uuid.uuid4().hex[:12]}",
        response=resilience.degraded_reply("atendimento"),
        active_agent="supervisor", route_source="fallback", cache_hit=False,
        timeline=[], usage={}, suggestions=[],
        llm_calls=[], economics={}, degraded=True, degraded_reason=reason,
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest, customer: Annotated[dict, Depends(current_customer)]):
    if not limiter.allow(request_identity_key(request, customer["customer_key"])):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    async with metrics.track_route("chat"):
        with observability.span("turn", conversation_id=payload.conversation_id or "novo",
                                customer_key=customer["customer_key"], area=customer["area"]):
            try:
                response = await asyncio.wait_for(
                    request.app.state.orchestrator.run_turn(payload.message, customer, payload.conversation_id),
                    timeout=settings.turn_deadline_seconds,
                )
            except TimeoutError as exc:
                await metrics.increment("turns.deadline_exceeded")
                if resilience.graceful_degradation():
                    return _degraded_turn(payload, customer, "deadline global do turno excedido")
                raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "deadline global do turno excedido") from exc
            except (BudgetExceeded, HTTPException):
                raise
            except Exception as exc:  # noqa: BLE001 — por padrão, falha não vira 500 mudo
                if not resilience.graceful_degradation():
                    raise
                await metrics.increment("turns.degraded")
                log("turn_degraded", error=type(exc).__name__, customer_key=customer["customer_key"])
                return _degraded_turn(payload, customer, type(exc).__name__)
            edge_guardrails.validate_response(ChatResponse, response)
            return response


@app.post("/api/warmup")
async def warmup(request: Request):
    """Aquece o cache em segundo plano e responde na hora. Aberto de propósito (a UI chama ao abrir), e seguro:
    execução única por vez e no máximo uma a cada `warmup_cooldown_minutes`, então cliques/abas repetidos custam zero."""
    if not limiter.allow(request_identity_key(request, "warmup")):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    return request.app.state.warmup.trigger()


@app.post("/api/demo/reset")
async def demo_reset(customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Desfaz o que a demo gravou NESTE cliente (customer_key do JWT, nunca do corpo) para repetir o roteiro."""
    if not settings.demo_token_issuance_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reset da demo desabilitado")
    return await reset_customer_memory(store, customer["customer_key"])


@app.get("/api/agents")
async def agents(_: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    items = await store.find_many("agent_registry", {}, brain=True, limit=20)
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


@app.get("/api/demo-scenarios")
async def demo_scenarios(customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Roteiro versionado no plano de coordenação; UI, warmup e eval usam a mesma fonte."""
    items = await store.find_many(
        "demo_scenarios",
        {"customer_key": customer["customer_key"]},
        brain=True,
        limit=20,
        sort=[("position", 1)],
    )
    public_keys = ("scenario_id", "position", "label", "message", "capabilities")
    return [{key: item[key] for key in public_keys if key in item} for item in items]


@app.get("/api/conversations/latest")
async def latest_conversation(customer: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    conversations = await store.find_many("agent_conversations", {"customer_key": customer["customer_key"]}, limit=1, sort=[("updated_at", -1)])
    if not conversations:
        return None
    conversation = {key: value for key, value in conversations[0].items() if key != "_id"}
    # sem isso, retomar a conversa mostra o texto certo mas a timeline/esteira ficam vazias — parece que o
    # multi-agent não rodou, quando na verdade só não foi recarregado o raio-x do último turno.
    traces = await store.find_many("agent_traces", {"conversation_id": conversation["conversation_id"]}, limit=1, sort=[("at", -1)])
    if traces:
        trace = traces[0]
        conversation["last_timeline"] = trace.get("timeline", [])
        conversation["last_usage"] = trace.get("usage", {})
        conversation["last_llm_calls"] = trace.get("llm_calls", [])
        conversation["last_economics"] = trace.get("economics", {})
    return conversation


@app.get("/api/handoffs")
async def handoffs(conversation_id: str = Query(min_length=4, max_length=80), customer: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    owner = await store.find_one("agent_conversations", {"conversation_id": conversation_id, "customer_key": customer["customer_key"]})
    if not owner:
        return []
    items = await store.find_many("agent_handoffs", {"conversation_id": conversation_id}, limit=100, sort=[("at", 1)])
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


@app.get("/api/decisions")
async def decisions(
    subject_id: str | None = Query(default=None, max_length=64),
    customer: dict = Depends(current_customer),
    store: DataStore = Depends(get_store),
):
    """Trilha de conformidade do próprio chamador: decisões imutáveis + eventos de auditoria.

    A `customer_key` vem do JWT, nunca do query string — a trilha de um cliente não é
    alcançável por outro nem informando o subject_id certo.
    """
    return await decision_trail(store, customer["customer_key"], subject_id=subject_id)


@app.get("/api/reviews")
async def my_reviews(
    status_filter: str = Query(default="pending", pattern="^(pending|resolved)$", alias="status"),
    customer: dict = Depends(current_customer),
    store: DataStore = Depends(get_store),
):
    """Casos do próprio cliente que estão (ou estiveram) aguardando decisão humana."""
    return await list_reviews(store, customer_key=customer["customer_key"], status=status_filter)


@app.get("/api/memory/{customer_key}")
async def memory(customer_key: str, customer: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    if customer_key != customer["customer_key"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "memória pertence a outra identidade")
    items = await store.find_many("customer_memory", {"customer_key": customer_key, "active": True}, limit=50)
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


# Painel "ai_brain" da demo: mostra ao cliente, coleção por coleção, o que a cascata
# checou ANTES de gastar token com o LLM. Sempre filtrado pelo customer_key do JWT —
# nunca aceita customer_key vindo do path para outra identidade, mesmo doc próprio.
INSPECTOR_COLLECTIONS = {
    "cache": "semantic_cache",
    "short": "short_term_memory",
    "long": "long_term_memory",
    "facts": "customer_memory",
}


@app.get("/api/inspector/{view}")
async def inspector(
    view: str,
    conversation_id: str | None = None,
    customer: dict = Depends(current_customer),
    store: DataStore = Depends(get_store),
):
    collection = INSPECTOR_COLLECTIONS.get(view)
    if not collection:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "visão de inspetor desconhecida")
    query = {"customer_key": customer["customer_key"]}
    if view == "cache":
        query["cache_policy"] = CACHE_POLICY
    if view == "facts":
        query["active"] = True
    if view == "short":
        # Memória de CURTO prazo é por sessão. Filtrar só por customer_key mostrava as
        # conversas anteriores todas: abrir uma aba nova, sem ter perguntado nada, exibia
        # 5 documentos — e o painel passava a contradizer o próprio conceito que ele existe
        # para provar. Sem conversa ativa, o correto é vir vazio.
        query["session_id"] = conversation_id or "__sem_conversa__"
    items = await store.find_many(collection, query, limit=30, sort=[("created_at", -1)])
    return {
        "view": view,
        "collection": collection,
        "customer_key": customer["customer_key"],
        "items": [{key: value for key, value in item.items() if key != "_id"} for item in items],
    }


# Admin-only: eventos/candidatos/denylist expõem mensagens de OUTROS clientes
# (tentativas de manipulação, PII mascarada) — não é dado de cliente comum.
@app.get("/api/guardrails/{view}", dependencies=[Depends(require_admin)])
async def guardrails(view: str, store: DataStore = Depends(get_store)):
    mapping = {"events": "guardrail_events", "candidates": "guardrail_candidates", "denylist": "guardrail_denylist"}
    collection = mapping.get(view)
    if not collection:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "visão de guardrail desconhecida")
    items = await store.find_many(collection, {}, limit=100, sort=[("at" if view == "events" else "created_at", -1)])
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


async def handoff_event_stream(request: Request, store: DataStore, customer_key: str, heartbeat_seconds: float = 15):
    """Adapt the Change Stream to SSE with immediate confirmation and heartbeats."""
    queue: asyncio.Queue[tuple[str, object | None]] = asyncio.Queue()

    async def pump_handoffs() -> None:
        try:
            async for event in store.watch_handoffs(customer_key):
                await queue.put(("event", event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    pump = asyncio.create_task(pump_handoffs())
    try:
        # Confirm immediately and keep proxies/browsers alive while the stream is idle.
        yield ": connected\n\n"
        while not await request.is_disconnected():
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if kind == "event":
                yield f"data: {json.dumps(payload, default=str)}\n\n"
            elif kind == "error":
                log("change_stream_error", customer_key=customer_key, error=type(payload).__name__)
                break
            else:
                break
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump


@app.get("/api/events/stream")
async def events_stream(request: Request, customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Feed ao vivo de coordenação: Change Stream do Atlas em agent_handoffs, via SSE."""

    return StreamingResponse(
        handoff_event_stream(request, store, customer["customer_key"]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/metrics")
async def get_metrics(_: dict = Depends(current_customer)):
    return metrics.snapshot()


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_admin)])
async def prometheus_metrics():
    return Response(metrics.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/api/health")
async def health(store: DataStore = Depends(get_store)):
    try:
        await store.ping()
        agents_count, handoffs_count, traces_count = await asyncio.gather(
            store.count("agent_registry", {"active": True}, brain=True), store.count("agent_handoffs"), store.count("agent_traces")
        )
        return {"status": "ok", "storage": "memory-demo" if store.memory else "mongodb-atlas", "mongodb": True, "anthropic_configured": bool(settings.anthropic_api_key), "counts": {"agents": agents_count, "handoffs": handoffs_count, "traces": traces_count}, "at": utcnow()}
    except Exception:
        logger.warning("healthcheck storage unavailable", exc_info=True)
        return JSONResponse(status_code=503, content={"status": "degraded", "mongodb": False})


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.patch("/api/admin/agents/{agent_key}", dependencies=[Depends(require_admin)])
async def update_agent(agent_key: str, payload: AgentUpdate, store: DataStore = Depends(get_store)):
    update = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not update:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nenhuma alteração")
    changed = await store.update_one("agent_registry", {"agent_key": agent_key}, {"$set": update}, brain=True)
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agente não encontrado")
    await store.insert_one("admin_audit", {"action": "agent.update", "target": agent_key, "changes": update, "at": utcnow()})
    return {"ok": True, "agent_key": agent_key, "changes": update}


@app.get("/api/admin/reviews", dependencies=[Depends(require_admin)])
async def pending_reviews(
    status_filter: str = Query(default="pending", pattern="^(pending|resolved)$", alias="status"),
    store: DataStore = Depends(get_store),
):
    """Fila do analista: todos os casos pausados, de todos os clientes."""
    return {"reviews": await list_reviews(store, status=status_filter),
            "override": await override_rate(store)}


@app.post("/api/admin/reviews/{review_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_pending_review(review_id: str, payload: ReviewResolution, store: DataStore = Depends(get_store)):
    """Fecha a pausa: grava a decisão humana (imutável) e devolve o caso ao agente.

    O handoff de volta é gravado em `agent_handoffs`, então a UI do cliente é notificada ao vivo
    pelo Change Stream que já existe — sem canal novo.
    """
    try:
        resolved = await resolve_review(store, review_id, human_decision=payload.decision,
                                        resolved_by=payload.resolved_by, note=payload.note)
    except RuntimeError as exc:
        # A decisão final não foi registrada, então a revisão continua pendente de propósito.
        # 503 e não 500: é uma falha transitória de gravação, e repetir a chamada é a ação certa.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not resolved:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "revisão não encontrada ou já resolvida")
    await store.insert_one("admin_audit", {"action": "review.resolve", "target": review_id,
                                           "changes": {"decision": payload.decision}, "at": utcnow()})
    return resolved


@app.post("/api/admin/guardrails/candidates/{candidate_id}/approve", dependencies=[Depends(require_admin)])
async def approve_candidate(candidate_id: str, store: DataStore = Depends(get_store)):
    candidate = await store.find_one("guardrail_candidates", {"_id": candidate_id})
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidato não encontrado")
    phrase = candidate["text"]
    await store.replace_one("guardrail_denylist", {"phrase_norm": phrase.lower()}, {"phrase": phrase, "phrase_norm": phrase.lower(), "active": True}, upsert=True)
    await store.update_one("guardrail_candidates", {"_id": candidate_id}, {"$set": {"status": "approved"}})
    await store.insert_one("admin_audit", {"action": "guardrail.approve", "target": candidate_id, "at": utcnow()})
    return {"ok": True}


@app.get("/api/eval/runs", dependencies=[Depends(require_admin)])
async def eval_runs(store: DataStore = Depends(get_store)):
    """Histórico de qualidade (GoalSuccessRate por caso) — gravado por backend/eval.py a cada execução."""
    items = await store.find_many("eval_runs", {}, limit=30, sort=[("at", -1)])
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]
