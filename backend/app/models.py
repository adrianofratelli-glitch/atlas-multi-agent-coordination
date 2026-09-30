from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator


class TokenRequest(BaseModel):
    customer_key: str = Field(min_length=3, max_length=64)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)


class AgentUpdate(BaseModel):
    active: bool | None = None
    model: str | None = Field(default=None, max_length=100)
    fallback_model: str | None = Field(default=None, max_length=100)
    persona: str | None = Field(default=None, max_length=4000)
    max_turn_tokens: int | None = Field(default=None, ge=128, le=8192)
    max_output_tokens: int | None = Field(default=None, ge=128, le=8192)


class OrderStatusUpdate(BaseModel):
    order_id: str = Field(min_length=5, max_length=64)
    status: Literal["processando", "enviado", "entregue", "troca_solicitada", "reembolsado"]

    @field_validator("order_id")
    @classmethod
    def validate_order_id(cls, value: str) -> str:
        value = value.upper().strip()
        if not value.startswith("PED-") or not value[4:].isdigit():
            raise ValueError("order_id deve seguir o formato PED-0000")
        return value


def _jsonable(value: Any) -> Any:
    """Tipos do driver (ObjectId, Decimal128, bytes) viram texto: documentos reais do Atlas chegam à timeline
    crus em alguns caminhos, e o DEMO_MODE nunca produz esses tipos."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if type(value).__module__.startswith("bson") or isinstance(value, bytes):
        return str(value)
    return value


class TimelineEvent(BaseModel):
    category: Literal["agent", "memory", "guardrail", "cache", "handoff", "fanout"]
    title: str
    agent: str | None = None
    collection: str | None = None
    op: Literal["read", "write", "vectorSearch", "hybridSearch", "changeStream", "graphLookup"] | None = None
    filter: dict[str, Any] | None = None
    result: Any = None
    reason: str | None = None
    duration_ms: float = 0
    replayed: bool = False

    @field_serializer("filter", "result")
    def _serialize_driver_types(self, value: Any) -> Any:
        return _jsonable(value)


class Suggestion(BaseModel):
    """Próximo passo clicável, sempre derivado de um documento que existe."""
    topic: str
    label: str
    message: str


class ChatResponse(BaseModel):
    conversation_id: str
    response: str
    active_agent: str
    route_source: Literal["rules", "orchestrator", "fallback", "fanout"]
    cache_hit: bool
    cache_source: Literal["curto_prazo", "cache"] | None = None
    tokens_economizados: int = 0
    timeline: list[TimelineEvent]
    usage: dict[str, int]
    suggestions: list[Suggestion] = []
    langfuse_trace_url: str | None = None
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    economics: dict[str, Any] = Field(default_factory=dict)
    # Turno que terminou em degradação graciosa (padrão do supervisor). Default False: o campo
    # só aparece preenchido quando algo realmente falhou, então nenhuma resposta normal muda.
    degraded: bool = False
    degraded_reason: str | None = None


class ReviewResolution(BaseModel):
    """Resolução de um caso escalado. `decision` é fechada: o analista escolhe entre encaminhamentos
    conhecidos, não digita uma ação livre que ninguém depois consegue agregar."""
    decision: Literal["quality_analysis", "approve_replacement", "refund", "reject"]
    resolved_by: str = Field(min_length=2, max_length=80)
    note: str = Field(default="", max_length=2000)
