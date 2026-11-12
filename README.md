# Multi-agent coordination on MongoDB Atlas

Mapa do código: [docs/MAPA_DO_CODIGO.md](docs/MAPA_DO_CODIGO.md)

[![CI](https://github.com/adrianofratelli-glitch/atlas-multi-agent-coordination/actions/workflows/ci.yml/badge.svg)](https://github.com/adrianofratelli-glitch/atlas-multi-agent-coordination/actions/workflows/ci.yml)

Eight customer-service agents coordinated through MongoDB Atlas: no queue, no Redis, no separate vector database. Routing rules, agent configuration, tool permissions, approval policies, memory, cache, handoffs, the per-turn blackboard and guardrail decisions are all documents you can query while the conversation is happening.

The specialists are real tool-calling agents: each one picks typed tools that the `agent_registry` allows for it, the orchestrator plans compound requests, every hop writes to a shared blackboard in Atlas, and the orchestrator consolidates one answer from it. The LLM proposes; the server disposes (ownership filters, write gates, human approval) ([ADR-011](docs/adr/ADR-011-tool-calling-plano-quadro-e-consolidacao.md)).

The UI is in Brazilian Portuguese (used in customer sessions); documentation and code are in English unless noted.

## The demo in five steps

**1. The customer sends a single message and watches the team work.** The chat view shows the conversation, a live agent graph (orchestrator in the center, specialists from `GET /api/graph/topology` around it, edges drawn in the order the handoffs happen) and an "under the hood" feed with every MongoDB operation of the turn and its real (masked) filter.

![Chat home with the live agent graph](docs/screenshots/01-chat-home.png)

**2. Plan → specialists with tool calling → blackboard → one answer.** For a compound request ("my headset is defective, recommend something cheaper, exchange it, then check delivery") the orchestrator decomposes it into tasks (temperature 0, JSON validated against the registry); the first agent is still the deterministic route. Each specialist calls only its own tools and hands off with `transfer_to_agent` (destination validated by the server: active in the registry, no cycle, 5-hop ceiling). Every hop is one LangGraph super-step with a checkpoint in Atlas and one entry in `turn_blackboard` (TTL 24 h). The next agent sees only server-generated facts from the blackboard (tools called, writes that actually happened), never another model's free text, so one agent cannot prompt-inject the next. The orchestrator then writes a single answer from the blackboard: in the customer's order, only facts on the board, and a completed write wins over any sentence that contradicts it.

![Turn chain: plan, hops, blackboard and consolidation](docs/screenshots/03-chain-timeline.png)

**3. Sensitive writes wait for a human, and a crashed turn resumes where it stopped.** `multiagent_brain.hitl_policies` says which tool calls need an analyst: a refund, and rewards of 500+ points (that second policy ships switched off, so it can be turned on live from the Agents tab). When one matches, the tool does not write: the graph calls LangGraph `interrupt()` and the turn stays paused in the Atlas checkpoint, with no open connection, surviving a restart. Each turn has its own checkpoint thread, recorded on the review. The analyst's decision (`POST /api/admin/reviews/{id}/resolve`) resumes exactly that paused turn (the pending interrupt must belong to that review); the approved write runs once, in its own graph node, through the same tool executor and gates, and is marked `applied_at` on the review so a retry (`POST /api/admin/reviews/{id}/continue`) never writes twice ([ADR-012](docs/adr/ADR-012-aprovacao-humana-com-interrupt.md)). "Simulate crash and resume from checkpoint" kills a turn before its second hop and resumes it: the completed hop and its write do not run again.

![Paused write waiting for an analyst](docs/screenshots/08-hitl-approval.png)

**4. Try to break it, including with random questions.** A jailbreak or false-authority prompt hits the denylist first. Anything new goes to a cheap LLM classifier that writes back **only the malicious clause** (never the benign opening of a compound message, never a phrase contained in a known legitimate question), quarantined to the customer who sent it with a 7-day TTL; it turns global only after 3 distinct customers send it (or an admin approves it), so one hostile customer cannot get another customer's legitimate question blocked. The automatic-block cutoff is **measured**, not guessed: the vector does not separate fraud from a legitimate refund ("I never received my order, I want my money back" scores 0.8664 against a fraud phrase). Above the highest measured legitimate score it blocks on its own; in the ambiguous band the classifier decides, so a customer is never blocked by vector proximity alone. And "what's the weather today?" is not an attack: it gets polite guidance with **0 tokens**, no agent and no cache, tagged `🧭 Scope guardrail`. What belongs to the store is decided by a vector search (`scope_probes`) that understands English, slang, and typos, and calls the LLM only in the ambiguous band. Measured on **287 LLM-generated situations** (with a holdout): accuracy **87.3% → 98.6%** on the holdout, 0% of legitimate customers blocked. Hiding a forbidden request behind a long legitimate one does not work either: the input is scored as a whole **and per clause** (measured on the real index: 0.887 alone, 0.7574 diluted, 0.8867 per clause, same cut-offs), at ~+23 ms p50 for a long message because the clause searches run in parallel. A write the customer did not explicitly ask for is refused by the server even if a model tries it.

![Guardrails panel: blocks, self-feeding denylist, flagged ambiguous cases](docs/screenshots/06-guardrails.png)

**5. Measure it, including against a single agent.** The A/B tab sends the same question to one generalist agent with every tool and to the team (`POST /api/compare`): same tools, same write gates, same model, writes simulated and no web-search quota spent in both arms, the same output guardrail, and an LLM judge scoring completeness, coherence and grounding. The honest numbers on the flagship scenario: the single agent took ~9 s and ~US$ 0.06; the team took ~40 s and ~US$ 0.27. What the team buys is per-agent write scope (`allowed_tools`), per-agent budgets, a per-hop audit trail on the blackboard, pause/resume by checkpoint and an auditable plan; parallelizing independent plan steps is the next latency step. Metrics come from the same collections everything else writes to.

![A/B: single agent vs. the team on the same question](docs/screenshots/07-ab-compare.png)

![Metrics: agent coverage, handoffs, writes, native searches, cost per agent](docs/screenshots/05-metrics.png)

## The agents

| Agent | Role | Tools (`allowed_tools` in `agent_registry`) | Writes? |
|---|---|---|---|
| `orchestrator` | Routes when the deterministic rule is unsure, plans compound requests, consolidates the answer | none (plan and consolidation are server steps) | No |
| `order_agent` | Order status, exchanges/refunds | `get_order`, `request_order_status_change` | Yes: status only, closed list; a refund pauses for approval |
| `product_agent` | Catalog recommendations | `search_catalog` (`$vectorSearch` + `$rerank`) | No |
| `support_agent` | Diagnosis via hybrid RAG, opens tickets | `search_knowledge_base` (`$rankFusion`), `open_support_ticket` | Yes: support tickets, only on explicit request |
| `billing_agent` | Invoice lookup | `get_invoice` | No |
| `warranty_agent` | Coverage by category + purchase date | `check_warranty` (date math in Python, `$graphLookup` chain) | No |
| `loyalty_agent` | Points, tiers, reward redemption | `get_loyalty_account`, `redeem_reward` | Yes: points deduction; 500+ points can require approval (policy off in the seed) |
| `logistics_agent` | Carrier, tracking, delivery estimate; carrier delay news when the delivery is overdue | `get_shipment`, `request_delivery_reschedule`, `search_carrier_news` | Yes: reschedule flag |

**Agents and tools are data, not deploys.** Model, persona, allowed tools, and token budget live in `agent_registry`; the global per-turn budget lives in `multiagent_brain.model_config.global_turn_tokens`. Switch an agent or a single tool off, or change its model, mid-demo with no redeploy (`PATCH /api/admin/agents/{agent_key}`); toggle an approval policy with `PATCH /api/admin/hitl-policies/{policy_id}`. The model never builds a query: tool arguments are validated ids and closed enums, and every executor rebuilds the owner filter from the JWT. Every write tool passes three server gates in order: web-search taint in the turn, an explicit request in the customer's own message (a question such as "how do I exchange it?" is not a request, and a status change only applies to the order the customer cited), and the approval policy. Tool arguments outside the tool's JSON Schema are dropped before the executor runs. Without a model (`DEMO_MODE`, CI, open breaker, or `AGENT_TOOL_CALLING=0`) each agent falls back to its deterministic runner, with the blackboard in the prompt, so the offline suite exercises the same graph.

![Agents tab: registry with model, tools, budget and approval policies](docs/screenshots/04-agents-registry.png)

The agents whose documents carry dates (orders, billing, warranty, logistics) get today's date in the prompt; date math (warranty days left, days past the delivery estimate) is done in Python, never by the model. Web search is a tool the model can ask for (`search_carrier_news`), but the server decides whether it runs (overdue delivery + the tool enabled for the customer's area in `multiagent_brain.tool_registry`) and builds the query from the shipment document, so customer text never leaves the process; after a web result no write runs in the same turn. Atlas governs the tool: live on/off per area with one `update_one`, an atomic monthly quota, a 6-hour result cache with TTL and a 30-day audit trail ([ADR-005](docs/adr/ADR-005-busca-web-deterministica-governada-pelo-atlas.md)). Out-of-scope questions are still refused with 0 tokens.

## Stack

Python 3.12 · FastAPI · LangGraph `StateGraph` with the MongoDB checkpointer · React + Vite · Claude models through an AI gateway (model per agent in `agent_registry`, tool calling) · MCP Python SDK · MongoDB Atlas (Vector Search with `voyage-4` auto-embedding, hybrid `$rankFusion`, `$rerank`, Change Streams, TTL, time series, schema validation).

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

## Docker

One container: nginx serves the frontend build and proxies `/api` (path unchanged, SSE unbuffered) to a single uvicorn worker.

```bash
docker build -t multi-agent .                                    # without pov-shared (local fallbacks)
docker build --build-context shared=../_shared -t multi-agent .  # with pov-shared, as in the author's workspace
docker run --env-file .env -p 127.0.0.1:18031:8080 multi-agent   # http://127.0.0.1:18031
```

Prebuilt image (published to GHCR on every push to `main` that touches the image):

```bash
docker pull ghcr.io/adrianofratelli-glitch/atlas-multi-agent-coordination:latest
docker run --env-file .env -p 127.0.0.1:8080:8080 ghcr.io/adrianofratelli-glitch/atlas-multi-agent-coordination:latest
```

Same `--env-file` rule as above (`KEY=value`, no quotes); tags: `latest` and `sha-<7-char commit>`. This image is built without pov-shared (local fallbacks), like `docker build -t multi-agent .`.

`docker run --env-file` does not strip quotes: a line `KEY="value"` arrives with the quotes, so write `KEY=value` in the file you pass. The frontend is built with a same-origin API base (`VITE_API_URL=""`), and no `VITE_ADMIN_KEY` is baked into the image, so the admin-only views (guardrail events, eval runs, agent edits, reviews) are rejected in the container. Health: `GET /api/health` (checks Atlas). Startup warmup calls the LLM; set `WARMUP_ON_START=0` to skip it.

## Tests

```bash
cd backend
pytest -q                       # offline unit + adversarial tests (no network)
(cd ../frontend && npm test)    # frontend unit tests (node --test)
python tests/adversarial/hostile_http.py <url> [--live]    # hostile inputs against a running server
python tests/smoke.py <url>     # black-box
python eval.py <url>            # golden dataset (route + trajectory), results in eval_runs
python eval_answers.py --offline   # answer eval harness with a scripted LLM (no network)

# LIVE mode: REAL Atlas and LLM (costs tokens; disposable identity, cleans up what it wrote)
LIVE=1 pytest tests/test_live.py -q            # ~2 min, essential contracts
LIVE=1 pytest tests/test_live_random.py -q     # ~4 min, random and out-of-scope questions
LIVE=1 pytest tests/test_live_scenarios.py -q  # ~10 min, full journey of the 4 customers
LIVE=1 pytest tests/test_live_web.py -q -s     # web search against real Atlas + LLM + Tavily, isolated *_test databases
LIVE=1 pytest tests/adversarial -q             # adds real double-click races on the isolated *_test database
python eval_situations.py                      # ~10 min, 287 LLM-generated situations against the real agent (dev vs holdout); exits 1 if any case misses
MONGODB_DB=multi_agent_poc_test MONGODB_BRAIN_DB=multiagent_brain_test python eval_answers.py --trials 3   # answer quality: trajectory, effect in the database, LLM judge, pass@k / pass^k (~US$ 0.25 and ~40 s per multi-agent turn)
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
             ├─ fan-out           (order_agent ║ billing_agent, independent questions only)
             ├─ routing           (deterministic rule -> LLM only when unsure)
             ├─ semantic cascade  (short-term -> global cache)  ── HIT ──> cached answer
             │
             ▼
        chain_prepare  ── opens turn_blackboard, orchestrator plan for compound requests
             │
        agent_hop  (one specialist per super-step, checkpoint in Atlas; 5-hop ceiling,
             │      per-agent timeout, loop detector)
             │      tools: find / $vectorSearch / $rankFusion / $graphLookup / gated writes
             ├── handoff (transfer_to_agent or next plan step) ──> advance ──> agent_hop
             ├── write needs approval ──> approval: interrupt() ... analyst decides ──> resume
             ▼
        consolidate  (orchestrator writes one answer from the blackboard)
             │
        finalize  (output guardrail, memory, cache, conversation, Langfuse trace, OTel spans)
```

Every agent, handoff, tool, and LLM call becomes a span carrying `conversation_id`, agent, tokens, estimated cost, and latency. Spans follow the OpenTelemetry GenAI conventions (status "Development"): `invoke_agent {agent}`, `execute_tool {tool}` as its child, and `chat {model}`, with `gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id` and `gen_ai.usage.*`. `backend/scripts/trace_query.py` uses them to answer **who stalled and where**:

```bash
cd backend && TRACE_SINK=atlas ../.venv/bin/python run.py      # spans become Atlas documents
cd backend && ../.venv/bin/python scripts/trace_query.py       # ranking per conversation
conversation           spans errors tokens cost_usd  slowest
conv-8b3ca34a297a         20     2       0  0.00000  agent [order_agent] 3ms  ERROR in tool.orders.find_many
```

## Interoperability: A2A card and MCP server

**A2A 1.0 agent cards, generated from the registry** ([ADR-013](docs/adr/ADR-013-interoperabilidade-a2a-mcp-otel.md)). `GET /.well-known/agent-card.json` is the card of the coordinated service (the orchestrator is the single entry point, `POST /api/chat` with a bearer JWT; each active specialist is one skill); `GET /api/agents/{agent_key}/card` is one agent's card, with its allowed tools as skills. Both are built from `agent_registry` on every request, so switching an agent off removes it from the card. No model name, budget, retrieval config or persona (part of the system prompt) leaves in a card: each skill carries a public role description. Behind a proxy, set `PUBLIC_BASE_URL` so the card advertises the public URL.

**MCP server (stdio, read-only).** `backend/mcp_server.py` exposes the specialists' READ tools (`get_order`, `get_invoice`, `check_warranty`, `get_loyalty_account`, `get_shipment`, `search_catalog`, `search_knowledge_base`) and two resources, `coordination://handoffs/recent` and `coordination://blackboard/latest`, filtered by `agent_registry` on every call (an inactive agent or a tool removed from `allowed_tools` disappears on the next request). Write tools are left out on purpose: their server gate needs the customer's own message, and an MCP client is another agent. Identity comes only from a JWT, re-verified on every call; without a token the server does not start.

```bash
# with the API running on :8031
TOKEN=$(curl -s -X POST http://127.0.0.1:8031/api/auth/token -H 'Content-Type: application/json' \
  -d '{"customer_key":"ana"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
MCP_CUSTOMER_TOKEN=$TOKEN .venv/bin/python backend/mcp_server.py      # stdio

# register it in Claude Code
claude mcp add atlas-multi-agent --env MCP_CUSTOMER_TOKEN=$TOKEN -- .venv/bin/python backend/mcp_server.py
```

The token expires after `JWT_TTL_MINUTES` (an expired token makes every call fail; issue a new one). In `DEMO_MODE` the MCP process seeds its own in-memory store, so it does not see writes made by a separately running API.

## Atlas capabilities as data (2026-10)

- **Support RAG in one server-side `$rankFusion`**, tuned live from the agent's own registry document (`agent_registry.support_agent.retrieval.fusion`: weights, depth per leg, fuzzy, synonyms; read on every hop, no restart, no extra round-trip). The BM25 leg uses Atlas Search **synonyms** (`kb_synonyms`, `lucene.portuguese`: refund/chargeback, defect/damaged, headset/earphone…) and fuzzy as separate clauses, and an optional category filter on both legs. `scoreDetails` explains every article in the timeline ("why these articles"). Measured on 33 synthetic queries: MRR 0.944 → 0.983, recall@3 0.963 → 1.0, same p50 ([ADR-006](docs/adr/ADR-006-kb-rankfusion-config-viva-sinonimos.md)). `$scoreFusion` (MongoDB 8.3+, `minMaxScaler`) is available as data (`retrieval.fusion.method: "score"`) and was measured worse on this small KB (MRR 0.872, recall@1 0.80 vs. 0.983 and 0.967), so `$rankFusion` stays the default.
- **`$rerank` per agent**, as registry data and decided by measurement ([ADR-007](docs/adr/ADR-007-rerank-por-agente-public-preview.md)): on for the product catalog (`rerank-2.5-lite`, recall@1 0.417 → 0.833, turn p95 unchanged), off for the knowledge base (no gain, +185 ms). `rerank-3*` models are Public Preview and labeled as such. A rejected stage is a no-op with a timeline event.
- **LLM economics in a time series collection** (`llm_usage_ts`, granularity `minutes`, 90-day TTL): one measurement per LLM call and per cache HIT; `GET /api/economics?hours=N` aggregates cost per agent per hour and cache savings on the server ([ADR-008](docs/adr/ADR-008-economia-llm-em-time-series.md)).
- **Hardened handoff Change Stream**: owner filter only in the server-side `$match`, per-subscriber resume token and backoff, `resync` event on history loss, cursor closed on disconnect, subscriber caps ([ADR-009](docs/adr/ADR-009-endurecimento-change-stream-handoffs.md)).
- **Live turn in the UI**: `POST /api/chat/stream` sends each timeline event as it happens; the chat view fits one screen with internal scrolling. Why customer memory is not LangGraph's store: [ADR-010](docs/adr/ADR-010-por-que-nao-mongodbstore-langgraph.md).

Every capability fails open: without `pov-shared`, in `DEMO_MODE`, or with a stage rejected by the server, the turn takes the previous path and the event says which one ran. `python backend/scripts/capabilities.py` checks the cluster (read-only).

## Flags (all opt-in; with none of them, behavior is unchanged)

> `TRACE_SINK` and `GUARDRAILS_EDGE` rely on optional helper packages that are not part of this repository. Without them both features fail open and are no-ops.

| Flag | Default | What it enables |
|---|---|---|
| `TRACE_SINK` | `off` | OpenTelemetry tracing: `console`, `phoenix`, or `atlas` (spans become documents) |
| `TRACE_MASK_PII` | forced to `1` | with any sink on, span content is masked; not optional |
| `AGENT_TOOL_CALLING` | `1` (on) | `0` forces every specialist onto its deterministic runner (no tool calling, no plan); without a model this already happens |
| `SUPERVISOR_LEGACY_500` | `0` | **restores** the old behavior: an agent failure surfaces as a 500. Graceful degradation is the default |
| `AGENT_TIMEOUT_SECONDS` | `45` | per-agent-hop ceiling (always on) |
| `LOOP_GUARD_REPEATS` | `2` | repetitions of (agent, intent) before escalating to a human |
| `TOOL_TIMEOUT_SECONDS` | `0` (off) | per-tool-call ceiling, including outside the agent hop |
| `TOOL_BREAKER` | `1` (on) | per-tool circuit breaker: 4 **consecutive** failures open it for 30s; `0` disables |
| `MONGODB_TEST_DB` / `MONGODB_TEST_BRAIN_DB` | `<database>_test` | isolated databases used by scripts that write real data |
| `ALLOW_DEMO_DB_WRITE` | `0` | escape hatch: lets those scripts write to the demo database |
| `GUARDRAILS_EDGE` | `0` | `mask_pii` in logs and `validate_output` (`max_repairs=0`) on the response |
| `MAX_HANDOFF_SUBSCRIBERS` / `MAX_HANDOFF_SUBSCRIBERS_PER_CUSTOMER` | `100` / `5` | caps on live handoff feeds (each one is a change stream cursor); above them, HTTP 429 |
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

[Architecture](docs/architecture.md) · [Code map](docs/MAPA_DO_CODIGO.md) · [ADR-001: document-oriented coordination](docs/adr/ADR-001-arquitetura-multi-agente.md) · [ADR-002: LLM memory and personal turns outside the cache](docs/adr/ADR-002-memoria-llm-e-turno-pessoal.md) · [ADR-003: two-band guardrail and out of scope](docs/adr/ADR-003-guardrail-em-duas-faixas.md) · [ADR-004: embedding scope and situation-based measurement](docs/adr/ADR-004-escopo-por-embedding-e-medicao-por-situacoes.md) · [ADR-005: governed, deterministic web search](docs/adr/ADR-005-busca-web-deterministica-governada-pelo-atlas.md) · [ADR-006: KB `$rankFusion` as live config](docs/adr/ADR-006-kb-rankfusion-config-viva-sinonimos.md) · [ADR-007: `$rerank` per agent](docs/adr/ADR-007-rerank-por-agente-public-preview.md) · [ADR-008: LLM economics in a time series](docs/adr/ADR-008-economia-llm-em-time-series.md) · [ADR-009: hardened handoff Change Stream](docs/adr/ADR-009-endurecimento-change-stream-handoffs.md) · [ADR-010: why not LangGraph's MongoDB store](docs/adr/ADR-010-por-que-nao-mongodbstore-langgraph.md) · [ADR-011: tool calling, plan, blackboard, consolidation](docs/adr/ADR-011-tool-calling-plano-quadro-e-consolidacao.md) · [ADR-012: human approval with interrupt](docs/adr/ADR-012-aprovacao-humana-com-interrupt.md) · [ADR-013: A2A, MCP, OTel GenAI](docs/adr/ADR-013-interoperabilidade-a2a-mcp-otel.md) · [Chaos report](docs/chaos-report.md) · [Eval format](eval/FORMAT.md)

The ADRs and briefing documents are written in Brazilian Portuguese.

## License

MIT, see [LICENSE](LICENSE).
