import asyncio
from time import monotonic

import anthropic

from . import chaos, observability
from .budget import TurnBudget, estimate_tokens
from .config import Settings


# Circuit breaker leve, por processo, compartilhado entre TODAS as chamadas (mesmo turno e
# entre turnos) — não é estado por instância de LLMGateway porque o objetivo é justamente
# não pagar o retry completo (até 2 modelos x 3 tentativas) em cada uma das até 5 chamadas
# de um turno quando o provedor já está fora do ar. Abre depois de `FAILURE_THRESHOLD` falhas
# consecutivas e fica aberto por `OPEN_SECONDS`; depois disso, uma nova tentativa "meia-aberta"
# decide se fecha (sucesso) ou reabre a janela (falha).
FAILURE_THRESHOLD = 4
OPEN_SECONDS = 30.0


class _CircuitBreaker:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    async def allow(self) -> bool:
        """False quando o circuito está aberto e ainda dentro da janela de curto-circuito."""
        async with self._lock:
            if self._opened_at is None:
                return True
            if monotonic() - self._opened_at >= OPEN_SECONDS:
                # Janela expirou: deixa a próxima chamada testar o provedor (meio-aberto),
                # sem resetar o contador ainda — só um sucesso real fecha o circuito.
                self._opened_at = None
                return True
            return False

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAILURE_THRESHOLD:
                self._opened_at = monotonic()


# Um breaker por endpoint, compartilhado entre turnos no processo.
# Falha na rota Anthropic não abre o circuito da rota Chat Completions.
_circuits: dict[str, _CircuitBreaker] = {}


def _validate_grove_url(url: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "grove-gateway-prod.azure-api.net"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in url)):
        raise ValueError("Grove requer HTTPS no host aprovado, sem credenciais ou query na URL")
    return url.rstrip("/")


class LLMGateway:
    """Explicit per-model protocol routing through Grove, with a per-endpoint breaker."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.timeout = min(float(settings.turn_deadline_seconds), 60.0)
        base = settings.grove_anthropic_base_url or settings.anthropic_base_url
        self.grove = bool(settings.grove_anthropic_base_url or (
            base and "grove-gateway-prod.azure-api.net" in base))
        if self.grove:
            _validate_grove_url(base)
        elif base and base.rstrip("/") not in ("https://api.anthropic.com", "https://api.anthropic.com/v1"):
            raise ValueError("Endpoint Anthropic direto deve usar api.anthropic.com")
        if settings.grove_chat_completions_url:
            _validate_grove_url(settings.grove_chat_completions_url)
        if settings.grove_openai_models and not settings.grove_chat_completions_url:
            raise ValueError("Configure GROVE_CHAT_COMPLETIONS_URL para os modelos OpenAI-compatible")
        key = (settings.grove_api_key or settings.anthropic_api_key) if self.grove else settings.anthropic_api_key
        self.anthropic_client = anthropic.AsyncAnthropic(
            api_key="unused-grove" if self.grove else key,
            base_url=base or None,
            default_headers={"api-key": key} if self.grove else {},
            http_client=anthropic.DefaultAsyncHttpxClient(follow_redirects=False),
            timeout=self.timeout, max_retries=0,
        ) if key and not settings.demo_mode else None
        # Existing callers use .client only as an availability check.
        self.client = self.anthropic_client or (
            True if settings.grove_api_key and settings.grove_chat_completions_url
            and not settings.demo_mode else None)

    async def _request(self, model, system_static, dynamic_context, message, max_tokens, temperature=None):
        # Antes do primeiro token: é aqui que 429/5xx e timeout do provedor batem de verdade.
        await chaos.hook("llm", name=model, phase="before_first_token")
        if model in self.settings.grove_openai_models:
            import httpx
            if not self.settings.grove_api_key:
                raise RuntimeError("GROVE_API_KEY ausente")
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.post(
                    self.settings.grove_chat_completions_url,
                    headers={"api-key": self.settings.grove_api_key},
                    json={"model": model, "max_completion_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system_static + "\n\n" + dynamic_context},
                                       {"role": "user", "content": message}]},
                )
                response.raise_for_status()
                body = response.json()
            usage = body.get("usage") or {}
            cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            known = "prompt_tokens" in usage and "completion_tokens" in usage
            counts = {"input_tokens": max(0, int(usage.get("prompt_tokens", 0)) - cached),
                      "output_tokens": int(usage.get("completion_tokens", 0)),
                      "cache_read_tokens": cached, "cache_write_tokens": 0}
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop" or not choice["message"].get("content"):
                return None, counts, known
            return choice["message"]["content"], counts, known
        if not self.anthropic_client:
            raise RuntimeError("Anthropic route is not configured")
        params = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
                    *([{"type": "text", "text": dynamic_context}] if dynamic_context.strip() else [])],
            messages=[{"role": "user", "content": message}],
        )
        if temperature is not None:
            # classificação (segurança, roteamento) precisa ser determinística: temperature 0
            params["temperature"] = temperature
        try:
            response = await self.anthropic_client.messages.create(**params)
        except anthropic.BadRequestError as exc:
            if temperature is None or "temperature" not in str(exc).lower():
                raise
            params.pop("temperature")  # nem todo modelo aceita o parâmetro: repete sem ele em vez de falhar o turno
            response = await self.anthropic_client.messages.create(**params)
        # Equivalente ao "meio do stream": resposta já saiu do provedor, conexão cai antes de
        # o turno usar o texto. (Este PoV não usa streaming; o ponto de falha é este.)
        await chaos.hook("llm", name=model, phase="mid_stream")
        usage = response.usage
        counts = {"input_tokens": int(usage.input_tokens), "output_tokens": int(usage.output_tokens),
                  "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                  "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0)}
        text = "".join(block.text for block in response.content if block.type == "text")
        return text if response.stop_reason == "end_turn" and text else None, counts, True

    async def complete(self, *, agent: dict, user_message: str, dynamic_context: str,
                       budget: TurnBudget, static_context: str = "") -> tuple[str | None, dict[str, int]]:
        import httpx
        from .economics import call_cost
        if not self.client:
            return None, {"input_tokens": 0, "output_tokens": 0}
        system_static = agent["persona"] + (f"\n\n{static_context}" if static_context else "")
        estimated = estimate_tokens(system_static + user_message + dynamic_context)
        models = list(dict.fromkeys([agent["model"], agent.get("fallback_model") or agent["model"]]))
        for model in models:
            protocol = "openai" if model in self.settings.grove_openai_models else "anthropic"
            endpoint = self.settings.grove_chat_completions_url if protocol == "openai" else (
                self.settings.grove_anthropic_base_url or self.settings.anthropic_base_url or "https://api.anthropic.com")
            breaker = _circuits.setdefault(endpoint, _CircuitBreaker())
            if not await breaker.allow():
                budget.llm_calls.append({"agent": agent["agent_key"], "model": model,
                                         "protocol": protocol, "status": "circuit_open",
                                         "estimated_cost_usd": 0.0, "usage_known": True})
                continue
            for attempt in range(3):
                remaining = min(budget.global_limit - budget.total_used,
                                budget.per_agent_limits.get(agent["agent_key"], budget.global_limit)
                                - budget.used_by_agent.get(agent["agent_key"], 0))
                if remaining <= estimated:
                    from .budget import BudgetExceeded
                    raise BudgetExceeded("budget insuficiente para entrada e saída do modelo")
                budget.reserve(agent["agent_key"], estimated)
                started = monotonic()
                from datetime import datetime, timezone
                started_at = datetime.now(timezone.utc).isoformat()
                record = {"agent": agent["agent_key"], "model": model, "protocol": protocol,
                          "provider": "grove" if protocol == "openai" or self.grove else "anthropic",
                          "attempt": attempt + 1, "fallback": model != models[0], "started_at": started_at,
                          "usage_known": False, "status": "error", "estimated_cost_usd": None}
                record["prices_usd_per_million"] = self.settings.llm_prices.get(model, {})
                blended = model not in self.settings.llm_prices and model in self.settings.llm_blended_prices
                record["cost_basis"] = "historical_blended_estimate" if blended else "configured_tariffs"
                if blended:
                    record["blended_usd_per_million"] = self.settings.llm_blended_prices[model]
                retry = False
                span_attrs = {"llm.model": model, "llm.protocol": protocol,
                              "agent": agent["agent_key"], "llm.attempt": attempt + 1}
                with observability.span("llm.call", **span_attrs) as current_span:
                    try:
                        text, counts, known = await self._request(
                            model, system_static, dynamic_context, user_message,
                            min(agent.get("max_output_tokens") or agent["max_turn_tokens"], remaining - estimated),
                            agent.get("temperature"))
                        record.update(counts, usage_known=known, status="ok" if text else "incomplete")
                        # Incomplete responses still carry billable usage.
                        record["estimated_cost_usd"] = call_cost({**record, "status": "ok"}, self.settings.llm_prices, self.settings.llm_blended_prices)
                        actual = sum(counts.values())
                        budget.reconcile(agent["agent_key"], estimated, actual if known else estimated)
                        budget.cache_read_tokens += counts["cache_read_tokens"]
                        budget.cache_write_tokens += counts["cache_write_tokens"]
                        observability.record_llm_usage(current_span, record)
                        if text:
                            await breaker.record_success()
                            return text, counts
                    except (anthropic.APIError, httpx.HTTPError, chaos.ChaosProviderError,
                            RuntimeError, ValueError, KeyError, IndexError) as exc:
                        # No error messages/bodies: providers can echo sensitive payloads.
                        record["error_type"] = type(exc).__name__
                        status = getattr(exc, "status_code", None)
                        if isinstance(exc, httpx.HTTPStatusError):
                            status = exc.response.status_code
                        retry = status == 429 or (status is not None and status >= 500) or isinstance(
                            exc, (anthropic.APIConnectionError, httpx.TransportError))
                        await breaker.record_failure()
                        # Unknown usage remains reserved conservatively for this failed attempt.
                    finally:
                        record["latency_ms"] = round((monotonic() - started) * 1000, 2)
                        budget.llm_calls.append(record)
                if not retry or attempt == 2:
                    break
                await asyncio.sleep(.25 * (2 ** attempt))
        return None, {"input_tokens": 0, "output_tokens": 0}
