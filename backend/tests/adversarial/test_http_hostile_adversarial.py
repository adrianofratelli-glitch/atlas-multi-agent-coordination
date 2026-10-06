"""Entradas hostis pela API real (uvicorn em DEMO_MODE, processo separado em 127.0.0.1:8031)."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[2]
PORT = 8031


def _port_busy() -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


@pytest.fixture(scope="module")
def server():
    if _port_busy():
        pytest.skip("porta 8031 ocupada (backend da demo no ar); rode tests/adversarial/hostile_http.py contra ele")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "GROVE_", "LANGFUSE_"))}
    env.update(DEMO_MODE="1", MONGODB_URI="", AUTH_REQUIRED="1", WARMUP_ON_START="0", RATE_LIMIT_REQUESTS="1000",
               ANTHROPIC_API_KEY="", GROVE_API_KEY="", LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            pytest.fail("servidor DEMO_MODE não subiu")
        yield f"http://127.0.0.1:{PORT}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_hostile_inputs_are_rejected_or_contained(server):
    sys.path.insert(0, str(Path(__file__).parent))
    from hostile_http import run
    failures = [(name, detail) for name, ok, detail in run(server) if not ok]
    assert not failures, failures
