import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import edge_guardrails, live_events, observability, resilience, usage_ts
from .budget import BudgetExceeded
from .cascade import CACHE_POLICY
from .config import get_settings
from .database import DataStore, get_store, set_store, utcnow
from .decisions import decision_trail
from .reviews import list_reviews, override_rate, resolve_review
from .llm import LLMGateway
from .metrics import metrics
from .models import (AgentUpdate, ChatRequest, ChatResponse, CompareRequest, HitlPolicyUpdate,
                     ReviewResolution, TokenRequest)
from .demo_reset import reset_customer_memory
from .warmup import WarmupService
from .orchestration import OrchestrationService
from .rate_limit import SlidingWindowLimiter
from .security import current_customer, issue_token, request_identity_key, require_admin


settings = get_settings()
limiter = SlidingWindowLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
compare_limiter = SlidingWindowLimiter(3, 60)
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


app = FastAPI(title="Multi-Agent PoV", version="1.3.0", lifespan=lifespan)
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


async def _run_chat(request: Request, payload: ChatRequest, customer: dict) -> ChatResponse:
    """Um turno, com o mesmo deadline/degradação para /api/chat e /api/chat/stream."""
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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest, customer: Annotated[dict, Depends(current_customer)]):
    if not limiter.allow(request_identity_key(request, customer["customer_key"])):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    return await _run_chat(request, payload, customer)


@app.post("/api/chat/stream")
async def chat_stream(request: Request, payload: ChatRequest, customer: Annotated[dict, Depends(current_customer)]):
    """O MESMO turno de /api/chat, com cada evento da timeline em SSE assim que nasce
    (`event: timeline`) e a resposta completa no fim (`event: result`, idêntica ao /api/chat)."""
    if not limiter.allow(request_identity_key(request, customer["customer_key"])):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    def on_event(event) -> None:
        if getattr(event, "replayed", False):
            return   # eventos de replay do cache chegam junto do resultado
        queue.put_nowait(("timeline", event.model_dump(mode="json")))

    async def run() -> None:
        try:
            with live_events.capture(on_event):
                response = await _run_chat(request, payload, customer)
            await queue.put(("result", response.model_dump(mode="json")))
        except HTTPException as exc:
            await queue.put(("error", {"status": exc.status_code, "message": str(exc.detail)}))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", {"status": 500, "message": type(exc).__name__}))

    async def body():
        task = asyncio.create_task(run())
        try:
            yield ": connected\n\n"
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
                if kind in ("result", "error"):
                    return
        finally:
            if not task.done():
                task.cancel()   # cliente desconectou: o turno não continua órfão
                with suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.post("/api/warmup")
async def warmup(request: Request):
    """Aquece o cache em segundo plano e responde na hora. Aberto de propósito (a UI chama ao abrir), e seguro:
    execução única por vez e no máximo uma a cada `warmup_cooldown_minutes`, então cliques/abas repetidos custam zero."""
    if not limiter.allow(request_identity_key(request, "warmup")):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    return request.app.state.warmup.trigger()


@app.post("/api/demo/reset")
async def demo_reset(request: Request, customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Desfaz o que a demo gravou NESTE cliente (customer_key do JWT, nunca do corpo) para repetir o roteiro.

    Fica liberado no banco da demo de propósito: é o botão "Reiniciar memória da demo" usado AO VIVO, e o
    escopo já é estreito (só o que o extrator/cache gravou para o cliente do token; nunca dado de negócio,
    cache global ou outro cliente). Fora da demo some junto com a emissão de token de demonstração
    (`DEMO_TOKEN_ISSUANCE_ENABLED=0`, obrigatório em produção). O reset COMPLETO é scripts/reset_demo.py.
    """
    if not settings.demo_token_issuance_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reset da demo desabilitado")
    if not limiter.allow(request_identity_key(request, f"reset:{customer['customer_key']}")):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
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
    # O cliente vê o caso e a decisão; quem decidiu, a nota interna e a evidência ficam com o analista.
    private = ("resolved_by", "note", "evidence")
    return [{k: v for k, v in review.items() if k not in private}
            for review in await list_reviews(store, customer_key=customer["customer_key"], status=status_filter)]


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
    # `candidate_id` (o _id em texto) é o que POST .../candidates/{id}/approve espera.
    return [{**{key: value for key, value in item.items() if key != "_id"},
             **({"candidate_id": str(item["_id"])} if view == "candidates" and "_id" in item else {})} for item in items]


# Teto de assinantes do feed ao vivo (ADR-009): cada assinante abre um change stream (um cursor
# no servidor). Sem teto, abas esquecidas ou um cliente hostil abriam cursores sem limite.
MAX_HANDOFF_SUBSCRIBERS = int(os.getenv("MAX_HANDOFF_SUBSCRIBERS", "100"))
MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER = int(os.getenv("MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER", "5"))
_handoff_subscribers: dict[str, int] = {}


def _subscribe(customer_key: str) -> bool:
    total = sum(_handoff_subscribers.values())
    if total >= MAX_HANDOFF_SUBSCRIBERS or _handoff_subscribers.get(customer_key, 0) >= MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER:
        return False
    _handoff_subscribers[customer_key] = _handoff_subscribers.get(customer_key, 0) + 1
    return True


class _Subscription:
    """Liberação idempotente: chamada no fim do gerador E na BackgroundTask da resposta (se o
    cliente cai antes do gerador começar, o `finally` dele nunca roda)."""

    def __init__(self, customer_key: str):
        self.customer_key, self.released = customer_key, False

    def release(self) -> None:
        if not self.released:
            self.released = True
            _unsubscribe(self.customer_key)


def _unsubscribe(customer_key: str) -> None:
    left = _handoff_subscribers.get(customer_key, 0) - 1
    if left > 0:
        _handoff_subscribers[customer_key] = left
    else:
        _handoff_subscribers.pop(customer_key, None)


async def handoff_event_stream(request: Request, store: DataStore, customer_key: str, heartbeat_seconds: float = 15,
                               subscription: "_Subscription | None" = None):
    """Adapt the Change Stream to SSE with immediate confirmation and heartbeats."""
    try:
        async for chunk in _handoff_event_stream(request, store, customer_key, heartbeat_seconds):
            yield chunk
    finally:
        if subscription is not None:
            subscription.release()


async def _handoff_event_stream(request: Request, store: DataStore, customer_key: str, heartbeat_seconds: float = 15):
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
            if kind == "event" and isinstance(payload, dict) and "_resync" in payload:
                # histórico perdido/invalidate: a UI relê /api/handoffs em vez de perder eventos
                yield f"event: resync\ndata: {json.dumps({'reason': str(payload['_resync'])[:80]})}\n\n"
            elif kind == "event":
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
    if not _subscribe(customer["customer_key"]):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "limite de feeds ao vivo abertos atingido; feche uma aba ou tente de novo")
    subscription = _Subscription(customer["customer_key"])
    return StreamingResponse(
        handoff_event_stream(request, store, customer["customer_key"], subscription=subscription),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        background=BackgroundTask(subscription.release),
    )


@app.get("/api/metrics")
async def get_metrics(_: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    snapshot = metrics.snapshot()
    # Economia agregada no servidor a partir da time series (ADR-008); sem ela, o snapshot em
    # processo de sempre continua respondendo.
    try:
        snapshot["llm_usage"] = await usage_ts.usage_report(store, 24)
    except Exception:  # noqa: BLE001
        snapshot["llm_usage"] = {"source": "in_process", "window_hours": 24}
    return snapshot


@app.get("/api/economics")
async def get_economics(hours: int = Query(24, ge=1, le=usage_ts.MAX_WINDOW_HOURS),
                        _: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    """Custo por agente por hora e economia do cache, por janela (time series `llm_usage_ts`)."""
    try:
        return await usage_ts.usage_report(store, hours)
    except Exception:  # noqa: BLE001 — leitura falhou: caminho anterior (contadores em processo)
        logger.warning("llm_usage_ts ilegível; devolvendo contadores em processo", exc_info=True)
        return {"source": "in_process", "window_hours": hours, "metrics": metrics.snapshot()}


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
    if "allowed_tools" in update:
        from .tools import TOOLS
        own = {name for name, spec in TOOLS.items() if spec.agent == agent_key}
        unknown = sorted(set(update["allowed_tools"]) - own)
        if unknown and agent_key != "orchestrator":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tools que não pertencem a {agent_key}: {unknown}")
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
async def resolve_pending_review(request: Request, review_id: str, payload: ReviewResolution, store: DataStore = Depends(get_store)):
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
    evidence = resolved.get("evidence") or {}
    if evidence.get("kind") == "hitl_interrupt":
        # Pausa de verdade (LangGraph interrupt): retoma o MESMO thread do checkpoint com a decisão.
        from . import orchestration_graph
        decision = {"approved": payload.decision in ("approve", "approve_replacement", "refund"),
                    "resolved_by": payload.resolved_by, "note": payload.note}
        try:
            # Thread GRAVADA na revisão (uma por turno) + conferência do review_id da pausa pendente.
            output = await orchestration_graph.resume_turn(request.app.state.orchestrator, evidence["thread_id"],
                                                           decision, review_id=review_id)
        except Exception as exc:  # noqa: BLE001 — a decisão humana já está gravada; a retomada pode ser repetida
            log("hitl_resume_failed", review_id=review_id, error=type(exc).__name__)
            output = None
        resolved["resumed_turn"] = output.model_dump(mode="json") if output else None
        if output is None:
            # A cadeia ainda não chegou ao ponto de aprovação: a decisão gravada é lida lá (n_approval).
            resolved["resume_note"] = "decisão gravada; o turno aplica ao chegar no ponto de aprovação"
        if output:
            # Avisa a tela do cliente DEPOIS de a resposta retomada estar gravada (o handoff do revisor,
            # gravado na transação da decisão, chega antes dela).
            await store.insert_one("agent_handoffs", {
                "conversation_id": output.conversation_id, "customer_key": resolved["customer_key"],
                "from_agent": resolved.get("agent") or "order_agent", "to_agent": "orchestrator",
                "reason": "turno retomado após decisão humana", "at": utcnow()})
    return resolved


@app.post("/api/admin/reviews/{review_id}/continue", dependencies=[Depends(require_admin)])
async def continue_review_turn(request: Request, review_id: str, store: DataStore = Depends(get_store)):
    """Repete a retomada de um turno cuja escrita aprovada falhou (o nó `apply_approved` é idempotente)."""
    from . import orchestration_graph
    review = await store.find_one("pending_reviews", {"review_id": review_id})
    thread_id = ((review or {}).get("evidence") or {}).get("thread_id")
    if not review or review.get("status") != "resolved" or not thread_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "revisão resolvida com turno pausado não encontrada")
    try:
        output = await orchestration_graph.continue_thread(request.app.state.orchestrator, thread_id)
        if output is None:
            # Pausa ainda pendente (a retomada da decisão falhou antes de aplicar): reenvia a decisão GRAVADA.
            output = await orchestration_graph.resume_turn(request.app.state.orchestrator, thread_id,
                                                           orchestration_graph.decision_from_review(review),
                                                           review_id=review_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"retomada falhou: {type(exc).__name__}") from exc
    if output is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "nada a retomar nesta thread")
    return output.model_dump(mode="json")


@app.post("/api/admin/guardrails/candidates/{candidate_id}/approve", dependencies=[Depends(require_admin)])
async def approve_candidate(candidate_id: str, store: DataStore = Depends(get_store)):
    candidate_key: object = candidate_id
    try:
        from bson import ObjectId
        candidate_key = ObjectId(candidate_id) if ObjectId.is_valid(candidate_id) else candidate_id
    except ImportError:  # pragma: no cover
        pass
    candidate = await store.find_one("guardrail_candidates", {"_id": candidate_key})
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidato não encontrado")
    phrase = candidate["text"]
    await store.replace_one("guardrail_denylist", {"phrase_norm": phrase.lower()}, {"phrase": phrase, "phrase_norm": phrase.lower(), "active": True}, upsert=True)
    await store.update_one("guardrail_candidates", {"_id": candidate_key}, {"$set": {"status": "approved"}})
    await store.insert_one("admin_audit", {"action": "guardrail.approve", "target": candidate_id, "at": utcnow()})
    return {"ok": True}


@app.get("/api/eval/runs", dependencies=[Depends(require_admin)])
async def eval_runs(store: DataStore = Depends(get_store)):
    """Histórico de qualidade (GoalSuccessRate por caso) — gravado por backend/eval.py a cada execução."""
    items = await store.find_many("eval_runs", {}, limit=30, sort=[("at", -1)])
    return [{key: value for key, value in item.items() if key != "_id"} for item in items]


# ---------------------------------------------------------------- coordenação agêntica (ADR-011/012/013)


@app.get("/api/tools")
async def tool_catalog(_: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Catálogo de tools reais e quais o `agent_registry` libera para cada agente agora."""
    from .tools import TOOLS, tools_for
    agents_docs = await store.find_many("agent_registry", {}, brain=True, limit=20)
    enabled = {doc["agent_key"]: [spec.name for spec in tools_for(doc, doc["agent_key"])] for doc in agents_docs}
    return {"tools": [{"name": spec.name, "agent": spec.agent, "effect": spec.effect, "description": spec.description,
                       "enabled": spec.name in enabled.get(spec.agent, [])} for spec in TOOLS.values()],
            "enabled_by_agent": enabled}


@app.get("/api/graph/topology")
async def graph_topology(_: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Nós e arestas para o grafo ao vivo: agentes do registry + nós do StateGraph do turno."""
    from .orchestration import ALLOWED_REVISITS
    agents_docs = await store.find_many("agent_registry", {}, brain=True, limit=20)
    return {
        "agents": [{"agent_key": d["agent_key"], "label": d.get("label"), "active": d.get("active", False),
                    "model": d.get("model")} for d in agents_docs],
        "graph_nodes": ["ingest", "guardrail", "memory_extract", "fanout_check", "decide", "cache_lookup",
                        "chain_prepare", "agent_hop", "approval", "consolidate", "finalize"],
        "allowed_revisits": [list(pair) for pair in ALLOWED_REVISITS],
    }


@app.get("/api/blackboard/latest")
async def latest_blackboard(conversation_id: str = Query(min_length=4, max_length=80),
                            customer: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    rows = await store.find_many("turn_blackboard", {"conversation_id": conversation_id,
                                                     "customer_key": customer["customer_key"]},
                                 limit=1, sort=[("created_at", -1)])
    return {key: value for key, value in rows[0].items() if key != "_id"} if rows else None


@app.get("/api/memory-typed")
async def typed_memory(customer: dict = Depends(current_customer), store: DataStore = Depends(get_store)):
    """Memória por tipo: semântica (fatos), episódica (episódios), procedural (como o time trabalha)."""
    key = customer["customer_key"]
    facts = await store.find_many("customer_memory", {"customer_key": key, "active": True}, limit=20)
    episodes = await store.find_many("long_term_memory", {"customer_key": key}, limit=10, sort=[("created_at", -1)])
    agents_docs = await store.find_many("agent_registry", {"active": True}, brain=True, limit=20)
    rules = await store.find_many("routing_rules", {}, brain=True, limit=50)
    policies = await store.find_many("hitl_policies", {}, brain=True, limit=20)
    strip = lambda doc: {k: v for k, v in doc.items() if k not in ("_id", "embedding", "embedding_v2")}  # noqa: E731
    return {
        "semantic": {"collection": "customer_memory", "items": [strip(d) for d in facts]},
        "episodic": {"collection": "long_term_memory", "items": [strip(d) for d in episodes]},
        "procedural": {"collections": ["agent_registry", "routing_rules", "hitl_policies"],
                       "agents": [{"agent_key": d["agent_key"], "label": d.get("label"),
                                   "allowed_tools": d.get("allowed_tools")} for d in agents_docs],
                       "routing_rules": len(rules),
                       "hitl_policies": [strip(d) for d in policies]},
    }


@app.get("/api/admin/hitl-policies", dependencies=[Depends(require_admin)])
async def hitl_policies(store: DataStore = Depends(get_store)):
    items = await store.find_many("hitl_policies", {}, brain=True, limit=50)
    return [{k: v for k, v in item.items() if k != "_id"} for item in items]


@app.patch("/api/admin/hitl-policies/{policy_id}", dependencies=[Depends(require_admin)])
async def update_hitl_policy(policy_id: str, payload: HitlPolicyUpdate, store: DataStore = Depends(get_store)):
    changed = await store.update_one("hitl_policies", {"policy_id": policy_id}, {"$set": {"active": payload.active}}, brain=True)
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "política não encontrada")
    await store.insert_one("admin_audit", {"action": "hitl.update", "target": policy_id,
                                           "changes": {"active": payload.active}, "at": utcnow()})
    return {"ok": True, "policy_id": policy_id, "active": payload.active}


@app.post("/api/compare")
async def compare_architectures(request: Request, payload: CompareRequest,
                                customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """A/B single × multi na mesma pergunta, com escritas simuladas e juiz LLM."""
    # Limite próprio: um A/B custa ~2 turnos + 2 juízes.
    if not compare_limiter.allow(request_identity_key(request, f"compare:{customer['customer_key']}")):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite do A/B excedido (3 por minuto)")
    from .budget import TurnBudget
    from .compare import compare
    from .guardrails import check_input
    llm = request.app.state.orchestrator.llm
    orchestrator_doc = await store.find_one("agent_registry", {"agent_key": "orchestrator"}, brain=True)
    from .security import mask_pii
    guard = await check_input(store, mask_pii(payload.message), customer, llm=llm,
                              budget=TurnBudget(20000, {"orchestrator": 20000}),
                              agent_doc={k: v for k, v in (orchestrator_doc or {}).items() if k != "_id"} or None)
    if guard.blocked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mensagem bloqueada pela política de segurança")
    try:
        return await asyncio.wait_for(compare(store, llm, customer, payload.message),
                                      timeout=settings.turn_deadline_seconds * 2)
    except (TimeoutError, BudgetExceeded) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"A/B não concluiu: {type(exc).__name__}") from exc
    except Exception as exc:  # noqa: BLE001 — degradação, nunca 500
        log("compare_failed", error=type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "A/B indisponível agora") from exc


@app.post("/api/demo/crash-resume")
async def demo_crash_resume(request: Request, payload: ChatRequest,
                            customer: Annotated[dict, Depends(current_customer)], store: DataStore = Depends(get_store)):
    """Derruba o turno de propósito antes do 2º hop e retoma do checkpoint no Atlas.

    Prova o que o checkpointer garante: o hop concluído (e sua escrita) não roda de novo."""
    if not settings.demo_token_issuance_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "demo desabilitada")
    if not limiter.allow(request_identity_key(request, f"crash:{customer['customer_key']}")):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "limite de requisições excedido")
    from . import orchestration_graph
    service = request.app.state.orchestrator
    conversation_id = payload.conversation_id or f"conv-{uuid.uuid4().hex[:12]}"
    turn_key = uuid.uuid4().hex[:12]
    thread_id = orchestration_graph.checkpoint_thread_id(customer, conversation_id, turn_key)
    try:
        output = await orchestration_graph.run_turn(service, payload.message, customer, conversation_id,
                                                    crash_before_hop=1, turn_key=turn_key)
        return {"crashed": False, "note": "o turno terminou antes do 2º hop (cadeia de um agente)",
                "result": output.model_dump(mode="json")}
    except orchestration_graph.SimulatedCrash as exc:
        crashed_at = str(exc)
    except Exception as exc:  # noqa: BLE001 — demo nunca vira 500
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"turno falhou: {type(exc).__name__}") from exc
    try:
        graph = orchestration_graph.get_graph(store.settings)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        before = {"next_nodes": list(snapshot.next), "checkpoint_id": snapshot.config["configurable"].get("checkpoint_id"),
                  "hops_done": len((snapshot.values or {}).get("board") or [])}
        output = await orchestration_graph.resume_after_crash(service, thread_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"retomada falhou: {type(exc).__name__}") from exc
    real_conversation = output.conversation_id if output else conversation_id
    handoffs = await store.find_many("agent_handoffs", {"conversation_id": real_conversation}, limit=20)
    pairs = [(h["from_agent"], h["to_agent"]) for h in handoffs]
    return {"crashed": True, "crash": crashed_at, "checkpoint_before_resume": before,
            "handoffs_written": len(handoffs), "duplicated_handoffs": len(pairs) - len(set(pairs)),
            "result": output.model_dump(mode="json") if output else None}


# ---------------------------------------------------------------- A2A (Agent2Agent 1.0): agentes como cards

@app.get("/.well-known/agent-card.json")
async def a2a_agent_card(request: Request, store: DataStore = Depends(get_store)):
    """Card público do serviço coordenado, gerado do `agent_registry` (registry é dado → card é query)."""
    from .agent_cards import system_card
    agents_docs = await store.find_many("agent_registry", {}, brain=True, limit=50)
    return JSONResponse(system_card(agents_docs, settings.public_base_url or str(request.base_url).rstrip("/")))


@app.get("/api/agents/{agent_key}/card")
async def a2a_single_agent_card(agent_key: str, request: Request, store: DataStore = Depends(get_store)):
    from .agent_cards import agent_card
    doc = await store.find_one("agent_registry", {"agent_key": agent_key}, brain=True)
    if not doc or not doc.get("active", True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agente desconhecido ou inativo")
    return JSONResponse(agent_card(doc, settings.public_base_url or str(request.base_url).rstrip("/")))
