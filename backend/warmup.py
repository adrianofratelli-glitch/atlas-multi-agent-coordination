"""Aquecimento MANUAL (opcional): o servidor já aquece sozinho ao subir e quando a UI abre.

Serve só para forçar/observar: dispara POST /api/warmup e acompanha até terminar. Respeita o cooldown do
servidor, então rodar de novo logo em seguida não gasta token.

Uso: python warmup.py [URL]
"""

import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8031"


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=30) as client:
        for _ in range(120):
            state = client.post("/api/warmup").json()
            print(state)
            if state["status"] in ("warm", "partial", "disabled"):
                break
            time.sleep(3)
    sys.exit(0 if state["status"] == "warm" else 1)


if __name__ == "__main__":
    main()
