"""A2A 1.0 AgentCards built from the live `agent_registry` (agents are data, so are their cards).

Pure functions: no I/O, no secrets. The caller reads the registry and passes the documents in.
Only public role data leaves here: `label`, `public_description` (never the `persona`, which is part
of the system prompt and editable by an admin), tool
names/descriptions and the active flag. Model names, token budgets, retrieval config and any other
registry field are deliberately left out.

Field names follow A2A 1.0 (camelCase): there is no top-level `url`/`protocolVersion`/
`preferredTransport`; the endpoint lives in `supportedInterfaces`.
"""

from __future__ import annotations

from typing import Any, Iterable

from .tools import ToolSpec, tools_for

PUBLIC_ROLES = {
    "orchestrator": "Plans the turn, routes to specialists and consolidates one answer.",
    "order_agent": "Order status, exchange and refund for the authenticated customer.",
    "product_agent": "Catalog recommendations with semantic search.",
    "support_agent": "Technical diagnosis from the knowledge base; human ticket on request.",
    "billing_agent": "Read-only invoice questions.",
    "warranty_agent": "Warranty coverage and replacement chain.",
    "loyalty_agent": "Loyalty points, tier and reward redemption.",
    "logistics_agent": "Carrier, tracking, delivery estimate and reschedule.",
}


def _public_description(agent: dict) -> str:
    """Texto público do papel: `public_description` do registry ou o papel fixo; nunca a persona."""
    return (agent.get("public_description") or PUBLIC_ROLES.get(agent.get("agent_key"), "")
            or agent.get("label") or "").strip()


A2A_PROTOCOL_VERSION = "1.0"
CARD_VERSION = "1.3.0"
ORCHESTRATOR = "orchestrator"
REPO_URL = "https://github.com/adrianofratelli-glitch/atlas-multi-agent-coordination"
SECURITY_SCHEME = "customerJwt"
TEXT_MODES = ["text/plain"]

ROUTES_DOC = """\
Routes to add to app/main.py (both public, read-only, no auth, no secrets in the payload):

GET /.well-known/agent-card.json
    -> system_card(<active agent_registry docs>, base_url)
    The card of the whole coordinated service: the orchestrator is the single entry point
    (POST /api/chat, Bearer JWT), each ACTIVE specialist is one AgentSkill.

GET /api/agents/{agent_key}/card
    -> agent_card(<registry doc>, base_url); 404 when the agent is unknown or inactive.
    Per-agent card: skills come from that agent's `allowed_tools` (tools.tools_for).

base_url = str(request.base_url).rstrip("/") (scheme + host the client used).
Registry read: store.find_many("agent_registry", {}, brain=True, limit=50).
"""


def _interfaces(base_url: str) -> list[dict]:
    return [{"url": f"{base_url.rstrip('/')}/api/chat", "protocolBinding": "HTTP+JSON",
             "protocolVersion": A2A_PROTOCOL_VERSION}]


def _security() -> tuple[dict, list[dict]]:
    schemes = {SECURITY_SCHEME: {"httpAuthSecurityScheme": {
        "scheme": "bearer", "bearerFormat": "JWT",
        "description": "Customer JWT issued by the service; customer identity comes only from its `sub`.",
    }}}
    return schemes, [{"schemes": {SECURITY_SCHEME: {"list": []}}}]


def _common(base_url: str) -> dict[str, Any]:
    schemes, requirements = _security()
    return {
        "supportedInterfaces": _interfaces(base_url),
        "provider": {"organization": "Atlas Multi-Agent Coordination PoV", "url": REPO_URL},
        "version": CARD_VERSION,
        "documentationUrl": REPO_URL,
        "capabilities": {"streaming": True, "pushNotifications": False, "extendedAgentCard": False},
        "securitySchemes": schemes,
        "securityRequirements": requirements,
        "defaultInputModes": list(TEXT_MODES),
        "defaultOutputModes": list(TEXT_MODES),
    }


def _active(agent: dict) -> bool:
    return bool(agent.get("active", True))


def _tool_tags(specs: Iterable[ToolSpec]) -> list[str]:
    return sorted({spec.effect for spec in specs})


def _specialist_skill(agent: dict) -> dict:
    key = agent["agent_key"]
    specs = tools_for(agent, key)
    persona = _public_description(agent)
    tool_lines = "; ".join(f"{spec.name} ({spec.effect}): {spec.description}" for spec in specs)
    description = persona + (f" Tools: {tool_lines}" if tool_lines else "")
    skill = {
        "id": key,
        "name": agent.get("label") or key,
        "description": description or key,
        "tags": [key, *(spec.name for spec in specs), *_tool_tags(specs)],
        "inputModes": list(TEXT_MODES),
        "outputModes": list(TEXT_MODES),
    }
    if agent.get("examples"):
        skill["examples"] = [str(item) for item in agent["examples"]][:5]
    return skill


def system_card(agents: list[dict], base_url: str) -> dict:
    """Public card of the coordinated service: orchestrator entry point + one skill per active specialist."""
    specialists = [a for a in agents if a.get("agent_key") and a["agent_key"] != ORCHESTRATOR and _active(a)]
    orchestrator = next((a for a in agents if a.get("agent_key") == ORCHESTRATOR), {})
    description = (
        "Multi-agent customer service where MongoDB Atlas is both the data plane and the coordination plane "
        "(routing rules, agent registry, handoffs, memory and guardrails are documents). One entry point: the "
        "orchestrator routes each turn to the specialists below and consolidates one answer. "
        + _public_description(orchestrator)
    ).strip()
    return {
        "name": "Atlas Multi-Agent Coordination",
        "description": description,
        **_common(base_url),
        "skills": [_specialist_skill(agent) for agent in sorted(specialists, key=lambda a: a["agent_key"])],
    }


def agent_card(agent: dict, base_url: str) -> dict:
    """Card of one registry agent. Skills = the tools its `allowed_tools` releases (none -> one role skill).

    The interface is still the orchestrator's `/api/chat`: specialists are not addressable directly, the
    orchestrator routes to them (stated in the description so a client does not expect a private endpoint).
    """
    key = agent["agent_key"]
    specs = tools_for(agent, key)
    skills = [{
        "id": spec.name,
        "name": spec.name,
        "description": spec.description,
        "tags": [key, spec.effect],
        "inputModes": list(TEXT_MODES),
        "outputModes": list(TEXT_MODES),
    } for spec in specs]
    if not skills:
        role = _specialist_skill(agent)
        role["tags"] = [key, "coordination"] if key == ORCHESTRATOR else role["tags"]
        skills = [role]
    persona = _public_description(agent)
    return {
        "name": f"{agent.get('label') or key} ({key})",
        "description": (persona + " Reached through the orchestrator at /api/chat.").strip(),
        **_common(base_url),
        "skills": skills,
    }
