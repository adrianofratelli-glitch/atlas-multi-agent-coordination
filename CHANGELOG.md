# Changelog

## 1.1.0 (2026-10-06)

Adversarial hardening.

- Guardrail: input scored per clause against dilution (a forbidden phrase appended to a long legitimate question dropped from 0.887 to 0.7574 on the real vector denylist and slipped through); lexical denylist folds zero-width/bidi characters and punctuation.
- Concurrency: loyalty redemption and human escalation are idempotent under real double clicks (measured on Atlas: 500 on the redemption and three tickets before).
- Isolation: LangGraph checkpoint threads are namespaced by customer and expire with the conversation (1-day TTL).
- PII: formatted CNPJ and zero-width-obfuscated documents are masked; card numbers are no longer split into phone labels.
- LLM gateway: `x-api-key` on the Chat Completions route; configuration errors (404/400) no longer open the shared circuit breaker.
- Ops: `backend/scripts/reset_demo.py` (single idempotent reset); `seed.py` and the reset refuse the demo database without `ALLOW_DEMO_DB_WRITE=1`; seed keeps the measured `vector_block_threshold`; `*_test` databases are never mistaken for the demo.
- Tests: `backend/tests/adversarial/` (Grove 429/5xx/timeout, dilution, inter-agent and KB injection, hostile HTTP inputs, checkpoint isolation, PII, reset guard, LIVE double-click races).
- Seed: `order_agent` (the only agent that writes order status) now uses `claude-sonnet-5-5` (model and fallback).
- Deps: `source-map-js` 1.2.2 (GHSA-68fv-2mgg-jv7q); `pymongo>=4.18.2` (CVE-2026-88029, CVE-2026-96747/96748/96749). Removed the unused `AiBrainPanel` component.
- UI: layout MongoDB 2026 "Dark Stage v4" (tokens mais escuros, Special Gothic / Source Code Pro locais, motivos de escada e grade, movimento escalonado).

## 1.0.0 (2026-09-30)

First public release.

- Repository rebuilt with a clean, single-commit history.
- English README and repository description, with screenshots captured against a real Atlas cluster.
- MIT license.
- Internal notes, presentation decks, test-output snapshots, and tooling configuration removed from the repository.
