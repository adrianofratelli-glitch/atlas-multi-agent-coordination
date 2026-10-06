"""Checkpoints do LangGraph (MongoDBSaver na demo, MemorySaver aqui) isolados por cliente.

Antes: `thread_id = conversation_id`. Bruno mandando o conversation_id da Ana carregava o
checkpoint dela no turno dele e gravava um checkpoint novo na thread dela.
"""

from app import orchestration_graph as og
from app.config import Settings
from app.database import DataStore
from app.llm import LLMGateway
from app.orchestration import OrchestrationService
from seed import seed

ANA = {"customer_key": "ana", "area": "varejo", "name": "Ana", "plan": "premium"}
BRUNO = {"customer_key": "bruno", "area": "varejo", "name": "Bruno", "plan": "essencial"}


async def _service():
    settings = Settings(demo_mode=True)
    store = DataStore(settings)
    await store.connect()
    await seed(store, create_indexes=False)
    return OrchestrationService(store, LLMGateway(settings), global_budget=20000), settings


def _latest(settings, customer, conversation_id):
    graph = og.get_graph(settings)
    snapshot = graph.get_state({"configurable": {"thread_id": og.checkpoint_thread_id(customer, conversation_id)}})
    return snapshot.values


async def test_foreign_conversation_id_never_touches_the_owner_checkpoint():
    service, settings = await _service()
    ana_turn = await service.run_turn("onde está o meu pedido PED-1001?", ANA, None)
    conv = ana_turn.conversation_id
    before = _latest(settings, ANA, conv)
    assert before["customer"]["customer_key"] == "ana"

    bruno_turn = await service.run_turn("onde está o meu pedido PED-2001?", BRUNO, conv)
    assert bruno_turn.conversation_id != conv, "id alheio é trocado por uma conversa nova"
    after = _latest(settings, ANA, conv)
    assert after["customer"]["customer_key"] == "ana"
    assert after["masked"] == before["masked"], "o turno do Bruno não pode gravar na thread da Ana"
    assert "PED-1001" not in str(_latest(settings, BRUNO, conv).get("masked", ""))


def test_checkpointer_has_ttl_aligned_with_conversations():
    assert og.CHECKPOINT_TTL_SECONDS == 86400
