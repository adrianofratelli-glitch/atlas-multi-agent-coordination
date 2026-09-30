import asyncio
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from time import perf_counter


class Metrics:
    def __init__(self):
        self.counters: Counter[str] = Counter()
        self.route_latency_ms: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def increment(self, name: str, value: int = 1) -> None:
        async with self._lock:
            self.counters[name] += value

    @asynccontextmanager
    async def track_route(self, route: str):
        started = perf_counter()
        try:
            yield
            await self.increment(f"route.{route}.ok")
        except Exception:
            await self.increment(f"route.{route}.error")
            raise
        finally:
            elapsed = (perf_counter() - started) * 1000
            async with self._lock:
                values = self.route_latency_ms[route]
                values.append(elapsed)
                if len(values) > 1000:
                    del values[:-1000]

    def snapshot(self) -> dict:
        latencies = {}
        for route, values in self.route_latency_ms.items():
            ordered = sorted(values)
            p95_index = max(0, int(len(ordered) * 0.95) - 1)
            latencies[route] = {
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 2) if values else 0,
                "p95_ms": round(ordered[p95_index], 2) if ordered else 0,
            }
        return {"counters": dict(self.counters), "routes": latencies}

    def prometheus(self) -> str:
        lines: list[str] = []
        for name, value in sorted(self.counters.items()):
            label = name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'pov_business_counter{{name="{label}"}} {value}')
        for route, values in sorted(self.route_latency_ms.items()):
            label = route.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'pov_http_requests_total{{route="{label}"}} {len(values)}')
            lines.append(f'pov_http_latency_ms_sum{{route="{label}"}} {sum(values):.3f}')
            lines.append(f'pov_http_latency_ms_max{{route="{label}"}} {max(values, default=0):.3f}')
        return "\n".join(lines) + "\n"


metrics = Metrics()
