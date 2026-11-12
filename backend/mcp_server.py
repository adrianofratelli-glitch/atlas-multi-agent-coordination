"""MCP server (stdio) exposing the READ-ONLY specialist tools to other agents.

Run (identity is mandatory; see README "Servidor MCP"):

    MCP_CUSTOMER_TOKEN=<JWT from POST /api/auth/token> python backend/mcp_server.py

What is exposed and why:

- Tools: only `effect == "read"` entries of `app.tools.TOOLS`, filtered LIVE by the
  `agent_registry` (agent `active` + its `allowed_tools`). Switching a tool or an agent off in the
  registry hides it from `tools/list` and refuses `tools/call` on the next request, no restart.
- Write tools (status change, reward redemption, reschedule, support ticket) are NOT exposed. Their
  server gate requires the explicit intent in the customer's OWN message (`tools._write_gate`); an
  MCP client is another agent, so there is no customer message to check and the gate would either
  always refuse or have to trust model-written text. External tools (web search) stay out too:
  they are only allowed inside a turn with an overdue delivery (ADR-005).
- Identity: `customer_key` comes ONLY from the verified JWT in `MCP_CUSTOMER_TOKEN` (same check as
  the HTTP API, `security.decode_customer_key`), re-verified on every call so an expired token stops
  working. Tool arguments never carry identity; executors rebuild the owner filter (`policies.py`).
- Resources: recent `agent_handoffs` and the latest `turn_blackboard` of that identity.

Data comes from the same `DataStore` as the API. In DEMO_MODE (no `MONGODB_URI`) the process seeds its
own in-memory store, so it does not see writes made by a separately running API process.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import jwt
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from app.config import Settings, get_settings
from app.database import DataStore
from app.policies import public_document
from app.security import decode_customer_key
from app.tools import TOOLS, ToolContext, ToolSpec, execute, tools_for

TOKEN_ENV = "MCP_CUSTOMER_TOKEN"
HANDOFFS_URI = "coordination://handoffs/recent"
BLACKBOARD_URI = "coordination://blackboard/latest"
HANDOFF_LIMIT = 20


class MCPIdentityError(RuntimeError):
    """Raised when the server cannot establish the customer identity from the JWT."""


def _json(value: Any) -> Any:
    """Round-trip through JSON so datetimes/ObjectIds become strings (MCP payloads are plain JSON)."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _error(message: str) -> CallToolResult:
    payload = {"erro": message}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          structured_content=payload, is_error=True)


async def resolve_identity(store: DataStore, token: str | None, settings: Settings) -> dict:
    """Customer document for the JWT. Refuses missing, invalid, expired or unknown identities."""
    if not token:
        raise MCPIdentityError(f"{TOKEN_ENV} is required: the MCP server never runs without a customer identity")
    try:
        customer_key = decode_customer_key(token, settings)
    except jwt.PyJWTError as exc:
        raise MCPIdentityError(f"{TOKEN_ENV} rejected: {type(exc).__name__}") from exc
    customer = await store.find_one("customers", {"customer_key": customer_key})
    if not customer:
        raise MCPIdentityError("unknown identity in token")
    return customer


class CoordinationMCPServer(MCPServer):
    """MCPServer whose tool list is computed from the live registry on every request."""

    def __init__(self, store: DataStore, customer: dict, token: str, settings: Settings):
        super().__init__(
            name="atlas-multi-agent-coordination",
            instructions=("Read-only tools of the multi-agent customer service, scoped to ONE authenticated "
                          "customer. Writes are not available here: they require the customer's own request "
                          "in the chat (/api/chat)."),
            version="1.3.0",
        )
        self.store = store
        self.customer = customer
        self._token = token
        self._settings = settings
        self.resource(HANDOFFS_URI, name="recent_handoffs", mime_type="application/json",
                      description="Recent agent handoffs of the authenticated customer")(self._handoffs)
        self.resource(BLACKBOARD_URI, name="latest_blackboard", mime_type="application/json",
                      description="Latest turn blackboard (what each agent did) of the authenticated customer")(
            self._blackboard)

    # ----------------------------------------------------------------------------------- identity

    def _identity_ok(self) -> bool:
        try:
            return decode_customer_key(self._token, self._settings) == self.customer["customer_key"]
        except jwt.PyJWTError:
            return False

    # -------------------------------------------------------------------------------------- tools

    async def exposed(self) -> dict[str, tuple[ToolSpec, dict]]:
        """name -> (spec, registry doc) for read tools released by ACTIVE agents' `allowed_tools`."""
        agents = await self.store.find_many("agent_registry", {"active": True}, brain=True, limit=50)
        out: dict[str, tuple[ToolSpec, dict]] = {}
        for doc in agents:
            for spec in tools_for(doc, doc.get("agent_key", "")):
                if spec.effect == "read":
                    out[spec.name] = (spec, doc)
        return out

    async def list_tools(self) -> list[Tool]:
        exposed = await self.exposed()
        return [Tool(name=spec.name, description=spec.description, input_schema=spec.input_schema,
                     annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                 idempotent_hint=True, open_world_hint=False))
                for spec, _ in sorted(exposed.values(), key=lambda item: item[0].name)]

    def _tool_input_schema(self, name: str) -> dict[str, Any] | None:
        spec = TOOLS.get(name)
        return spec.input_schema if spec and spec.effect == "read" else None

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None) -> CallToolResult:
        if not self._identity_ok():
            return _error("token expirado ou inválido; gere um novo MCP_CUSTOMER_TOKEN")
        exposed = await self.exposed()
        if name not in exposed:
            return _error(f"tool não disponível via MCP: {name} (só leitura liberada no agent_registry)")
        spec, doc = exposed[name]
        ctx = ToolContext(store=self.store, customer=self.customer, message="",
                          conversation_id=f"mcp-{uuid.uuid4().hex[:12]}", agent_key=spec.agent,
                          agent_doc=doc, turn_context={})
        result = await execute(ctx, name, dict(arguments or {}))
        content = _json(result.content)
        structured = content if isinstance(content, dict) else {"result": content}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(content, ensure_ascii=False))],
                              structured_content=structured, is_error=result.is_error)

    # ---------------------------------------------------------------------------------- resources

    async def _handoffs(self) -> str:
        if not self._identity_ok():
            raise PermissionError("token expired or invalid")
        items = await self.store.find_many("agent_handoffs", {"customer_key": self.customer["customer_key"]},
                                           limit=HANDOFF_LIMIT, sort=[("at", -1)])
        return json.dumps([public_document(item) for item in items], default=str, ensure_ascii=False)

    async def _blackboard(self) -> str:
        if not self._identity_ok():
            raise PermissionError("token expired or invalid")
        items = await self.store.find_many("turn_blackboard", {"customer_key": self.customer["customer_key"]},
                                           limit=1, sort=[("created_at", -1)])
        return json.dumps(public_document(items[0]) if items else None, default=str, ensure_ascii=False)


async def create_server(store: DataStore, token: str | None, settings: Settings | None = None) -> CoordinationMCPServer:
    settings = settings or get_settings()
    customer = await resolve_identity(store, token, settings)
    return CoordinationMCPServer(store, customer, token or "", settings)


async def _main() -> None:
    settings = get_settings()
    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        raise MCPIdentityError(f"{TOKEN_ENV} is required")
    store = DataStore(settings)
    await store.connect()
    try:
        if store.memory:
            backend_dir = Path(__file__).resolve().parent
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))
            from seed import seed

            await seed(store, create_indexes=False)
        server = await create_server(store, token, settings)
        # stdout is the MCP channel: anything else goes to stderr.
        print(f"MCP server ready for customer {server.customer['customer_key']}", file=sys.stderr)
        await server.run_stdio_async()
    finally:
        await store.close()


def main() -> None:
    import anyio

    try:
        anyio.run(_main)
    except MCPIdentityError as exc:
        print(f"mcp_server: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
