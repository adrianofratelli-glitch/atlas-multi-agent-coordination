"""A2A AgentCards and the read-only MCP server, offline (DEMO_MODE store + seed)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from mcp import Client

from app.agent_cards import ROUTES_DOC, agent_card, system_card
from app.config import Settings
from app.database import DataStore
from app.security import issue_token
from app.tools import TOOLS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_server import (BLACKBOARD_URI, HANDOFFS_URI, MCPIdentityError, create_server,  # noqa: E402
                        resolve_identity)
from seed import seed  # noqa: E402

BASE = "https://pov.example"
READ_TOOLS = {name for name, spec in TOOLS.items() if spec.effect == "read"}
SETTINGS = Settings(demo_mode=True)


async def _store() -> DataStore:
    store = DataStore(SETTINGS)
    await seed(store, create_indexes=False)
    return store


async def _agents(store: DataStore) -> list[dict]:
    return await store.find_many("agent_registry", {}, brain=True, limit=50)


# --------------------------------------------------------------------------------------- cards

def _assert_a2a(card: dict) -> None:
    for field in ("name", "description", "supportedInterfaces", "version", "capabilities",
                  "defaultInputModes", "defaultOutputModes", "skills"):
        assert field in card, field
    for legacy in ("url", "protocolVersion", "preferredTransport", "default_input_modes"):
        assert legacy not in card
    iface = card["supportedInterfaces"][0]
    assert iface == {"url": f"{BASE}/api/chat", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}
    assert card["capabilities"]["streaming"] is True and card["capabilities"]["pushNotifications"] is False
    scheme = card["securitySchemes"]["customerJwt"]["httpAuthSecurityScheme"]
    assert scheme["scheme"] == "bearer" and scheme["bearerFormat"] == "JWT"
    assert card["securityRequirements"] == [{"schemes": {"customerJwt": {"list": []}}}]
    assert card["skills"]
    for skill in card["skills"]:
        assert skill["id"] and skill["name"] and skill["description"] and isinstance(skill["tags"], list)
    blob = json.dumps(card)
    for secret_ish in ("jwt_secret", "max_turn_tokens", "claude-", "mongodb+srv"):
        assert secret_ish not in blob


async def test_system_card_has_one_skill_per_active_specialist():
    agents = await _agents(await _store())
    card = system_card(agents, BASE + "/")
    _assert_a2a(card)
    ids = {skill["id"] for skill in card["skills"]}
    assert "orchestrator" not in ids
    assert ids == {a["agent_key"] for a in agents if a["agent_key"] != "orchestrator" and a.get("active")}
    order = next(s for s in card["skills"] if s["id"] == "order_agent")
    assert "get_order" in order["tags"] and "get_order" in order["description"]


async def test_deactivated_agent_is_not_a_skill():
    agents = await _agents(await _store())
    for agent in agents:
        if agent["agent_key"] == "billing_agent":
            agent["active"] = False
    ids = {skill["id"] for skill in system_card(agents, BASE)["skills"]}
    assert "billing_agent" not in ids and "order_agent" in ids


async def test_agent_card_skills_follow_allowed_tools():
    agents = {a["agent_key"]: a for a in await _agents(await _store())}
    card = agent_card(agents["logistics_agent"], BASE)
    _assert_a2a(card)
    assert {s["id"] for s in card["skills"]} == {"get_shipment", "request_delivery_reschedule", "search_carrier_news"}
    narrowed = agent_card({**agents["logistics_agent"], "allowed_tools": ["get_shipment"]}, BASE)
    assert [s["id"] for s in narrowed["skills"]] == ["get_shipment"]
    orchestrator = agent_card(agents["orchestrator"], BASE)
    _assert_a2a(orchestrator)
    assert orchestrator["skills"][0]["id"] == "orchestrator"


def test_routes_doc_names_both_routes():
    assert "/.well-known/agent-card.json" in ROUTES_DOC and "/api/agents/{agent_key}/card" in ROUTES_DOC


# ----------------------------------------------------------------------------------------- MCP

async def _server(store: DataStore, customer_key: str = "ana"):
    return await create_server(store, issue_token(customer_key, SETTINGS), SETTINGS)


async def test_startup_without_token_fails():
    store = await _store()
    with pytest.raises(MCPIdentityError):
        await create_server(store, None, SETTINGS)
    with pytest.raises(MCPIdentityError):
        await resolve_identity(store, "not-a-jwt", SETTINGS)
    forged = jwt.encode({"sub": "ana", "iss": SETTINGS.app_name,
                         "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, "x" * 40, algorithm="HS256")
    with pytest.raises(MCPIdentityError):
        await resolve_identity(store, forged, SETTINGS)


def test_main_refuses_to_start_without_token(monkeypatch):
    import mcp_server

    monkeypatch.delenv("MCP_CUSTOMER_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        mcp_server.main()
    assert exc.value.code == 2


async def test_lists_only_read_tools_released_by_registry():
    store = await _store()
    async with Client(await _server(store)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == READ_TOOLS
        assert not names & {n for n, s in TOOLS.items() if s.effect != "read"}
        # Switching a tool off in the registry hides it on the next listing (no restart).
        await store.update_one("agent_registry", {"agent_key": "order_agent"},
                               {"$set": {"allowed_tools": ["request_order_status_change"]}}, brain=True)
        await store.update_one("agent_registry", {"agent_key": "billing_agent"},
                               {"$set": {"active": False}}, brain=True)
        names = {tool.name for tool in (await client.list_tools(cache_mode="refresh")).tools}
        assert "get_order" not in names and "get_invoice" not in names
        result = await client.call_tool("get_order", {"order_id": "PED-1001"})
        assert result.is_error


async def test_get_order_returns_only_own_order():
    store = await _store()
    async with Client(await _server(store)) as client:
        own = await client.call_tool("get_order", {"order_id": "PED-1001"})
        assert not own.is_error
        assert own.structured_content["order_id"] == "PED-1001"
        assert own.structured_content["owner_customer_key"] == "ana"
        other = await client.call_tool("get_order", {"order_id": "PED-2001"})  # bruno's order
        assert other.structured_content.get("encontrado") is False
        leaked = json.dumps(other.structured_content)
        assert "bruno" not in leaked and "Monitor View" not in leaked


async def test_write_tools_are_refused_and_bad_args_are_errors():
    store = await _store()
    async with Client(await _server(store)) as client:
        write = await client.call_tool("request_order_status_change", {"order_id": "PED-1001", "status": "reembolsado"})
        assert write.is_error
        order = await store.find_one("orders", {"order_id": "PED-1001"})
        assert order["status"] == "enviado"
        bad = await client.call_tool("get_order", {"order_id": "1001; drop"})
        assert bad.is_error


async def test_resources_are_scoped_to_identity():
    store = await _store()
    now = datetime.now(timezone.utc)
    await store.insert_one("agent_handoffs", {"conversation_id": "c-ana", "customer_key": "ana", "from_agent": "order_agent",
                                              "to_agent": "logistics_agent", "reason": "rastreio", "at": now})
    await store.insert_one("agent_handoffs", {"conversation_id": "c-bruno", "customer_key": "bruno", "from_agent": "order_agent",
                                              "to_agent": "billing_agent", "reason": "fatura", "at": now})
    await store.insert_one("turn_blackboard", {"turn_id": "turn-ana", "customer_key": "ana", "conversation_id": "c-ana",
                                               "entries": [], "created_at": now})
    async with Client(await _server(store)) as client:
        handoffs = json.loads((await client.read_resource(HANDOFFS_URI)).contents[0].text)
        assert [h["conversation_id"] for h in handoffs] == ["c-ana"]
        board = json.loads((await client.read_resource(BLACKBOARD_URI)).contents[0].text)
        assert board["turn_id"] == "turn-ana"
