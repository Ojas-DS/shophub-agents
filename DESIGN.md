# DESIGN

## 1. Architecture

```
                 ┌──────────────────────── Orchestrator :8002 (FastAPI + LangGraph) ────────────────────────┐
  staff client   │ POST /chat ─► require_identity ─► graph:  START ─┬► resume_remote ─(decided)─► END       │
  ──SSE────────► │  (Bearer)     (store /auth/me,               │      │ (pending A2A task?)              │
                 │               403 if no merchant)            │      └─(not a decision)─┐               │
                 │                                              └► agent (LLM) ◄──────────┘               │
                 │                                                  │ tool call ask_orders_agent          │
                 │                                                  ▼                                     │
                 │                                              call_agents ──(input-required)──► END     │
                 │   AgentRegistry: GET /.well-known/agent-card.json at startup -> one tool per agent       │
                 └───────────────────────────────────┬─────────────────────────────────────────────────────┘
                     A2A JSON-RPC 2.0  message/stream (SSE)  Authorization: <same token>  X-Correlation-ID
                 ┌───────────────────────────────────▼──── Orders Agent :8001 (a2a-sdk + LangGraph) ───────┐
                 │ StoreAuthMiddleware (store /auth/me, fail closed) ─► TenantScopedRequestHandler          │
                 │  (task stamped with owner merchant) ─► OrdersAgentExecutor (1 A2A task = 1 graph thread) │
                 │  graph: agent ─► read_tools (list_orders, lookup_order) ─► agent ─► END                 │
                 │               └► approval_gate: build proposal ─► interrupt() ─► approve? execute : drop│
                 └───────────────────────────────────┬─────────────────────────────────────────────────────┘
                                 HTTP + same token   ▼
                 Store API :8000 (in-memory, every endpoint scoped to the token's merchant)
                 PostgreSQL: orchestrator checkpoints | orders-agent checkpoints + A2A tasks
```

## 2. One turn, end to end ("Refund order 1042", then "approve")

1. `POST /chat`. The orchestrator validates the token against the store's `/auth/me` before any model call. An invalid token gets 401; a valid token with no merchant gets 403. The thread id is namespaced as `merchant:user:thread`.
2. The user's text is PII-redacted and appended to the thread. There is no pending task, so the graph goes to `agent`. The LLM sees the agents it discovered from their cards, and calls `ask_orders_agent(request=...)`.
3. `call_agents` sends A2A `message/stream` to the URL in the card, with the caller's own `Authorization` header and the correlation id. Status events stream back and are relayed to the client as SSE `remote_status` events.
4. The Orders Agent's middleware re-validates the token with the store, so the system still fails closed even if the orchestrator is bypassed. The executor creates a task stamped with the merchant id and runs its graph. The LLM calls `refund_order`, but because that is an *action tool* it is routed to `approval_gate`. The gate looks up the order with the caller's token, checks the state machine, builds a proposal (order, merchant, customer, `PAID -> REFUNDED`, amount), and calls `interrupt()`. The A2A task becomes **`input-required`**, and the proposal is returned as a TextPart plus a DataPart.
5. The orchestrator stores `pending = {agent, task_id}` in its checkpoint, shows the proposal verbatim (amounts are never reworded by an LLM), and ends the turn. No data has changed.
6. Next turn, "approve". Because `pending` is set, the graph goes to `resume_remote`. It forwards the raw text to the same A2A task, and **no LLM is involved**. The executor sees the graph's interrupt and resumes it with `Command(resume=text)`. The gate re-runs from the top, which rebuilds the proposal from **live** data using the approver's token, then `parse_decision("approve")` returns `approve`, then `POST /orders/refund`. The task is `completed` with `{"decision": "approved"}`.

## 3. Key decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| **HITL lives in the agent that owns the action** (A2A `input-required`), not in the chat API. There are no `decision` / `pending_approval` fields in the API: approval is a plain chat message. | The orchestrator only knows the generic A2A rule "a task waiting for input gets the next message". A future payments or address agent brings its own gate and needs no orchestrator change. | One A2A round trip per approval. |
| **The approval decision is parsed deterministically** (`decisions.py`), not by an LLM. Anything that is not a clear "approve" is treated as "not approved". | Approval is a security decision. A regex cannot be prompt-injected, and it is fully unit-tested. | English-only phrasing. A real-model classifier constrained to the enum is an easy upgrade, and it would only ever see the human's text. |
| **The proposal is rebuilt on resume** (LangGraph re-runs the interrupted node). | Prevents executing stale intent if the order changed while waiting (this is tested). | One extra lookup per approval. |
| **The token lives in contextvars**, never in graph state or `configurable`. | Checkpoint state is persisted. A test greps the SQLite files to show the token is never written. | Requires discipline: every outbound call goes through `outbound_headers()`. |
| **Identity is checked at every hop** via the store's `/auth/me`. | The store is the source of truth, and nothing is cached across users. | One extra store call per hop (cheap; cacheable per token for a few seconds if needed). |
| **A2A tasks are stamped with the owning merchant**, and foreign access returns "task not found". | Task ids are addressable over JSON-RPC (`tasks/get`, `message/send` with `taskId`). | Small subclass of the SDK's request handler. |
| **Delegation is natural language** (`ask_orders_agent(request)`), not remote function calls. | This is how A2A is meant to work: the sub-agent owns its tools and its schemas can change without touching the orchestrator. | Two LLM hops per read question, which roughly doubles cost and latency. |
| **Guardrails are layered**: field allow-list, then regex PII redaction over all text (including the user's own message and anything crossing an agent boundary), then quarantine of instruction-like notes, then data fencing. | No single layer is trusted. The *structural* layer (actions only become proposals) holds even if detection misses. A test switches the guardrail off and shows the gullible fake model gets hijacked but still cannot change data. | The injection detector is a heuristic, with false positives (an innocent "refund now" note is withheld) and false negatives (novel phrasing). |
| **a2a-sdk 0.3.26 pinned** (protocol 0.3, JSON-RPC). | Stable, widely deployed wire format. SDK 1.x moved to protobuf types. | A migration later; it is contained to `orders_agent/server.py`, `security.py` and `orchestrator/registry.py`. |
| **Postgres in docker, SQLite or memory locally**, one factory. | The spec asks for Postgres, and tests stay hermetic. | None in practice. |

**Fake LLM.** `ScriptedChatModel` is a real `BaseChatModel`: it supports `bind_tools`, streams token chunks, and emits tool calls. Each role has a rule-based policy. Two properties make the tests meaningful. First, `TRANSCRIPT` records everything any model saw, so a test can assert that no email or phone number ever reached a model. Second, the orders policy is deliberately *gullible*: it obeys instructions it finds in tool data. The injection tests therefore fail if the guardrail is removed.

## 4. Cost and latency per turn (estimates)

The numbers below are estimates for `claude-haiku-4-5` at $1 per million input tokens and $5 per million output tokens. **They are not measured runs**: development and CI used the fake model.

| Turn type | LLM calls | ~Input / output tokens | ~Cost | ~Latency |
|---|---|---|---|---|
| Read question ("paid not shipped") | 4 (orchestrator ×2, orders agent ×2) | 5.5k / 0.4k | ≈ $0.0075 | 3–5 s (calls are sequential) |
| Refund request (proposal) | 2 (gate is deterministic) | 2.5k / 0.1k | ≈ $0.003 | 1.5–2.5 s |
| "approve" / "reject" | **0** | 0 | $0 | ~100 ms (3 HTTP hops + DB) |
| Fake mode, any turn | 0 | 0 | $0 | 50–200 ms |

The biggest saving would be to let the orchestrator relay a completed sub-agent answer without a second LLM call. That cuts a read turn to 3 calls, about 25% less. Prompt caching of the system prompt plus tool schemas (stretch S4) would also help.

## 5. What I cut, and why

- **Docker could not be run in my build sandbox** (its container registry was blocked). Instead, I verified the exact processes and env vars from `docker-compose.yml` natively against **PostgreSQL 16**: the services started, the A2A card was discovered, the SSE stream worked, a refund was proposed, **both agents were killed and restarted**, the thread history and the pending approval were still there, "approve" executed, the correlation id appeared in all three services' logs, and the eval harness against the live stack passed 11/11. The compose file uses only standard images and the same commands.
- **No real-model eval run is recorded here**, because I had no API key while building. The harness supports it with `--base-url` against a stack started with `LLM_PROVIDER=anthropic`. I expect the first cases to need system-prompt tuning; for example, the orchestrator must restate the request completely when delegating.
- **The injection detector is regex-based.** The next step would be a small classifier model as a second opinion. The structural guarantee (no action without human approval) is what I rely on.
- **PII detection is regex-based** (emails and international phone formats). Microsoft Presidio, or an NER pass, would also catch addresses and names in notes.
- **One action per approval**, with no proposal expiry and no idempotency key on refund. In production, the store's refund would take an idempotency key derived from the A2A task id.
- **Message history is unbounded.** It needs trimming or summarising after N turns.
- **Stretch goal: only S3 (correlation id)** was done, because it was cheap once headers were forwarded on every hop.

## 6. With another week

1. Run the evals against the real model (3 seeds per case), tune the prompts, and add the eval run to CI. Then add S2: per-turn token and cost events in the SSE stream, plus a per-merchant rate limit.
2. Approval hardening: TTL on pending tasks, idempotency keys, an audit log of who approved what, and role checks (only managers can approve refunds above $X).
3. Agent-to-agent trust: signed Agent Cards (supported in A2A 0.3), mTLS or workload identity between agents, and an allow-list of card URLs.
4. OpenTelemetry traces across A2A hops (the correlation id becomes the trace id), and migration to a2a-sdk 1.x.
5. Add a second sub-agent (for example, customers) to show multi-agent routing beyond one card. The registry and graph already support any number of agents.

## 7. Use of AI assistance

I used an AI coding assistant (Claude) to help with scaffolding, tests and documentation. I reviewed, ran and can explain every line. The design decisions above, especially where HITL lives, the deterministic approval parser, and keeping credentials out of state, were deliberate choices, informed by how the reference A2A and LangGraph documentation recommend these patterns.
