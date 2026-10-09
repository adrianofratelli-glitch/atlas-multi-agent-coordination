# Multi-agent coordination on MongoDB Atlas

[![CI](https://github.com/adrianofratelli-glitch/atlas-multi-agent-coordination/actions/workflows/ci.yml/badge.svg)](https://github.com/adrianofratelli-glitch/atlas-multi-agent-coordination/actions/workflows/ci.yml)

Eight customer-service agents coordinated through MongoDB Atlas: no queue, no Redis, no separate vector database. Routing rules, agent configuration, memory, cache, handoffs, and guardrail decisions are all documents you can query while the conversation is happening.

The UI is in Brazilian Portuguese (used in customer sessions); documentation and code are in English unless noted.

## The demo in five steps

**1. The customer sends a single message.**

![Chat home with the demo prompts](docs/screenshots/01-chat-home.png)

**2. It crosses four agents in one turn.** Support diagnoses the defect → product recommends a cheaper item → orders processes the exchange (the only write) → billing explains the invoice impact. Each hop is a document in `agent_handoffs`, streamed live through Change Streams.

![Agent chain timeline showing four handoffs in a single turn](docs/screenshots/03-chain-timeline.png)

**3. Agents are data, not deploys.** Model, persona, tools, and token budget live in `agent_registry`. Switch an agent off, or change its model, mid-demo with no redeploy.

![Agent registry with model, scope, and a per-agent on/off switch](docs/screenshots/04-agents-registry.png)

**4. Try to break it, including with random questions.** A jailbreak or false-authority prompt hits the denylist first. Anything new goes to a cheap LLM classifier that writes back **only the malicious clause** (never the benign opening of a compound message, never a phrase contained in a known legitimate question), quarantined to the customer who sent it with a 7-day TTL; it turns global only after 3 distinct customers send it (or an admin approves it), so one hostile customer cannot get another customer's legitimate question blocked. The automatic-block cutoff is **measured**, not guessed: the vector does not separate fraud from a legitimate refund ("I never received my order, I want my money back" scores 0.8664 against a fraud phrase). Above the highest measured legitimate score it blocks on its own; in the ambiguous band the classifier decides, so a customer is never blocked by vector proximity alone. And "what's the weather today?" is not an attack: it gets polite guidance with **0 tokens**, no agent and no cache, tagged `🧭 Scope guardrail`. What belongs to the store is decided by a vector search (`scope_probes`) that understands English, slang, and typos, and calls the LLM only in the ambiguous band. Measured on **287 LLM-generated situations** (with a holdout): accuracy **87.3% → 98.6%** on the holdout, 0% of legitimate customers blocked. Hiding a forbidden request behind a long legitimate one does not work either: the input is scored as a whole **and per clause** (measured on the real index: 0.887 alone, 0.7574 diluted, 0.8867 per clause, same cut-offs), at ~+23 ms p50 for a long message because the clause searches run in parallel.

![Guardrails panel: blocks, self-feeding denylist, flagged ambiguous cases](docs/screenshots/06-guardrails.png)

**5. Prove it happened.** Metrics come from the same collections everything else writes to.

![Metrics: agent coverage, handoffs, writes, native searches](docs/screenshots/05-metrics.png)

## The agents

| Agent | Role | Writes? |
|---|---|---|
| `orchestrator` | Classifies intent and routes | No |
| `order_agent` | Order status, exchanges/refunds | Yes: status only, with approved values |
| `product_agent` | Catalog recommendations via `$vectorSearch` | No |
| `support_agent` | Diagnosis via hybrid RAG, opens tickets | Yes: support tickets |
| `billing_agent` | Invoice lookup | No |
| `warranty_agent` | Coverage by category + purchase date | No |
| `loyalty_agent` | Points, tiers, reward redemption | Yes: points deduction |
| `logistics_agent` | Carrier, tracking, delivery estimate | Yes: reschedule flag |

## Stack

Python 3.12 · FastAPI · React + Vite · Claude models through an AI gateway (model per agent in `agent_registry`) · MongoDB Atlas (Vector Search with `voyage-4` auto-embedding, hybrid RRF, Change Streams, TTL, schema validation).

## Run it

```bash
cp .env.example .env            # fill MONGODB_URI and the LLM gateway variables
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
(cd frontend && npm ci)
ALLOW_DEMO_DB_WRITE=1 python backend/scripts/reset_demo.py   # full reset: data, probes, Search/Vector indexes (waits for READY)
./start.sh                      # backend :8031 + frontend :5191
```

`backend/scripts/reset_demo.py` is the single, idempotent reset: it runs `seed.py`, the embedding-classifier probes, wipes what rehearsals wrote (support tickets, redemptions, conversations, learned denylist phrases, per-customer demo memory, orphan LangGraph checkpoints) and waits for the Search/Vector indexes. It keeps the measured configuration (thresholds, classifier configs) and observability (`agent_traces`, `agent_handoffs`). Both it and `seed.py` **refuse the demo database** unless `ALLOW_DEMO_DB_WRITE=1` is passed; pointing `MONGODB_DB`/`MONGODB_BRAIN_DB` at `*_test` databases needs no flag. On a brand-new cluster run `python backend/calibrate_thresholds.py --apply` once after the indexes are READY.

Optional helper package: inside the author's workspace the shared `pov-shared` package (`uv pip install -e ../_shared`) supplies the tracing/edge-guardrail helpers and the clause splitter used by the anti-dilution guardrail. It is not required: without it the guardrail uses an equivalent local splitter and the optional features are no-ops.

The launcher uses a no-reload backend and an optimized frontend build by default. For reload/HMR development run `POV_DEV=1 ./start.sh`; the build is only redone when sources, lockfile, or configuration change.

No Atlas cluster? `DEMO_MODE=1 AUTH_REQUIRED=1 python run.py` runs the same contracts in memory (no Vector Search / Change Streams). This is what CI uses.

**The cache warms itself.** The server triggers warmup at startup and the frontend triggers it again on load (`POST /api/warmup`, one run at a time, at most once every 45 minutes), so the first click on a generic question is already `cache_hit: true`. `python backend/warmup.py` only forces it and follows along.

The demo writes for real (facts, episodes, customer cache). The **"Reset demo memory"** button (`POST /api/demo/reset`, only the JWT's customer) undoes what it wrote and reactivates what it replaced, so the script can be repeated from scratch.

## Tests

```bash
cd backend
pytest -q                       # offline unit + adversarial tests (no network)
(cd ../frontend && npm test)    # frontend unit tests (node --test)
python tests/adversarial/hostile_http.py <url> [--live]    # hostile inputs against a running server
python tests/smoke.py <url>     # black-box
python eval.py <url>            # golden dataset, results in eval_runs

# LIVE mode: REAL Atlas and LLM (costs tokens; disposable identity, cleans up what it wrote)
LIVE=1 pytest tests/test_live.py -q            # ~2 min, essential contracts
LIVE=1 pytest tests/test_live_random.py -q     # ~4 min, random and out-of-scope questions
LIVE=1 pytest tests/test_live_scenarios.py -q  # ~10 min, full journey of the 4 customers
LIVE=1 pytest tests/adversarial -q             # adds real double-click races on the isolated *_test database
python eval_situations.py                      # ~10 min, 287 LLM-generated situations against the real agent (dev vs holdout); exits 1 if any case misses
```

`DEMO_MODE` hides bugs that only the real driver produces: timezone-naive datetimes, a raw `ObjectId` in the timeline, a legacy document missing a new field. All three were found exactly that way, in LIVE mode, after the offline suite was green. That is why `git push` runs `.githooks/pre-push` (ruff + offline tests + `test_live.py`); enable it in a fresh clone with `git config core.hooksPath .githooks`.

The smoke test repeats the personalized query inside the same `conversation_id` and requires a HIT in `short_term_memory`; it then opens a new conversation and requires a MISS. This validates the cache without turning operational-state replay into cross-session leakage.

Every pull request runs the backend test suite and Ruff checks, plus a clean production frontend build and a dependency audit. CI uses the deterministic in-memory data-store implementation and needs no Atlas or Anthropic credentials.

## Flow of a turn

```
customer ──> POST /api/chat (JWT)
             │
             ├─ input guardrail   (lexical denylist -> vector -> LLM classifier)
             ├─ embedding scope   (in / out / chat)  ── out/chat ──> guidance, 0 tokens
             ├─ routing           (deterministic rule -> LLM only when unsure)
             ├─ semantic cascade  (short-term -> global cache)  ── HIT ──> cached answer
             │
             ▼
        SUPERVISOR  (5-hop ceiling, per-agent timeout, loop detector)
             │
             ├── parallel fan-out ──> order_agent ║ billing_agent
             └── chain ──> support_agent ─handoff─> product_agent ─handoff─> order_agent ─> logistics/billing
                                 │                       │                       │
                                 └── tools (Atlas): find / vectorSearch / hybridSearch / write
             │
             ├─ output guardrail + memory (fact, episode, cache)
             └─ turn trace (Langfuse) + OpenTelemetry spans (agent, handoff, tool, LLM)
```

Every agent, handoff, tool, and LLM call becomes a span carrying `conversation_id`, agent, tokens, estimated cost, and latency. `backend/scripts/trace_query.py` uses them to answer **who stalled and where**:

```bash
cd backend && TRACE_SINK=atlas ../.venv/bin/python run.py      # spans become Atlas documents
cd backend && ../.venv/bin/python scripts/trace_query.py       # ranking per conversation
conversation           spans errors tokens cost_usd  slowest
conv-8b3ca34a297a         20     2       0  0.00000  agent [order_agent] 3ms  ERROR in tool.orders.find_many
```

## Flags (all opt-in; with none of them, behavior is unchanged)

> `TRACE_SINK` and `GUARDRAILS_EDGE` rely on optional helper packages that are not part of this repository. Without them both features fail open and are no-ops.

| Flag | Default | What it enables |
|---|---|---|
| `TRACE_SINK` | `off` | OpenTelemetry tracing: `console`, `phoenix`, or `atlas` (spans become documents) |
| `TRACE_MASK_PII` | forced to `1` | with any sink on, span content is masked; not optional |
| `SUPERVISOR_LEGACY_500` | `0` | **restores** the old behavior: an agent failure surfaces as a 500. Graceful degradation is the default |
| `AGENT_TIMEOUT_SECONDS` | `45` | per-agent-hop ceiling (always on) |
| `LOOP_GUARD_REPEATS` | `2` | repetitions of (agent, intent) before escalating to a human |
| `TOOL_TIMEOUT_SECONDS` | `0` (off) | per-tool-call ceiling, including outside the agent hop |
| `TOOL_BREAKER` | `1` (on) | per-tool circuit breaker: 4 **consecutive** failures open it for 30s; `0` disables |
| `MONGODB_TEST_DB` / `MONGODB_TEST_BRAIN_DB` | `<database>_test` | isolated databases used by scripts that write real data |
| `ALLOW_DEMO_DB_WRITE` | `0` | escape hatch: lets those scripts write to the demo database |
| `GUARDRAILS_EDGE` | `0` | `mask_pii` in logs and `validate_output` (`max_repairs=0`) on the response |
| `CHAOS` | `0` | enables the fault-injection points (`CHAOS_SCENARIO`, `CHAOS_TARGET`, `CHAOS_PHASE`, `CHAOS_STATUS`, `CHAOS_DELAY`, `CHAOS_COUNT`) |

The LLM gateway (`app/llm.py`) already had retry with backoff, model fallback, and a per-endpoint circuit breaker, and these remain on by default.

**The PoV is resilient by default:** graceful supervisor degradation, per-agent timeout, loop detector, and per-tool circuit breaker apply without enabling anything. The flags above exist to DISABLE (`SUPERVISOR_LEGACY_500`, `TOOL_BREAKER=0`) or to tune a limit, not to turn resilience on.

## Resilience (measured, not asserted)

`backend/scripts/chaos_suite.py` is the PoV trying to break itself: 10 injected-failure scenarios on the real path, each with an explicit assertion of what "resilient" means there. Last run: **11/11**. The same scenarios run as regression in `tests/test_chaos.py`.

```bash
cd backend && CHAOS=1 ../.venv/bin/python scripts/chaos_suite.py          # whole battery
cd backend && CHAOS=1 LIVE=1 ../.venv/bin/python scripts/chaos_suite.py   # includes the SIGKILL (real Atlas)
cd backend && CHAOS=1 ../.venv/bin/python -m pytest tests/test_chaos.py -q
```

**What was broken before and fixed because of the battery:**

* The supervisor timeout only covered what happens inside a hop. A query hanging in turn loading held everything for 20s with `AGENT_TIMEOUT_SECONDS=1`. There is now a per-tool ceiling (`TOOL_TIMEOUT_SECONDS`) at the single Atlas access boundary: 21.2s → 2.2s.
* A tool failure did not count toward the circuit breaker (the failure point was outside the protected block): 5 consecutive error turns left the counter at zero.
* The "stuck agent" scenario did not interrupt anything. Measuring it showed the failure point had to sit inside the timed coroutine.

**Database isolation:** scenarios that write real data (`crash_resume`) and `eval_routing.py --live` use isolated test databases on the same cluster (`multi_agent_poc_test` / `multiagent_brain_test`, `backend/scripts/isolation.py`) and **refuse to run** if the target is the demo database, unless `ALLOW_DEMO_DB_WRITE=1` is passed. First use provisions the test database (seed + real Search/Vector indexes, a few minutes): `cd backend && ../.venv/bin/python scripts/isolation.py`. It also copies from the demo brain, **read-only**, what only exists on the cluster: the measured configuration (`guardrail_policies`, `turn_classifier_config`, `scope_classifier_config`) and the embedding-classifier probes (`turn_probes` 44, `scope_probes` 214), creating `turn_probes_vs`/`scope_probes_vs` in the test database. The isolated eval therefore measures the SAME embedding path as the demo, not the keyword fallback. The eval report prints that verdict (`embedding_classifiers`), so a "100%" is never ambiguous about which path was exercised. `backend/eval.py` (golden dataset, black-box over HTTP) goes through the same guard.

**Known limitations:** there is no streaming (the "mid-stream" failure scenario is a drop right after the provider's response); concurrency was measured with 5 simultaneous requests in a single process and is not a load test. Details and numbers in [docs/chaos-report.md](docs/chaos-report.md).

## Eval comparable with the single-agent PoV

24 reference conversations with expected routing (`eval/routing_dataset.json`, `synthetic: true`) and the metrics both PoVs can measure: routing accuracy (**entry** agent), resolution rate, handoffs per turn, and tokens per turn. Format documented in [eval/FORMAT.md](eval/FORMAT.md).

```bash
cd backend && ../.venv/bin/python eval_routing.py           # offline: 100% routing, 100% resolution, 0.125 handoff/turn, 43.5 tokens
cd backend && ../.venv/bin/python eval_routing.py --live    # Atlas + LLM, ISOLATED database: 100% / 100% / 0.125 / 762.6 tokens
```

The dataset is synthetic and from the same model family as the agent: it serves as regression, not as an estimate of real traffic. Three cases only have a verdict with an LLM and are left out of the offline accuracy.

## Optional observability: Langfuse

With `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in `.env` (`backend/app/langfuse_client.py`), each turn becomes ONE trace covering the whole timeline: routing, the cache decision, and each agent hop with its handoff, not just an isolated cache hit-rate number. Each agent that answered becomes a generation; cache/handoff/memory/guardrail become spans. Fail-open: without the keys, or with Langfuse down, it is a no-op (`auth_check()` runs once per process, so an unavailable Langfuse never exposes a link that 404s mid-demo). A "View trace in Langfuse" badge and a "MongoDB savings" card (semantic cascade + prompt cache) appear in the turn header, in the UI itself.

## Production boundary

Set `ENVIRONMENT=production`, `AUTH_REQUIRED=1`, and `DEMO_TOKEN_ISSUANCE_ENABLED=0`. Startup then fails closed on a weak or default JWT/admin secret, wildcard CORS, disabled authentication, or demo-token issuance left on. `/metrics` is admin-only. The local launcher remains a PoV runtime: add TLS termination, a corporate IdP, and a managed process/container platform before any external exposure.

## Documentation

[Architecture](docs/architecture.md) · [ADR-001: document-oriented coordination](docs/adr/ADR-001-arquitetura-multi-agente.md) · [ADR-002: LLM memory and personal turns outside the cache](docs/adr/ADR-002-memoria-llm-e-turno-pessoal.md) · [ADR-003: two-band guardrail and out of scope](docs/adr/ADR-003-guardrail-em-duas-faixas.md) · [ADR-004: embedding scope and situation-based measurement](docs/adr/ADR-004-escopo-por-embedding-e-medicao-por-situacoes.md) · [Chaos report](docs/chaos-report.md) · [Eval format](eval/FORMAT.md)

The ADRs and briefing documents are written in Brazilian Portuguese.

## License

MIT, see [LICENSE](LICENSE).
