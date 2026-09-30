"""Warmup automático do cache semântico: a demo abre e o cache já está quente.

Antes era um script manual que o apresentador precisava lembrar de rodar — e que aquecia cenários que
leem dados DO PRÓPRIO cliente (pedido, fatura, pontos), que a política `stable_v1` proíbe cachear (corretamente):
nunca gerava um HIT. O cache só serve pergunta GENÉRICA (catálogo/base de conhecimento), então é essas que
aquecemos, uma vez por área (o escopo global é por área), com uma identidade neutra `warmup-<area>`: sem
orçamento nem histórico, para a resposta guardada não carregar nada de nenhum cliente real.

O servidor aquece sozinho — ao subir e quando o frontend abre — chamando o orquestrador em processo.
Custo limitado por construção: execução única por vez (single-flight) e no máximo uma a cada
`cooldown_minutes`, não importa quantas abas/cliques disparem. Falha de um turno não interrompe os demais
nem derruba nada; uma rodada que falhou pode ser repetida logo em seguida.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from .seed_data import MEMORY_DEMOS

logger = logging.getLogger(__name__)

# Perguntas genéricas que roteiam para agentes cacheáveis (produto/suporte) — medido em modo live: a repetição
# vira HIT. Política/garantia roteiam para agentes pessoais e ficam de fora de propósito.
GENERIC_WARMUP_PROMPTS = [
    "me recomenda um fone de ouvido",
    "quais teclados vocês têm?",
    "preciso de um monitor bom para trabalhar",
    "como parear o fone bluetooth?",
    "meu fone chegou com defeito, o que eu faço?",
    "meu mouse parou de funcionar, o que faço?",
    # as perguntas dos chips "⚡ Cache semântico" de cada usuário: o 1º clique da demo já é HIT (mesma fonte, sem divergir)
    *[demo["cache"] for demo in MEMORY_DEMOS.values()],
]


class WarmupService:
    def __init__(self, store, orchestrator, *, has_llm: bool, cooldown_minutes: int = 45,
                 clock: Callable[[], float] = time.monotonic):
        self.store, self.orchestrator = store, orchestrator
        self.has_llm, self.cooldown, self.clock = has_llm, cooldown_minutes * 60, clock
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state: dict = {"status": "idle" if has_llm else "disabled", "ok": 0, "failed": 0}
        self._finished_at: float | None = None

    def state(self) -> dict:
        return dict(self._state)

    def _is_warm(self) -> bool:
        return self._finished_at is not None and self._state["failed"] == 0 and self.clock() - self._finished_at < self.cooldown

    async def _turn(self, customer: dict, message: str) -> bool:
        try:
            await self.orchestrator.run_turn(message, customer, None)
            return True
        except Exception:  # noqa: BLE001 — warmup nunca derruba o servidor
            logger.warning("warmup: turno falhou (%s)", customer["customer_key"], exc_info=True)
            return False

    async def run(self) -> dict:
        if not self.has_llm:
            return self.state()
        async with self._lock:  # single-flight: quem chega durante a rodada espera e reaproveita o resultado
            if self._is_warm():
                return self.state()
            self._state = {"status": "running", "ok": 0, "failed": 0}
            areas = sorted({doc["area"] for doc in await self.store.find_many("customers", {}) if doc.get("area")})

            async def warm_area(area: str) -> list[bool]:
                customer = {"customer_key": f"warmup-{area}", "name": "Warmup", "area": area, "plan": "essencial"}
                return [await self._turn(customer, message) for message in GENERIC_WARMUP_PROMPTS]  # em série

            results = [ok for chunk in await asyncio.gather(*[warm_area(a) for a in areas]) for ok in chunk]
            self._finished_at = self.clock()
            failed = results.count(False)
            self._state = {"status": "warm" if not failed else "partial", "ok": results.count(True), "failed": failed}
            logger.info("warmup concluído: %s", self._state)
            return self.state()

    def trigger(self) -> dict:
        """Dispara em segundo plano e devolve o estado atual na hora (a UI não espera o aquecimento)."""
        if self.has_llm and not self._is_warm() and (self._task is None or self._task.done()):
            self._state = {**self._state, "status": "running"}
            self._task = asyncio.create_task(self.run())
        return self.state()

    async def wait(self) -> None:
        if self._task:
            await self._task
