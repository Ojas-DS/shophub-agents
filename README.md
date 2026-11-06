# ShopHub order-support agents

A multi-tenant support assistant for ShopHub merchant staff, built from **two LangGraph agents that talk over Google's A2A protocol**:

- **Orchestrator** (`:8002`) — the chat API. It finds specialist agents by reading their A2A Agent Cards, and streams answers over SSE.
- **Orders Agent** (`:8001`) — an A2A server (JSON-RPC 2.0). It owns the order tools and the human-approval gate for refunds and cancellations.
- **Mock store API** (`:8000`) — the in-memory backend. It enforces tenant isolation itself.
- **PostgreSQL** — LangGraph checkpoints for both agents, plus the A2A task store.

```
client ──SSE──► Orchestrator ──A2A JSON-RPC (message/stream)──► Orders Agent ──HTTP──► Store API
                (LangGraph)      discovered via agent card        (LangGraph +          (tenant-scoped)
                                                                   interrupt gate)
          the caller's bearer token is forwarded unchanged on every hop ─────────────────────────►
```

Architecture, trade-offs, what was cut, and cost/latency are in [DESIGN.md](DESIGN.md).

## Quick start (Docker, no API key)

```bash
docker compose up --build            # postgres + store + orders agent + orchestrator
python scripts/chat_client.py "Which paid orders have not shipped in 3 days?"
python scripts/chat_client.py "Refund order 1042" --thread demo     # -> proposal, nothing changes
python scripts/chat_client.py "approve" --thread demo               # -> executes
```

The chat client only needs `httpx` (`pip install httpx`). You can use curl instead:

```bash
curl -N localhost:8002/chat -H "Authorization: Bearer acme-staff-token" \
     -H "Content-Type: application/json" -d '{"message":"Refund order 1042","thread_id":"demo"}'
```

Demo credentials (fixtures of the in-memory mock, not secrets):

| Token | User | Merchant |
|---|---|---|
| `acme-staff-token` | alice | merchant_a, Acme Outfitters |
| `bolt-staff-token` | bob | merchant_b, Bolt Electronics |
| `platform-auditor-token` | auditor | **none**, so every request is rejected with 403 (fail closed) |

## Local setup (no Docker)

```bash
python3.12 -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install -e ".[dev]"
./scripts/run_local.sh          # 3 processes on :8000/:8001/:8002, SQLite persistence
python scripts/chat_client.py -i  # interactive chat
```

## Tests (no API key, no network)

```bash
pytest
```

The suite has 57 tests and runs in about 10 seconds. The two mandatory tests are `tests/test_tenant_isolation.py` (R3) and `tests/test_hitl.py` (R5). The tests run all three services in one process, and each service runs in its own empty context. So the agents still find each other through the Agent Card and talk over real A2A JSON-RPC and HTTP, and the only thing that crosses a service boundary is real HTTP headers.

## Eval harness

```bash
python -m evals.run_evals                                   # fake LLM, in-process, PASS/FAIL per case
python -m evals.run_evals --base-url http://localhost:8002  # against a running stack (fresh one)
```

There are 11 cases: correct tool chosen, correct answer substring, the legacy envelope, proposal raised, approve, reject, injection ignored (2 cases), cross-merchant refused (2 cases), and fail-closed. Each case is reported on its own, with no blended score. Cases are defined in `evals/cases.json`.

## Model provider

There is one factory, `shophub/llm/factory.py::make_chat_model`, and the `LLM_PROVIDER` variable selects the provider:

| `LLM_PROVIDER` | What runs |
|---|---|
| `fake` **(default)** | `ScriptedChatModel`, a deterministic, rule-based LangChain chat model. No key and no network. |
| `anthropic` | `ChatAnthropic` (default `claude-haiku-4-5`; override with `ANTHROPIC_MODEL`). Needs `ANTHROPIC_API_KEY`. |

**Exact fake-LLM command:** `LLM_PROVIDER=fake docker compose up --build`. `fake` is also the default, so plain `docker compose up --build` does the same.
Real model: `LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... docker compose up --build`.

## Demos

| Requirement | How to see it |
|---|---|
| R2 discovery | `curl localhost:8001/.well-known/agent-card.json` and `curl localhost:8002/agents -H "Authorization: Bearer acme-staff-token"` |
| R6 streaming | `python scripts/chat_client.py "List all orders"` prints `tool_started`, `remote_status`, `tool_finished` and `token` events as they arrive |
| R7 restart | `./scripts/demo_restart.sh` asks for a refund, restarts both agents, shows the thread, approves, and the refund executes |
| S3 correlation id | `docker compose logs` shows the same `correlation_id` on the orchestrator, orders-agent and store-api log lines for one turn |

## Endpoints

- Orchestrator: `POST /chat` (SSE), `GET /threads/{id}`, `GET /agents`, `GET /healthz`
- Orders Agent: `GET /.well-known/agent-card.json`, `POST /` (A2A JSON-RPC: `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`), `GET /healthz`
- Store: `GET /auth/me`, `GET /orders?status=`, `GET /orders/lookup?id=` (legacy envelope), `POST /orders/refund`, `POST /orders/cancel`

## Repository layout

```
src/shophub/
  config.py                 env-driven settings
  common/                   request context (token, correlation id), guardrails, checkpointer factory, JSON logs
  llm/                      model factory + scripted fake model
  store_api/                R0 mock store: models, seed data, app
  orders_agent/             A2A server: agent card, auth/tenant security, executor, LangGraph + approval gate, tools
  orchestrator/             agent registry (discovery + A2A client), LangGraph, SSE chat API
  testing.py                in-process 3-service stack for tests/evals
tests/  evals/  scripts/  docker/
```
