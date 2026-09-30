import asyncio
import copy
import logging
import re
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from pymongo import ASCENDING, AsyncMongoClient

from .chaos import mangle
from .config import Settings
from .resilience import call_tool

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: datetime) -> datetime:
    """Documento antigo/legado pode trazer datetime naive; trata como UTC (é assim que o Mongo grava)."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


_COMPARISONS = {"$gt": lambda a, b: a > b, "$gte": lambda a, b: a >= b,
                "$lt": lambda a, b: a < b, "$lte": lambda a, b: a <= b}


def _compare(operator: str, actual, value) -> bool:
    """Como o Mongo: campo ausente/de outro tipo não casa em comparação (em vez de estourar TypeError)."""
    if actual is None:
        return False
    try:
        return _COMPARISONS[operator](actual, value)
    except TypeError:
        return False


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator in _COMPARISONS and not _compare(operator, actual, value):
                    return False
                if operator == "$exists" and (key in document) != bool(value):
                    return False
                if operator == "$in" and actual not in value:
                    return False
                if operator == "$ne" and actual == value:
                    return False
                if operator == "$regex" and not (isinstance(actual, str) and re.search(value, actual)):
                    return False
        elif actual != expected:
            return False
    return True


class Transaction:
    """Handle de uma transação em curso.

    `driver_session` é a sessão do pymongo, ou `None` quando a atomicidade não vem do driver
    — em `DEMO_MODE` ela é emulada por snapshot, e num mongod standalone simplesmente não
    existe. Por isso o objeto existe sempre: é ele que responde "estou dentro de uma
    transação?", pergunta que `session is None` não conseguia responder.

    `atomic` diz se um rollback é de fato possível neste escopo. Quem grava usa isso para
    escolher entre propagar a exceção (dá para desfazer: propagar É o rollback) e registrar
    a falha sem derrubar o turno (não dá para desfazer: a ação de negócio já aconteceu e
    somar uma tela de erro ao cliente não melhora nada).
    """

    __slots__ = ("driver_session", "atomic")

    def __init__(self, driver_session=None, *, atomic: bool = True):
        self.driver_session = driver_session
        self.atomic = atomic


def is_transient_transaction_error(exc: BaseException) -> bool:
    """True quando o MongoDB pede explicitamente para repetir a operação.

    Duas transações mexendo no MESMO documento produzem `WriteConflict`, e o servidor marca
    o erro com o label `TransientTransactionError` — que significa "tente de novo", não
    "deu errado". Sem tratar isso, dois analistas clicando no mesmo caso recebiam HTTP 500
    em vez do 404 correto ("alguém já resolveu").

    A checagem é pelo label, não pelo código: é o contrato que o driver garante, e cobre
    os outros erros transitórios (eleição de primário, timeout de commit) de graça.
    """
    labels = getattr(exc, "_error_labels", None) or getattr(exc, "error_labels", None) or set()
    if callable(getattr(exc, "has_error_label", None)):
        return bool(exc.has_error_label("TransientTransactionError")
                    or exc.has_error_label("UnknownTransactionCommitResult"))
    return "TransientTransactionError" in labels


def _driver_session(session):
    """Desembrulha o handle para o que o pymongo espera receber em `session=`."""
    return session.driver_session if isinstance(session, Transaction) else session


_T = TypeVar("_T")

RETRY_MAX_ATTEMPTS = 3


async def run_in_transaction_with_retry(
    store: "DataStore", body: Callable[["Transaction"], Awaitable[_T]], *, max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> _T:
    """Roda `body(tx)` dentro de `store.transaction()`, repetindo em `TransientTransactionError`.

    Extraído do padrão que `reviews.py:resolve_review` já usava sozinho: duas transações mexendo no
    MESMO documento (ex. duplo clique em "resgatar pontos") produzem `WriteConflict`/
    `TransientTransactionError` — que significa "tente de novo", não "deu errado". Sem repetir,
    isso sobe cru até o handler genérico de erro e vira um HTTP 500 em vez de simplesmente
    convergir na segunda tentativa. `body` deve ser idempotente o bastante para rodar mais de uma
    vez (ela recebe uma transação NOVA a cada tentativa — nunca reaproveita uma sessão abortada).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with store.transaction() as tx:
                return await body(tx)
        except Exception as exc:
            last_exc = exc
            if not is_transient_transaction_error(exc) or attempt == max_attempts:
                raise
            logger.info("conflito transitório na transação (tentativa %d) — repetindo", attempt)
            await asyncio.sleep(0.05 * attempt)
    raise last_exc  # pragma: no cover — inalcançável: o loop sempre raise/retorna antes


class DataStore:
    """Uma porta pequena para Atlas com fallback determinístico para testes locais."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.memory = settings.use_memory_store
        self.client: AsyncMongoClient | None = None
        self._data: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        self._lock = asyncio.Lock()
        # Transação multi-documento exige replica set (ou cluster shardeado). Todo Atlas é
        # replica set, mas um mongod standalone local não é — e ali a escrita precisa
        # continuar acontecendo, só que sem atomicidade e dizendo isso em voz alta.
        self._transactions_available = False

    async def connect(self) -> None:
        if self.memory:
            return
        self.client = AsyncMongoClient(
            self.settings.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            appname=self.settings.app_name,
            # datetimes lidos voltam AWARE (UTC), como utcnow(): sem isso o PyMongo devolve naive e qualquer
            # comparação com utcnow() estoura TypeError — só em modo live, nunca em DEMO_MODE.
            tz_aware=True,
        )
        hello = await self.client.admin.command("hello")
        # setName = replica set; "isdbgrid" = mongos à frente de um cluster shardeado.
        self._transactions_available = bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
        if not self._transactions_available:
            logger.warning("servidor sem suporte a transação multi-documento (standalone): "
                           "escrita de negócio e decisão serão sequenciais, sem atomicidade")

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    def _collection(self, name: str, brain: bool = False):
        if not self.client:
            raise RuntimeError("cliente MongoDB não conectado")
        db_name = self.settings.mongodb_brain_db if brain else self.settings.mongodb_db
        return self.client[db_name][name]

    def _bucket(self, name: str, brain: bool = False) -> list[dict]:
        db_name = self.settings.mongodb_brain_db if brain else self.settings.mongodb_db
        return self._data[db_name][name]

    async def ping(self) -> bool:
        if self.memory:
            return True
        await self.client.admin.command("ping")  # type: ignore[union-attr]
        return True

    @asynccontextmanager
    async def transaction(self):
        """Escrita de negócio e registro de decisão como uma coisa só.

        O problema que isto resolve: mudar o status de um pedido e gravar a decisão que o
        justifica eram dois `await` em sequência. Uma falha entre os dois deixava o mundo
        alterado sem registro — o furo que a trilha de auditoria existe para impedir, aberto
        exatamente no meio dela. Com a transação, ou as duas acontecem, ou nenhuma.

        Devolve um handle de sessão para passar adiante (`session=`), ou `None` quando não
        há transação disponível. `None` é um caminho legítimo, não um erro: em `DEMO_MODE`
        a atomicidade é emulada por snapshot, e num mongod standalone a escrita continua
        acontecendo de forma sequencial, com o aviso já emitido no connect.

        Importante: todas as collections envolvidas precisam viver no MESMO banco. É o caso
        de `orders`/`loyalty_accounts`/`shipments` e `agent_decisions`/`agent_audit_events`;
        o banco `multiagent_brain` (configuração) fica de fora, e não participa de escrita.
        """
        if self.memory:
            # Emulação por snapshot: dá all-or-nothing de verdade em DEMO_MODE/CI, então o
            # teste de rollback testa comportamento, não um no-op que sempre passa.
            async with self._lock:
                snapshot = copy.deepcopy(self._data)
            try:
                yield Transaction(None, atomic=True)
            except Exception:
                async with self._lock:
                    self._data.clear()
                    for db_name, collections in snapshot.items():
                        for collection, documents in collections.items():
                            self._data[db_name][collection] = documents
                raise
            return

        if not self._transactions_available:
            # Sem replica set não há como desfazer: `atomic=False` avisa quem grava para
            # não propagar uma exceção que ninguém consegue reverter.
            yield Transaction(None, atomic=False)
            return

        async with self.client.start_session() as session:  # type: ignore[union-attr]
            async with await session.start_transaction():
                yield Transaction(session, atomic=True)

    async def find_one(self, name: str, query: dict, *, brain: bool = False, session=None) -> dict | None:
        return mangle("tool", f"{name}.find_one", await call_tool(
            f"{name}.find_one", self._find_one(name, query, brain=brain, session=session),
            **{"db.collection": name, "db.op": "find_one"}))

    async def _find_one(self, name: str, query: dict, *, brain: bool = False, session=None) -> dict | None:
        if not self.memory:
            return await self._collection(name, brain).find_one(query, session=_driver_session(session))
        async with self._lock:
            return next((copy.deepcopy(d) for d in self._bucket(name, brain) if _matches(d, query)), None)

    async def find_many(
        self,
        name: str,
        query: dict | None = None,
        *,
        brain: bool = False,
        limit: int = 100,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict]:
        return mangle("tool", f"{name}.find_many", await call_tool(
            f"{name}.find_many", self._find_many(name, query, brain=brain, limit=limit, sort=sort),
            **{"db.collection": name, "db.op": "find"}))

    async def _find_many(
        self,
        name: str,
        query: dict | None = None,
        *,
        brain: bool = False,
        limit: int = 100,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict]:
        query = query or {}
        if not self.memory:
            cursor = self._collection(name, brain).find(query)
            if sort:
                cursor = cursor.sort(sort)
            return await cursor.to_list(length=limit)
        async with self._lock:
            items = [copy.deepcopy(d) for d in self._bucket(name, brain) if _matches(d, query)]
        if sort:
            for field, direction in reversed(sort):
                items.sort(key=lambda item: item.get(field) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction < 0)
        return items[:limit]

    async def count(self, name: str, query: dict | None = None, *, brain: bool = False) -> int:
        if not self.memory:
            if not query:
                # Contagem sem filtro (health/painéis): metadata da collection,
                # O(1) — count_documents({}) faz scan completo.
                return await self._collection(name, brain).estimated_document_count()
            return await self._collection(name, brain).count_documents(query)
        return len(await self.find_many(name, query, brain=brain, limit=100_000))

    async def insert_one(self, name: str, document: dict, *, brain: bool = False, session=None) -> None:
        await call_tool(f"{name}.insert_one", self._insert_one(name, document, brain=brain, session=session),
                        **{"db.collection": name, "db.op": "insert"})

    async def _insert_one(self, name: str, document: dict, *, brain: bool = False, session=None) -> None:
        payload = copy.deepcopy(document)
        if not self.memory:
            await self._collection(name, brain).insert_one(payload, session=_driver_session(session))
            return
        async with self._lock:
            payload.setdefault("_id", f"{name}-{len(self._bucket(name, brain)) + 1}")
            self._bucket(name, brain).append(payload)

    async def replace_one(
        self, name: str, query: dict, document: dict, *, brain: bool = False, upsert: bool = False
    ) -> None:
        payload = copy.deepcopy(document)
        if not self.memory:
            await self._collection(name, brain).replace_one(query, payload, upsert=upsert)
            return
        async with self._lock:
            bucket = self._bucket(name, brain)
            for index, item in enumerate(bucket):
                if _matches(item, query):
                    payload.setdefault("_id", item.get("_id"))
                    bucket[index] = payload
                    return
            if upsert:
                payload.setdefault("_id", f"{name}-{len(bucket) + 1}")
                bucket.append(payload)

    async def update_one(
        self, name: str, query: dict, update: dict, *, brain: bool = False, upsert: bool = False,
        session=None,
    ) -> int:
        return await call_tool(
            f"{name}.update_one", self._update_one(name, query, update, brain=brain, upsert=upsert, session=session),
            **{"db.collection": name, "db.op": "update"})

    async def _update_one(
        self, name: str, query: dict, update: dict, *, brain: bool = False, upsert: bool = False,
        session=None,
    ) -> int:
        if not self.memory:
            result = await self._collection(name, brain).update_one(
                query, update, upsert=upsert, session=_driver_session(session))
            return result.modified_count
        async with self._lock:
            bucket = self._bucket(name, brain)
            target = next((item for item in bucket if _matches(item, query)), None)
            was_insert = target is None
            if target is None and upsert:
                target = {**query, "_id": f"{name}-{len(bucket) + 1}"}
                bucket.append(target)
            if target is None:
                return 0
            if was_insert:
                for key, value in update.get("$setOnInsert", {}).items():
                    target.setdefault(key, copy.deepcopy(value))
            for key, value in update.get("$set", {}).items():
                target[key] = copy.deepcopy(value)
            for key in update.get("$unset", {}):
                target.pop(key, None)
            for key, value in update.get("$inc", {}).items():
                target[key] = target.get(key, 0) + value
            for key, value in update.get("$push", {}).items():
                array = target.setdefault(key, [])
                if isinstance(value, dict) and "$each" in value:
                    # Emula o $push com $each/$slice do driver real: append de vários itens
                    # de uma vez seguido de um corte para o final do array (slice negativo é o
                    # único caso que o orchestration.py usa, para limitar o tamanho do histórico).
                    array.extend(copy.deepcopy(item) for item in value["$each"])
                    slice_spec = value.get("$slice")
                    if isinstance(slice_spec, int):
                        target[key] = array[slice_spec:] if slice_spec < 0 else array[:slice_spec]
                else:
                    array.append(copy.deepcopy(value))
            return 1

    async def aggregate(self, name: str, pipeline: list[dict], *, brain: bool = False) -> list[dict]:
        return mangle("tool", f"{name}.aggregate", await call_tool(
            f"{name}.aggregate", self._aggregate(name, pipeline, brain=brain),
            **{"db.collection": name, "db.op": "aggregate"}))

    async def _aggregate(self, name: str, pipeline: list[dict], *, brain: bool = False) -> list[dict]:
        """Só para pipelines reais ($vectorSearch/$unionWith) — sem equivalente em DEMO_MODE, chamador
        precisa ter um caminho alternativo quando self.memory é True (ver cascade.py)."""
        if self.memory:
            raise RuntimeError("aggregate() não tem fallback em DEMO_MODE — trate store.memory antes de chamar")
        cursor = await self._collection(name, brain).aggregate(pipeline)
        return await cursor.to_list(length=None)

    async def delete_many(self, name: str, query: dict, *, brain: bool = False) -> int:
        if not self.memory:
            result = await self._collection(name, brain).delete_many(query)
            return result.deleted_count
        async with self._lock:
            bucket = self._bucket(name, brain)
            kept = [item for item in bucket if not _matches(item, query)]
            deleted = len(bucket) - len(kept)
            bucket[:] = kept
            return deleted

    async def watch_handoffs(self, customer_key: str):
        """Live feed de handoffs do cliente: Change Stream no Atlas, poll no modo DEMO_MODE."""
        if not self.memory:
            try:
                # customer_key denormalizado no handoff: o filtro de dono roda no
                # próprio Change Stream (server-side), sem find_one por evento.
                # Docs antigos sem o campo caem no fallback app-side abaixo.
                stream = await self._collection("agent_handoffs").watch(
                    [{"$match": {"operationType": "insert", "$or": [
                        {"fullDocument.customer_key": customer_key},
                        {"fullDocument.customer_key": {"$exists": False}},
                    ]}}],
                    full_document="updateLookup",
                )
                async with stream:
                    async for change in stream:
                        doc = change["fullDocument"]
                        if doc.get("customer_key") is None:  # legado: valida dono via conversa
                            owner = await self.find_one("agent_conversations", {"conversation_id": doc.get("conversation_id"), "customer_key": customer_key})
                            if not owner:
                                continue
                        yield {key: value for key, value in doc.items() if key != "_id"}
                return
            except Exception:
                pass
        seen: set[str] = set()
        seen_order: deque[str] = deque()
        seen_limit = 1000
        while True:
            await asyncio.sleep(1.2)
            items = await self.find_many("agent_handoffs", {}, limit=200, sort=[("at", 1)])
            for item in items:
                marker = str(item.get("_id"))
                if marker in seen:
                    continue
                seen.add(marker)
                seen_order.append(marker)
                if len(seen_order) > seen_limit:
                    seen.discard(seen_order.popleft())
                owner = await self.find_one("agent_conversations", {"conversation_id": item.get("conversation_id"), "customer_key": customer_key})
                if owner:
                    yield {key: value for key, value in item.items() if key != "_id"}

    async def create_schema_validators(self) -> list[str]:
        """Schema-at-boundary: cada handoff/trace é validado pelo próprio MongoDB, não só pela camada Python —
        o mesmo controle que arquiteturas AWS multi-agent pedem serviço externo (schema registry) pra garantir."""
        if self.memory:
            return ["JSON Schema validators: ignorados em DEMO_MODE"]
        validators = {
            "agent_handoffs": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["conversation_id", "from_agent", "to_agent", "reason", "at"],
                    "properties": {
                        "conversation_id": {"bsonType": "string", "minLength": 4},
                        "customer_key": {"bsonType": "string"},
                        "from_agent": {"bsonType": "string"},
                        "to_agent": {"bsonType": "string"},
                        "reason": {"bsonType": ["string", "null"]},
                        "at": {"bsonType": "date"},
                    },
                }
            },
            "pending_reviews": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["review_id", "agent", "action", "subject_id", "customer_key", "recommended_action", "status", "created_at"],
                    "properties": {
                        "review_id": {"bsonType": "string", "pattern": "^REV-[0-9A-F]{10}$"},
                        "agent": {"bsonType": "string"},
                        "customer_key": {"bsonType": "string", "minLength": 1},
                        "status": {"enum": ["pending", "resolved", "expired"]},
                        "recommended_action": {"bsonType": "string", "minLength": 3},
                        "human_decision": {"bsonType": ["string", "null"]},
                        "overrode_agent": {"bsonType": ["bool", "null"]},
                        "created_at": {"bsonType": "date"},
                    },
                }
            },
            "agent_decisions": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["decision_id", "action", "subject_id", "customer_key", "agent", "decided_by", "reasoning", "at"],
                    "properties": {
                        "decision_id": {"bsonType": "string", "pattern": "^DEC-[0-9A-F]{12}$"},
                        "action": {"bsonType": "string", "minLength": 3},
                        "subject_id": {"bsonType": "string"},
                        "customer_key": {"bsonType": "string", "minLength": 1},
                        "agent": {"bsonType": "string"},
                        "decided_by": {"enum": ["agent", "human"]},
                        "reasoning": {"bsonType": "string", "minLength": 3},
                        "confidence": {"bsonType": ["double", "int", "null"], "minimum": 0, "maximum": 1},
                        "risk_factors": {"bsonType": "array", "items": {"bsonType": "string"}},
                        "escalated": {"bsonType": "bool"},
                        "supersedes": {"bsonType": ["string", "null"]},
                        "at": {"bsonType": "date"},
                    },
                }
            },
            "agent_audit_events": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["event_id", "event_type", "customer_key", "severity", "at"],
                    "properties": {
                        "event_id": {"bsonType": "string", "pattern": "^AUD-[0-9A-F]{12}$"},
                        "event_type": {"bsonType": "string", "minLength": 3},
                        "decision_id": {"bsonType": ["string", "null"]},
                        "customer_key": {"bsonType": "string", "minLength": 1},
                        "severity": {"enum": ["info", "warning", "critical"]},
                        "at": {"bsonType": "date"},
                    },
                }
            },
            "agent_traces": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["conversation_id", "customer_key", "active_agent", "at"],
                    "properties": {
                        "conversation_id": {"bsonType": "string"},
                        "customer_key": {"bsonType": "string"},
                        "active_agent": {"bsonType": "string"},
                        "at": {"bsonType": "date"},
                    },
                }
            },
        }
        messages: list[str] = []
        db = self.client[self.settings.mongodb_db]
        for collection, validator in validators.items():
            try:
                await db.command("collMod", collection, validator=validator, validationLevel="moderate", validationAction="error")
                messages.append(f"{collection}: validator aplicado (collMod)")
            except Exception:
                try:
                    await db.create_collection(collection, validator=validator, validationLevel="moderate", validationAction="error")
                    messages.append(f"{collection}: validator aplicado (create_collection)")
                except Exception as exc:
                    messages.append(f"{collection}: validator best-effort ({exc})")
        return messages

    async def create_standard_indexes(self) -> None:
        if self.memory:
            return
        definitions = {
            "customers": [[("customer_key", ASCENDING)]],
            # 3º índice: $graphLookup casa replacement_order_id -> order_id a cada salto;
            # sem índice em connectToField a travessia vira collection scan por salto.
            "orders": [[("order_id", ASCENDING)], [("owner_customer_key", ASCENDING), ("status", ASCENDING)], [("owner_customer_key", ASCENDING), ("replacement_order_id", ASCENDING)]],
            "invoices": [[("invoice_id", ASCENDING)], [("owner_customer_key", ASCENDING), ("due_date", ASCENDING)]],
            "loyalty_accounts": [[("customer_key", ASCENDING)]],
            "shipments": [[("order_id", ASCENDING)], [("owner_customer_key", ASCENDING)]],
            "warranty_policies": [[("category", ASCENDING)]],
            "agent_conversations": [[("conversation_id", ASCENDING)], [("updated_at", ASCENDING)]],
            "customer_memory": [[("customer_key", ASCENDING), ("active", ASCENDING)]],
            "agent_handoffs": [[("conversation_id", ASCENDING), ("at", ASCENDING)], [("at", ASCENDING)]],
            "agent_traces": [[("conversation_id", ASCENDING), ("at", ASCENDING)], [("at", ASCENDING)]],
            "semantic_cache": [[("agent", ASCENDING), ("area", ASCENDING)], [("expires_at", ASCENDING)]],
            "short_term_memory": [[("session_id", ASCENDING)], [("expires_at", ASCENDING)]],
            "long_term_memory": [[("customer_key", ASCENDING)]],
            "guardrail_denylist": [[("phrase_norm", ASCENDING)]],
            "guardrail_events": [[("at", ASCENDING)]],
            "guardrail_candidates": [[("status", ASCENDING), ("created_at", ASCENDING)]],
            "admin_audit": [[("at", ASCENDING)]],
            "eval_runs": [[("at", ASCENDING)]],
            "support_tickets": [[("customer_key", ASCENDING), ("created_at", ASCENDING)]],
            "redemptions": [[("customer_key", ASCENDING), ("at", ASCENDING)]],
            # Sem TTL de propósito: agent_traces/agent_handoffs são observabilidade e expiram em
            # 30 dias; decisão e trilha de auditoria são registro de conformidade e ficam.
            # Idempotência da pausa: (subject_id, action, status) é o que open_review consulta
            # antes de abrir, para o analista não receber o mesmo caso duas vezes.
            "pending_reviews": [[("review_id", ASCENDING)], [("subject_id", ASCENDING), ("action", ASCENDING), ("status", ASCENDING)], [("status", ASCENDING), ("created_at", ASCENDING)]],
            "agent_decisions": [[("decision_id", ASCENDING)], [("customer_key", ASCENDING), ("at", ASCENDING)], [("subject_id", ASCENDING), ("at", ASCENDING)]],
            "agent_audit_events": [[("decision_id", ASCENDING)], [("customer_key", ASCENDING), ("at", ASCENDING)], [("subject_id", ASCENDING), ("at", ASCENDING)]],
        }
        unique = {("customers", 0), ("orders", 0), ("agent_decisions", 0), ("agent_audit_events", 0), ("pending_reviews", 0), ("invoices", 0), ("agent_conversations", 0), ("guardrail_denylist", 0), ("loyalty_accounts", 0), ("shipments", 0), ("warranty_policies", 0)}
        ttl = {
            ("agent_conversations", 1): 86400,
            ("agent_handoffs", 1): 30 * 86400,
            ("agent_traces", 1): 30 * 86400,
            ("semantic_cache", 1): 0,
            ("short_term_memory", 1): 0,
            ("guardrail_events", 0): 30 * 86400,
            ("admin_audit", 0): 30 * 86400,
            ("eval_runs", 0): 90 * 86400,
        }
        for collection, indexes in definitions.items():
            for position, keys in enumerate(indexes):
                options: dict[str, Any] = {}
                if (collection, position) in unique:
                    options["unique"] = True
                if (collection, position) in ttl:
                    options["expireAfterSeconds"] = ttl[(collection, position)]
                await self._collection(collection).create_index(keys, **options)


store: DataStore | None = None


def set_store(value: DataStore) -> None:
    global store
    store = value


def get_store() -> DataStore:
    if store is None:
        raise RuntimeError("DataStore ainda não inicializado")
    return store
