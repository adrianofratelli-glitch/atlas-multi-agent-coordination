from datetime import timedelta

from app.cascade import cascade_lookup, cascade_store_turn
from app.config import Settings
from app.database import DataStore, utcnow
from app.llm import LLMGateway
from app.orchestration import OrchestrationService


async def test_personalized_intent_stays_session_scoped_and_never_leaks():
    store = DataStore(Settings(demo_mode=True))
    await cascade_store_turn(store, target="order_agent", area="varejo", customer_key="ana", session_id="s1", intent="status_pedido", message="onde está meu pedido", answer="resposta da Ana", timeline=[], active_agent="order_agent")

    same_session = await cascade_lookup(store, target="order_agent", area="varejo", customer_key="ana", session_id="s1", message="onde está meu pedido")
    assert same_session.hit and same_session.fonte == "curto_prazo"

    same_customer_new_session = await cascade_lookup(store, target="order_agent", area="varejo", customer_key="ana", session_id="s2", message="onde está meu pedido")
    assert same_customer_new_session.hit is False

    other_session = await cascade_lookup(store, target="order_agent", area="varejo", customer_key="bruno", session_id="s2", message="onde está meu pedido")
    assert other_session.hit is False


async def test_short_term_cache_requires_customer_even_when_session_id_matches():
    store = DataStore(Settings(demo_mode=True))
    await cascade_store_turn(store, target="order_agent", area="varejo", customer_key="ana", session_id="shared-id", intent="status_pedido", message="onde está meu pedido", answer="resposta da Ana", timeline=[], active_agent="order_agent")

    leaked = await cascade_lookup(store, target="order_agent", area="varejo", customer_key="bruno", session_id="shared-id", message="onde está meu pedido")
    assert leaked.hit is False


async def test_global_cache_requires_explicit_safe_eligibility():
    store = DataStore(Settings(demo_mode=True))
    await cascade_store_turn(store, target="support_agent", area="varejo", customer_key="ana", session_id="s1", intent="suporte", message="como parear bluetooth", answer="orientação pública", timeline=[], active_agent="support_agent")

    without_approval = await cascade_lookup(store, target="support_agent", area="varejo", customer_key="bruno", session_id="s2", message="como parear bluetooth")
    assert without_approval.hit is False

    await cascade_store_turn(store, target="support_agent", area="varejo", customer_key="ana", session_id="s1", intent="suporte", message="como redefinir bluetooth", answer="orientação pública", timeline=[], active_agent="support_agent", cache_eligible=True)
    approved = await cascade_lookup(store, target="support_agent", area="varejo", customer_key="bruno", session_id="s2", message="como redefinir bluetooth")
    assert approved.hit and approved.fonte == "cache"


async def test_warranty_is_never_global_even_when_caller_requests_it():
    store = DataStore(Settings(demo_mode=True))
    await cascade_store_turn(store, target="warranty_agent", area="varejo", customer_key="ana", session_id="s1", intent="garantia", message="meu pedido está na garantia?", answer="resposta da Ana", timeline=[], active_agent="warranty_agent", cache_eligible=True)

    same_customer_new_session = await cascade_lookup(store, target="warranty_agent", area="varejo", customer_key="ana", session_id="s2", message="meu pedido está na garantia?")
    assert same_customer_new_session.hit is False

    leaked = await cascade_lookup(store, target="warranty_agent", area="varejo", customer_key="bruno", session_id="s2", message="meu pedido está na garantia?")
    assert leaked.hit is False


async def test_cache_entry_from_previous_unrestricted_policy_is_ignored():
    store = DataStore(Settings(demo_mode=True))
    await store.insert_one(
        "semantic_cache",
        {
            "agent": "order_agent",
            "area": "varejo",
            "customer_key": "ana",
            "scope": "customer",
            "question_text": "onde está meu pedido",
            "question_norm": "onde esta meu pedido",
            "answer": "estado possivelmente antigo",
            "expires_at": utcnow() + timedelta(hours=1),
        },
    )

    result = await cascade_lookup(store, target="order_agent", area="varejo", customer_key="ana", session_id="s2", message="onde está meu pedido")
    assert result.hit is False


async def test_foreign_conversation_id_is_replaced_instead_of_hijacked():
    settings = Settings(demo_mode=True, anthropic_api_key="")
    store = DataStore(settings)
    await store.insert_one(
        "agent_registry",
        {
            "agent_key": "order_agent",
            "label": "Pedidos",
            "model": "demo",
            "persona": "Pedidos",
            "allowed_tools": ["read_order"],
            "max_turn_tokens": 1000,
            "active": True,
        },
        brain=True,
    )
    await store.insert_one(
        "routing_rules",
        {"intent": "status_pedido", "keywords": ["PED-"], "target_agent": "order_agent", "priority": 100},
        brain=True,
    )
    await store.insert_one(
        "orders",
        {
            "order_id": "PED-2001",
            "owner_customer_key": "bruno",
            "product": "Monitor",
            "status": "processando",
            "timeline": [],
        },
    )
    await store.insert_one(
        "agent_conversations",
        {
            "conversation_id": "conv-da-ana",
            "customer_key": "ana",
            "turns": [{"role": "user", "content": "segredo da Ana"}],
            "active_agent": "order_agent",
        },
    )

    service = OrchestrationService(store, LLMGateway(settings), global_budget=6000)
    response = await service.run_turn(
        "PED-2001",
        {"customer_key": "bruno", "area": "varejo", "name": "Bruno", "plan": "essencial"},
        "conv-da-ana",
    )

    assert response.conversation_id != "conv-da-ana"
    original = await store.find_one("agent_conversations", {"conversation_id": "conv-da-ana"})
    assert original["customer_key"] == "ana"
    assert original["turns"][0]["content"] == "segredo da Ana"


async def test_atlas_index_drift_falls_back_without_breaking_the_turn():
    class IndexBuildingStore:
        memory = False

        async def aggregate(self, *_args, **_kwargs):
            raise RuntimeError("filter path ainda não disponível no índice")

        async def find_one(self, *_args, **_kwargs):
            return None

    result = await cascade_lookup(
        IndexBuildingStore(),
        target="order_agent",
        area="varejo",
        customer_key="ana",
        session_id="conv-1",
        message="onde está meu pedido",
    )

    assert result.hit is False
