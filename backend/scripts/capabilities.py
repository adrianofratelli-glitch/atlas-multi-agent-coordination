"""Sonda e checagem das capabilities do Atlas usadas pelo multiagente (SÓ LEITURA).

    cd backend && ../.venv/bin/python scripts/capabilities.py            # banco de teste
    cd backend && ../.venv/bin/python scripts/capabilities.py --demo     # leitura no banco da demo
    cd backend && ../.venv/bin/python scripts/capabilities.py --json out.json

Confere: versão do servidor, `$rankFusion` + `scoreDetails` no kb_articles, `$rerank` depois da
fusão (modelo configurado no agent_registry), índice `kb_lexical_v1` READY com o mapeamento de
sinônimos `kb_sinonimos` READY, bloco `retrieval` do support_agent válido, time series da
economia e o worker de change stream (heartbeat). Nada é escrito. Sai 1 se algo bloqueante
falhar (índice/sinônimos ausentes); o resto é aviso, porque o app degrada sozinho.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import atlas_compat as ac  # noqa: E402
from app import retrieval  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import DataStore  # noqa: E402


async def _try(store, name, pipeline, report):
    t0 = time.perf_counter()
    try:
        rows = await store.aggregate("kb_articles", pipeline)
        report[name] = {"ok": True, "rows": len(rows), "ms": round((time.perf_counter() - t0) * 1000, 1),
                        "sample": json.loads(json.dumps(rows[:1], default=str))}
    except Exception as exc:  # noqa: BLE001 — a sonda registra a resposta do servidor
        report[name] = {"ok": False, "code": getattr(exc, "code", None),
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="lê o banco da demo (nada é escrito)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if args.demo:
        settings = get_settings()
    else:
        from scripts.isolation import test_settings
        settings = test_settings()
    store = DataStore(settings)
    await store.connect()
    report: dict = {"database": settings.mongodb_db, "pov_shared": ac.SHARED}
    try:
        info = await store.client.admin.command("buildInfo")
        report["buildInfo"] = {"version": info.get("version"), "modules": info.get("modules")}
        registry = await store.find_one("agent_registry", {"agent_key": "support_agent"}, brain=True) or {}
        fusion, warns = retrieval.kb_config((registry.get("retrieval") or {}).get("fusion"))
        rerank, rwarns = retrieval.rerank_config(registry)
        report["registry_retrieval"] = {"present": "retrieval" in registry, "warnings": warns + rwarns,
                                        "fusion": fusion, "rerank": rerank}
        q = "o carregador demora muito"
        await _try(store, "rankFusion_scoreDetails", retrieval.build_kb_rank_fusion_pipeline(q, config=fusion), report)
        await _try(store, "rankFusion_basico", retrieval.build_kb_rank_fusion_pipeline(q, config=fusion, basic=True), report)
        rr = dict(rerank, enabled=True)
        await _try(store, f"rerank_{rr['model']}", retrieval.build_kb_rank_fusion_pipeline(q, config=fusion, rerank=rr), report)
        cursor = await store._collection("kb_articles").list_search_indexes()
        idx = {i["name"]: i for i in await cursor.to_list(None)}
        lex = idx.get(retrieval.KB_LEXICAL_INDEX, {})
        report["kb_lexical_v1"] = {"status": lex.get("status"), "synonymMappingStatus": lex.get("synonymMappingStatus"),
                                   "synonyms": (lex.get("latestDefinition") or {}).get("synonyms")}
        report["kb_synonyms_docs"] = await store.count("kb_synonyms")
        db = store.client[settings.mongodb_db]
        info = await db.list_collections(filter={"name": "llm_usage_ts"})
        ts = [c async for c in info]
        report["llm_usage_ts"] = (ts[0].get("options") or {}).get("timeseries") if ts else None
        hb = await store.find_one("change_stream_tokens", {"_id": "handoff_stream_probe"}, brain=True)
        report["change_stream_heartbeat"] = bool(hb)
    finally:
        await store.close()
    blocking = []
    if report["kb_lexical_v1"]["status"] != "READY":
        blocking.append("kb_lexical_v1 não está READY")
    if report["kb_lexical_v1"]["synonymMappingStatus"] != "READY":
        blocking.append("mapeamento de sinônimos não está READY (o retrieval cai para rankFusion_basico)")
    if not report["rankFusion_scoreDetails"]["ok"]:
        blocking.append("$rankFusion recusado (o retrieval cai para RRF na aplicação)")
    report["blocking"] = blocking
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
