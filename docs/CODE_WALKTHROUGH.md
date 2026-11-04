# Code walkthrough

This document walks through the codebase in the order a request travels, from the entry point to the response. It covers every file, every block and why each one exists. Line numbers refer to the current `main` branch.

---

## Part 1 - The big picture

### 1.1 Processes

| # | Process | Port | Module | What it is |
|---|---|---|---|---|
| 1 | Store API | 8000 | `shophub.store_api` | Mock backend: in-memory orders, bearer-token auth, tenant scoping |
| 2 | **Orders Agent** | 8001 | `shophub.orders_agent` | **Agent #2.** An A2A server. Its own LangGraph agent with its own LLM, order tools and approval gate |
| 3 | **Orchestrator** | 8002 | `shophub.orchestrator` | **Agent #1.** The chat API. Its own LangGraph agent with its own LLM; it discovers agents and delegates to them |
| 4 | PostgreSQL | 5432 | — | Checkpoints for both agents + the A2A task table |

### 1.2 Where the agents are, and how they find each other

There are **two LLM agents**, each a separate LangGraph graph in a separate process:

1. **Orchestrator agent** (`orchestrator/graph.py`). It talks to the human, decides *who* should handle a request, and relays answers. It has **no store tools**. Its only tools are "ask agent X".
2. **Orders agent** (`orders_agent/graph.py`). It owns the order tools (`list_orders`, `lookup_order`, `refund_order`, `cancel_order`) and the human-approval gate.

**Discovery (A2A Agent Card).**

- The orders agent publishes a JSON "business card" at `GET /.well-known/agent-card.json`. The card is built in `orders_agent/server.py::build_agent_card` (line 42). It holds the agent's name, description, endpoint URL, skills, the auth scheme (bearer), and capabilities (streaming).
- The orchestrator is configured with only a list of base URLs: env `AGENT_CARD_URLS`, set to `http://orders-agent:8001` in docker.
- At startup, `orchestrator/api.py` line 66 calls `registry.discover()`. That runs `orchestrator/registry.py::discover` (line 83), which uses the SDK's `A2ACardResolver` to fetch each card (line 86).
- Each discovered card becomes one LangChain tool named `ask_<card.name>`, for example `ask_orders_agent` (`registry.tools`, line 107). The tool's description is built from the card's description and skills. The LLM chooses the agent from that text.
- **The orchestrator never imports orders-agent code.** To add a payments agent, you add a URL to `AGENT_CARD_URLS`, and the new card becomes a new tool automatically.

### 1.3 Every place the A2A protocol is used

| What | Where | SDK piece |
|---|---|---|
| Agent Card (discovery document) | `orders_agent/server.py` `build_agent_card` | `a2a.types.AgentCard`, `AgentSkill`, `AgentCapabilities`, `SecurityScheme` |
| Serving the card + JSON-RPC 2.0 endpoint | `orders_agent/server.py` `create_app` → `A2AStarletteApplication(...).routes()` | `a2a.server.apps` |
| Handling JSON-RPC methods (`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`) | `orders_agent/security.py` `TenantScopedRequestHandler` (subclass of the SDK's `DefaultRequestHandler`) | `a2a.server.request_handlers` |
| Doing the work for a task | `orders_agent/executor.py` `OrdersAgentExecutor.execute` | `AgentExecutor`, `RequestContext`, `EventQueue`, `TaskUpdater` |
| Task lifecycle states | executor: `start_work()` → *working*, `requires_input()` → **input-required**, `complete()`, `failed()` | `TaskState` |
| Persisting tasks across restarts | `server.py` `_make_task_store` → `DatabaseTaskStore` (Postgres) | `a2a.server.tasks` |
| Fetching cards | `orchestrator/registry.py` `discover` | `A2ACardResolver` |
| Sending messages, streaming events | `orchestrator/registry.py` `send` | `ClientFactory`, `ClientConfig`, `Message`, `Part`, `TextPart`, `DataPart` |
| Continuing a task (approval) | `registry.send(..., task_id=...)`, called from `orchestrator/graph.py` `resume_remote` | A2A rule: a message carrying the `taskId` of a task in `input-required` continues it |

**What one A2A call looks like on the wire:**

```http
POST http://orders-agent:8001/
Authorization: Bearer acme-staff-token        <- the user's own token, unchanged
X-Correlation-ID: 4ca7...                     <- S3
Content-Type: application/json

{"jsonrpc":"2.0","id":"...","method":"message/stream",
 "params":{"message":{"role":"user","messageId":"...","contextId":"...","taskId":null,
           "parts":[{"kind":"text","text":"Refund order 1042"}]}}}
```

The response is an SSE stream of JSON-RPC results:

1. `Task` (state `submitted`)
2. `TaskStatusUpdateEvent` (state `working`, with "tool_started: lookup_order")
3. the final `TaskStatusUpdateEvent`, state `input-required`, carrying the proposal as text plus data

### 1.4 The 30-second explanation

> "Staff chat with an orchestrator agent over SSE. It discovers specialist agents from their A2A Agent Cards and delegates over A2A JSON-RPC. The orders agent owns the order tools. Reads run immediately. Refunds and cancels hit a LangGraph `interrupt`, which puts the A2A task into `input-required`. The user's next message is routed to that same task, and a deterministic parser decides approve or reject. The user's bearer token travels unchanged on every hop and is re-validated against the store at each one. State lives in Postgres checkpoints, and the token never does."

---

## Part 2 - Request walkthroughs

### 2.1 "Which paid orders have not shipped in 3 days?"

1. `scripts/chat_client.py` POSTs to `/chat` with `Authorization: Bearer acme-staff-token`.
2. `orchestrator/api.py` `require_identity` (line 77) calls the store's `/auth/me`. The result is Alice, merchant_a. The token and correlation id are stored in contextvars (lines 82-83).
3. `chat` (line 135) starts an SSE `StreamingResponse`. Inside `stream()`, the PII-redacted message becomes a `HumanMessage` (line 152), and the graph runs with `astream_events` (line 153).
4. Graph `route_entry`: there is no pending task, so it goes to `agent`. The LLM is bound to `ask_orders_agent` and calls it with `request="Which paid orders..."`.
5. `call_agents` emits `tool_started` and then calls `registry.send`. The A2A `message/stream` goes out with the same Authorization header (`registry.py` line 129).
6. Orders agent: `StoreAuthMiddleware` (`security.py` line 59) calls the store's `/auth/me` with the forwarded token. An `Identity` is placed in the ASGI scope, `IdentityContextBuilder` copies it into the A2A call context, and the handler checks ownership (a new task, so there is nothing to check).
7. `OrdersAgentExecutor.execute`: it creates the task, stamps the owner, sets `thread_id=orders:merchant_a:<task_id>`, and runs the orders graph.
8. Orders graph: `agent` → LLM calls `list_orders(status=PAID, older_than_days=3)`. `route` sends this to `read_tools` (a `ToolNode`). The tool calls the store's `GET /orders?status=PAID`, filters by age, and passes every order through `safe_order` (PII stripped, notes neutralised). The JSON goes back to `agent`, and the LLM writes the answer, then END.
9. Executor: no interrupt, so `updater.complete(...)` with the answer text.
10. Orchestrator: `call_agents` adds a `ToolMessage`, goes back to `agent`, and the LLM relays the answer. Tokens stream as SSE `token` events (`api.py` line 155), then `final`.

### 2.2 "Refund order 1042" → "approve"

Turn 1:

- The orders agent's LLM calls `refund_order(order_id=1042)`. `route` sees an **action tool** and sends it to `approval_gate` (`orders_agent/graph.py` line 101).
- `build_proposal` (line 76) looks up the order live with the caller's token, checks that `PAID` is refundable, and builds the proposal dict.
- `reply = interrupt(proposal)` (line 126). LangGraph saves the checkpoint and stops here.
- Executor: `snapshot.interrupts` is non-empty (line 74), so `updater.requires_input(...)`. The A2A task is now **input-required**. The message contains a `TextPart` (human-readable) and a `DataPart` (`{"proposal": {...}}`).
- Orchestrator `call_agents` (lines 97-103): it saves `pending={agent_tool, task_id, context_id, proposal}` in its own checkpoint, emits the SSE `proposal` event, appends the proposal text as the AI message, and the graph ENDs (`route_after_call`).

Turn 2 ("approve"):

- `route_entry` sees `pending` and goes to `resume_remote` (line 119). This step uses **no LLM**. It takes the raw human text and calls `registry.send(..., task_id=pending.task_id)`.
- Orders agent: the middleware re-authenticates, and the handler verifies the task belongs to this merchant. The executor finds `snapshot.interrupts` (line 57) and resumes with `Command(resume="approve")`.
- LangGraph re-runs `approval_gate` from the top. The proposal is rebuilt from live data. `interrupt()` now *returns* "approve" instead of pausing. `parse_decision` returns `approve`, and `store.refund_order(1042)` runs. The result is "Done: order #1042 is now REFUNDED".
- Executor `complete(...)` with DataPart `{"outcome": {"decision": "approved", ...}}`. Orchestrator: `pending=None`, and the answer is shown.

Other outcomes:

- **Reject:** `parse_decision` returns `reject`, so nothing executes.
- **Something else** ("show me order 1006"): `unclear`. The orders agent discards the proposal, the orchestrator emits a `notice`, and `route_after_resume` sends the message to `agent` as a new question.

### 2.3 Restart

- Orchestrator checkpoint (Postgres) holds the messages and `pending`.
- Orders-agent checkpoint (Postgres) holds the interrupted graph.
- The A2A `tasks` table (Postgres, `DatabaseTaskStore`) holds the task in `input-required`.

After both processes restart, "approve" finds all three pieces and continues. This is tested in `tests/test_persistence.py` and shown live by `scripts/demo_restart.sh`.

---

## Part 3 - File by file

### `src/shophub/config.py`

- `Settings(BaseSettings)` (line 14) reads **every** setting from environment variables, or from `.env` in local dev.
- `llm_provider` is `fake` by default, so nothing needs a key. `anthropic_model` defaults to Haiku (cheap).
- `store_api_url`, `orders_agent_public_url` (the URL written into the Agent Card), and `agent_card_urls` (where the orchestrator looks for cards).
- Three persistence URLs: one per checkpointer, plus the A2A task store. `memory`, `sqlite:///` and `postgresql://` are all supported.
- `guardrails_enabled` exists only so one test can prove the guardrail matters.
- `get_settings()` is `lru_cache`d: settings are read once per process.

### `src/shophub/common/request_context.py`

- `bearer_token` and `correlation_id` are **ContextVars**: values scoped to the current asyncio task, which here means the current HTTP request. **Why:** the token must be forwarded but never persisted. LangGraph state is checkpointed to Postgres, and even `config["configurable"]` gets copied into checkpoint metadata. A ContextVar is never saved anywhere.
- `Identity` (line 24) is user, merchant_id and merchant_name, as returned by the store's `/auth/me`. `current_identity` holds it for the current request.
- `outbound_headers()` (line 36) is the **only** way downstream calls get credentials. It returns `{"Authorization": <exact incoming value>, "X-Correlation-ID": ...}`. It **raises if there is no token**, so the system fails closed instead of making an unauthenticated call.

### `src/shophub/common/logging.py`

- `JsonFormatter` writes one JSON object per log line, including `correlation_id` read from the ContextVar (S3). Extra fields can be passed with `extra={"fields": {...}}`.
- `setup_logging` installs the formatter and quietens `httpx`.

### `src/shophub/common/guardrails.py` (R4)

Read the module docstring first: it describes four layers.

- Lines 27-46: email and phone regexes. The phone pattern needs at least 7 digits and excludes ISO dates (`_DATE_RE`, line 37), so "placed 2026-10-30" and "$129.99" are not redacted.
- `redact_pii` (line 48) and `redact_obj` (line 54, recursive over dicts and lists).
- `_INJECTION_PATTERNS` (line 69) lists the signals that a piece of data is talking to an AI:
  - role prefixes (`assistant:`, `system:`)
  - "ignore/disregard ... instructions"
  - "you are now / act as / admin mode"
  - urgent action verbs ("refund ... immediately")
  - exfiltration ("send ... customer data")
  - fake `<system>` tags
- `_normalize` (line 83) applies NFKC folding, so full-width letters like `ｉｇｎｏｒｅ` become `ignore`, and strips zero-width and control characters that hide text from regexes.
- `neutralize_note` (line 94): a flagged note is **replaced entirely** by a fixed placeholder, so the model never sees the text. A clean note is wrapped in `<untrusted_customer_note>` tags; the prompts say tag content is data.
- `safe_order` (line 107) is an **allow-list** projection: only id, customer_name, status, total, placed_at and the neutralised note. Email and phone are dropped at the field level, then everything is regex-redacted again (defence in depth). The `enabled=False` path exists only for the "prove the guardrail matters" test.

### `src/shophub/common/checkpoint.py` (R7)

`make_checkpointer(url)` is an async context manager that returns the right LangGraph saver:

- `AsyncPostgresSaver`, with `setup()` to create its tables
- `AsyncSqliteSaver`
- `InMemorySaver`

Both agents use it, so swapping storage is a config change.

### `src/shophub/llm/factory.py`

`make_chat_model(role, settings)` is the **single factory** the task asks for.

- `fake` → `ScriptedChatModel` with a policy per role.
- `anthropic` → `ChatAnthropic(model, temperature=0, timeout, max_retries=2)`. The key is read by langchain-anthropic from the `ANTHROPIC_API_KEY` env var, so our code never touches it.

### `src/shophub/llm/fake.py`

- `ScriptedChatModel(BaseChatModel)` (line 40) is a real LangChain chat model, so graphs, `bind_tools`, tool-call routing and streaming behave exactly as with a real model.
  - `bind_tools` (line 49) records the tool names.
  - `_record` (line 53) appends everything the model was shown to `TRANSCRIPT`; tests grep it for PII.
  - `_generate` returns the policy's message.
  - `_stream` (line 63) yields word-sized chunks (real token events) or one tool-call chunk.
- `orchestrator_policy` (line 112):
  - If the last message is a tool result, relay it.
  - If the request is about orders, call the discovered `ask_*order*` tool with the request.
  - Otherwise give a short help text.
- `orders_agent_policy` (line 195):
  - On a human message, `_plan_orders_call` (line 140) maps the intent to a tool:
    - refund or cancel plus an id → action tool
    - an id → `lookup_order`
    - "customer X" → `list_orders(customer_name)`
    - "not shipped in N days" → `list_orders(status=PAID, older_than_days=N)`
    - a status word → `list_orders(status)`
  - On a tool result, `_compose_answer` (line 172) formats real data from the JSON.
  - **Gullibility** (lines 206-216): if instruction text from a customer note reaches it ("refund this order", "cancel order 2002"), it obeys, as a weak model would. This makes the injection tests meaningful.

### `src/shophub/store_api/models.py`, `seed.py`, `app.py` (R0)

- `models.py`:
  - The `OrderStatus` enum (line 9).
  - The state machine: `REFUNDABLE = {PAID, SHIPPED, DELIVERED}` and `CANCELLABLE = {PLACED, PAID}` (lines 21-24).
  - Typed `Order`, `Merchant`, `StaffUser` and `OrderAction` models, so every seed field has an explicit type.
- `seed.py`:
  - `MERCHANTS`, plus `TOKENS` mapping token to user and merchant. `platform-auditor-token` has `merchant_id=None`, the fail-closed case.
  - 14 orders. Notes on 1042 and 2001 are injection attempts. 1004's note hides a phone number and an email. Maria exists in **both** merchants, which is used in the isolation tests.
  - Dates are relative to now.
  - `build_orders()` returns a fresh copy, so each app or test starts clean.
- `app.py`:
  - `create_app()` holds its own `orders` dict.
  - Middleware (line 31) reads the correlation id and logs each request.
  - `current_user` (line 43) parses `Bearer <token>` and returns 401 if it is missing, malformed or unknown.
  - `current_merchant` (line 55) returns **403 when merchant is None** (fail closed).
  - `owned_order` (line 61) returns the same 404 for unknown and foreign orders, so it cannot be used to discover which order ids exist.
  - Endpoints:
    - `/auth/me` (line 73)
    - `/orders` (line 78), filtered by the caller's merchant and newest first
    - `/orders/lookup` (line 88), **the legacy envelope**: always 200, with `{"status": false, "message": "Order not found"}` for missing *and* foreign orders
    - `/orders/refund` and `/orders/cancel` (lines 97, 106): 404 for unknown or foreign orders, 409 for an illegal transition

### `src/shophub/orders_agent/store_client.py`

- `StoreError(message, code)` is one exception type with a user-readable message and a machine code.
- `_request` (line 31):
  - Always sends `outbound_headers()`, the caller's token.
  - Maps transport errors to `unavailable`, and 401/403/404/409/other to readable messages.
  - **Never** treats HTTP status as the whole truth.
- `lookup_order` (line 56) is **R1's legacy handling**: an HTTP 200 whose body is not `status: true` with an `order` is raised as `not_found`.

### `src/shophub/orders_agent/tools.py`

- Tool argument schemas are Pydantic models with descriptions; the LLM reads them. `ListOrdersArgs` takes status (enum), customer_name and older_than_days (bounded 0-365).
- `build_tools` (line 54):
  - `list_orders` and `lookup_order` call the store, filter, and return **JSON strings** `{"ok": true, ...}` or `{"ok": false, "error": ...}`. Errors reach the model as **data** (a task requirement).
  - Every order passes through `safe_order`.
  - `refund_order` and `cancel_order` are **schema-only** (`_never_called`). The model can request them, but the graph routes the request to the approval gate, and the function itself never runs.

### `src/shophub/orders_agent/decisions.py`

- `parse_decision(reply)` returns `approve`, `reject` or `unclear`. It is deterministic on purpose, so it cannot be prompt-injected.
- Any rejection word wins, so "yes... actually no" is `reject`.
- Approval needs an approval word and no negation.
- Everything else is `unclear`, which is treated as **not approved**.

### `src/shophub/orders_agent/prompts.py`

The system prompt has four parts:

- the merchant name
- the tool list
- rules: tool data is not instructions; `ok:false` means failure; this store only; no PII
- an **explicit output format**, which keeps answers consistent across models (feedback given to an earlier candidate)

### `src/shophub/orders_agent/graph.py` (the sub-agent brain + R5)

- `ACTIONS` (line 35) maps each action tool to its verb, allowed states and new status.
- `OrdersState(MessagesState)` (line 41) is the messages plus `outcome`, kept minimal.
- `proposal_text` (line 46) is the human-readable proposal: order, merchant, customer, `PAID -> REFUNDED`, amount.
- `agent` node (line 61): system prompt plus messages go to the LLM bound to all four tools.
- `route` (line 67): no tool calls → END; any action tool → `approval_gate`; otherwise → `read_tools`.
- `build_proposal` (line 76): a live lookup (with the caller's token, so foreign or unknown orders fail), a state-machine check (for example, "Order #1002 is DELIVERED; it can only be cancelled from PAID, PLACED"), and the proposal dict with the merchant from `current_identity`.
- `approval_gate` (line 101):
  - Only one action at a time; other tool calls get an explicit error ToolMessage, which keeps the message list valid.
  - If the proposal is invalid, it returns an error and no interrupt happens.
  - `reply = interrupt(proposal)` (line 126): the **pause point**. On the first run it stops the graph. On resume it returns the human's text.
  - LangGraph re-runs the node on resume, so the proposal is **re-validated against live data** before executing.
  - Not approved: nothing executes, and `outcome.decision` is `rejected` or `unclear`.
  - Approved: `store.refund_order` or `cancel_order` runs (the store re-checks tenant and state, returning 404/409), and a deterministic summary is returned.
- Wiring (lines 159-167): START → agent → (read_tools → agent)* → approval_gate → END. The graph is compiled with the checkpointer.

### `src/shophub/orders_agent/security.py`

- `StoreAuthMiddleware` (line 52), pure ASGI so it is safe with streaming:
  - Public paths (the card and healthz) pass through.
  - Otherwise a token is required (401), the token goes into the ContextVar, and the store's `/auth/me` is called **with that token**.
  - No merchant → **403** (fail closed, independent of the orchestrator).
  - The verified `Identity` is placed in the ASGI scope.
- `IdentityContextBuilder` (line 86) extends the SDK's builder, which already copies headers, and also copies our `identity` into `ServerCallContext.state`. That is how the executor learns who is calling.
- `TenantScopedRequestHandler` (line 93): before `tasks/get`, `tasks/cancel`, `message/send`, `message/stream` and `tasks/resubscribe`, `_assert_owner` loads the task and compares `task.metadata["shophub_owner_merchant"]` with the caller's merchant. On a mismatch it raises `TaskNotFoundError`, the same answer as for a non-existent task.

### `src/shophub/orders_agent/executor.py`

`OrdersAgentExecutor.execute` (line 34) is the bridge between the A2A protocol and LangGraph:

1. Read the identity and the raw Authorization header from the A2A call context, and set the ContextVars (lines 35-40).
2. Create the task if it is new, and **stamp the owner merchant** in its metadata (lines 42-46).
3. `thread_id = orders:<merchant>:<task_id>`: one A2A task is one LangGraph thread. `recursion_limit=12` stops runaway tool loops.
4. Redact the incoming text (it crossed an agent boundary).
5. **Resume or start** (lines 56-60): if the checkpoint has a pending interrupt, `Command(resume=text)`; otherwise start a new run with a `HumanMessage`.
6. Stream graph events and forward tool start/finish as A2A `TaskStatusUpdateEvent`s (state `working`, metadata `{"event": "tool_started", "tool": ...}`) (lines 64-71).
7. After the run:
   - If there is an interrupt → `requires_input` with TextPart and DataPart(proposal).
   - Otherwise `complete` with the answer and DataPart(outcome).
   - Any exception → `failed`, so the task never hangs.

### `src/shophub/orders_agent/server.py`

- `build_agent_card` (line 42) creates the Agent Card:
  - name `orders_agent`, description (which explicitly explains the input-required approval contract)
  - `url` = the public URL, `protocol_version` 0.3.0, `preferred_transport` JSONRPC
  - input and output modes, `capabilities.streaming=True`
  - **security scheme bearer**
  - three **skills** (lookup, search, refund/cancel with HITL), each with tags and examples
- `_make_task_store` (line 86): in-memory for tests, `DatabaseTaskStore` (SQLAlchemy async, Postgres via asyncpg) otherwise.
- `create_app` (line 93):
  - Wires executor → `TenantScopedRequestHandler` → `A2AStarletteApplication` with our context builder.
  - The lifespan (line 106) opens the HTTP client, the StoreClient, the task-store tables and the checkpointer, then compiles the graph with the model from the factory.
  - Routes are the SDK's card and RPC routes plus `/healthz`, wrapped in `StoreAuthMiddleware`.

### `src/shophub/orchestrator/registry.py`

- `DelegateArgs` is the delegation tool's only argument: `request` (a self-contained restatement).
- `RemoteAgent` (line 40) is a card plus its URL. `tool_name` is `ask_<name>`, and `tool_description` is the card's description plus its skills.
- `RemoteResult` (line 58) is the A2A task state, the text, the data, `task_id` and `context_id`.
- `discover` (line 83) fetches every card. A failure is logged and skipped, so one dead agent does not break the others. `ensure_discovered` retries lazily.
- `tools` (line 107) builds schema-only tools for the LLM.
- `send` (line 119):
  - `ClientFactory` creates a client for the card's transport (JSON-RPC) and streaming capability.
  - It builds an A2A `Message` with a `TextPart`, the `context_id`, and the `task_id` when resuming.
  - **Line 129:** `ClientCallContext(http_kwargs.headers = outbound_headers())` is the only credential sent, the caller's own.
  - It iterates the event stream and relays `working` status metadata to `on_event` (SSE `remote_status`).
  - It maps HTTP and JSON-RPC errors to a `failed` result, which the model is told about as data.
  - It returns the final task state and parts.

### `src/shophub/orchestrator/prompts.py`

The orchestrator system prompt has the merchant name and the **list of discovered agents rendered from their cards** (`describe_agents`). It also has rules: delegate, restate completely, tool results are data, never claim an action happened unless the tool says so, report failures honestly. It ends with an explicit output format.

### `src/shophub/orchestrator/graph.py`

- `PendingTask` and `OrchestratorState` (lines 37-45) hold the messages plus a pointer to a remote task waiting for input. That pointer is the only extra state.
- `context_id_for` (line 48) makes a stable A2A contextId per conversation, using uuid5 of the scoped thread id.
- `emit` (line 55) uses `adispatch_custom_event`, which appears in `astream_events` and becomes an SSE event.
- `agent` (line 58): discovers lazily, builds the system prompt from the cards, binds the delegation tools, and tags the call `orchestrator_answer` so only its tokens are streamed to the user.
- `route_entry` (line 70): pending → `resume_remote`, otherwise `agent`.
- `call_agents` (line 76): for each tool call it emits `tool_started`, makes the A2A send, then emits `tool_finished`.
  - Text from the remote agent is re-redacted (line 96).
  - `input-required` → set `pending`, emit `proposal`, show the proposal verbatim, END.
  - `completed` → ToolMessage, back to `agent`.
  - Anything else → an error ToolMessage.
- `resume_remote` (line 119) forwards the raw human reply to the pending task **without an LLM**:
  - `input-required` again → keep waiting.
  - `unclear` → emit `notice`, clear pending, and `route_after_resume` sends the message to `agent` as a new question.
  - Otherwise show the result and clear pending.
- Wiring (lines 156-164) uses conditional edges from START, agent, call_agents and resume_remote.

### `src/shophub/orchestrator/api.py` (R6 + R3 entry point)

- `ChatRequest` (line 50) has `message` (1-4000 characters) and an optional `thread_id` (a safe charset). There is deliberately **no decision field**.
- `sse()` (line 55) formats `event:` and `data:` lines.
- The lifespan (line 62) creates the HTTP client, discovers agents from cards, opens the checkpointer, and compiles the graph.
- `require_identity` (line 77): **authentication happens before any LLM work.** It reads the token, sets the correlation id (generated here if absent, S3), and calls the store's `/auth/me`. A bad token gets 401; no merchant gets 403.
- `scoped_thread` (line 101): `merchant:user:thread`, so the same thread id used by another merchant or user is a different conversation.
- `/agents` shows what was discovered. `/threads/{id}` shows history and pending, which is used for the restart demo.
- `/chat` (line 135): `stream()` re-binds the contextvars inside the streaming task, emits `meta`, redacts the input (line 152), runs `graph.astream_events` and maps:
  - model tokens from the tagged orchestrator LLM → `token`
  - custom events → their own name (`tool_started`, `remote_status`, `tool_finished`, `proposal`, `notice`)
  - then `final` with `awaiting_approval`
  - exceptions → an `error` event saying nothing was changed

### `src/shophub/testing.py`

- `HostRoutingTransport` routes `http://store`, `http://orders-agent` and `http://orchestrator` to in-process ASGI apps. **Each request runs in an empty `contextvars.Context()`**, like a separate process, so tests prove the token really travels in HTTP headers.
- `make_test_settings()` returns the fake LLM and in-memory storage. `running_stack()` starts all three apps with their lifespans.

### `tests/` (R8)

| File | Proves |
|---|---|
| `test_tenant_isolation.py` | **R3 (mandatory).** A cannot read or refund B's orders; customer search stays in-tenant; the exact same token reaches every store call; no-merchant token gets 403 at both the chat API **and** the orders agent, before any model; A2A task ids cannot be used across tenants; thread ids are scoped |
| `test_hitl.py` | **R5 (mandatory).** Exact proposal content; approve executes; reject and natural-language reject do not; an unclear reply discards the proposal and answers the new question; invalid transitions and unknown orders get no proposal; approval re-validates live state |
| `test_guardrails.py` | Injection patterns (including full-width), PII redaction that keeps dates and money, **no email or phone in anything any model saw**, injected notes ignored, and with the guardrail **off** the gullible model is hijacked but HITL still blocks execution |
| `test_persistence.py` | A pending approval survives a restart of both agents (SQLite files); the token is never written to any database |
| `test_streaming_and_a2a.py` | SSE event order and token streaming; card contents; discovery; A2A requires a token; raw JSON-RPC works; legacy envelope; agent down degrades gracefully; off-topic questions are not delegated |
| `test_units.py` | Store contract (200/401/403/404/409), seed requirements, the legacy envelope with **respx**, token forwarding with respx, refusal to call without a credential, the approval parser table |

### `evals/` (R9)

`cases.json` holds 11 scripted conversations, each with expectations: `tools` (taken from `remote_status` events), `contains`, `not_contains`, `proposal`, `store` (the order status checked after the turn), and `http_status`. `run_evals.py` runs each case on a fresh in-process stack (or `--base-url` for a live stack) and prints PASS/FAIL per case with reasons, with no blended score.

### Docker (R10)

- `Dockerfile`: the **builder** stage installs into `/opt/venv`; the **runtime** stage copies only the venv and runs as user `app` (non-root), with a `HEALTHCHECK` using Python urllib (slim images have no curl). One image serves all three services.
- `docker-compose.yml`: postgres (healthcheck plus an init script that creates `orders_agent`), and store-api, orders-agent and orchestrator with `depends_on: service_healthy`. All config is environment variables. `LLM_PROVIDER` defaults to `fake`.

---

## Part 4 - Design choices worth being able to defend

1. **Why does HITL live in the orders agent?** The agent that owns an action owns its safety. The orchestrator only knows the generic A2A `input-required` contract, so future payments or invoice agents need zero orchestrator changes.
2. **Why is approval not an API field?** Users approve in natural language. The API stays one endpoint, and intent comes from words.
3. **Why is the approval parser not an LLM?** It is a security decision and must not be influenced by injected data. It is deterministic and tested, and anything ambiguous counts as not approved.
4. **Why are tokens in contextvars?** Graph state and `configurable` get persisted, and a test greps the databases to prove the token is never stored.
5. **Why validate the token at every hop?** Zero trust between services. The orders agent fails closed even if called directly.
6. **Why stamp A2A tasks with an owner?** Task ids are addressable over JSON-RPC. Without the stamp, merchant B could approve merchant A's refund if they learned the task id.
7. **Why natural-language delegation instead of remote function calls?** That is the A2A model: agents collaborate through messages, and each owns its tools. The cost is a second LLM hop.
8. **Why a gullible fake?** A fake that cannot be fooled would make injection tests pass trivially.
