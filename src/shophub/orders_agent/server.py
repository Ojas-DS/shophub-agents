"""Orders Agent as an A2A server (R2): Agent Card + JSON-RPC 2.0 endpoint.

Routes (served by the a2a-sdk):
  GET  /.well-known/agent-card.json   public Agent Card (discovery)
  POST /                              JSON-RPC 2.0: message/send, message/stream,
                                      tasks/get, tasks/cancel, tasks/resubscribe
  GET  /healthz                       liveness for docker
"""

import contextlib
from typing import AsyncIterator

import httpx
from a2a.server.apps import A2AStarletteApplication
from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from shophub.common.checkpoint import make_checkpointer
from shophub.config import Settings
from shophub.llm.factory import make_chat_model
from shophub.orders_agent.executor import OrdersAgentExecutor
from shophub.orders_agent.graph import build_orders_graph
from shophub.orders_agent.security import (
    IdentityContextBuilder,
    StoreAuthMiddleware,
    TenantScopedRequestHandler,
)
from shophub.orders_agent.store_client import StoreClient


def build_agent_card(public_url: str) -> AgentCard:
    """The machine-readable description other agents discover us by."""
    return AgentCard(
        name="orders_agent",
        description=(
            "Answers questions about the calling merchant's orders and prepares refunds or "
            "cancellations. Refunds/cancellations are never executed directly: the task enters "
            "the A2A 'input-required' state with a proposal, and only an explicit human "
            "'approve' sent to the same task executes it."
        ),
        url=public_url,
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        security_schemes={
            "bearer": SecurityScheme(root=HTTPAuthSecurityScheme(
                scheme="bearer", description="The merchant staff token, forwarded unchanged."))
        },
        security=[{"bearer": []}],
        skills=[
            AgentSkill(
                id="order_lookup", name="Order lookup",
                description="Find one order by id: status, total, customer name, date.",
                tags=["orders", "lookup"], examples=["What is the status of order 1042?"],
            ),
            AgentSkill(
                id="order_search", name="Order search",
                description="List orders filtered by status, customer name, or age in days.",
                tags=["orders", "search", "customers"],
                examples=["Which paid orders have not shipped in 3 days?", "What did customer Maria order last?"],
            ),
            AgentSkill(
                id="order_actions", name="Refund / cancel (human approval)",
                description="Propose a refund or cancellation; executes only after explicit human approval.",
                tags=["orders", "refund", "cancel", "human-in-the-loop"],
                examples=["Refund order 1042", "Cancel order 1005"],
            ),
        ],
    )


def _make_task_store(url: str) -> TaskStore:
    if url == "memory":
        return InMemoryTaskStore()
    # A2A tasks persist too, so a pending approval survives a restart (R7).
    return DatabaseTaskStore(create_async_engine(url), create_table=True)


def create_app(settings: Settings, http: httpx.AsyncClient | None = None) -> Starlette:
    """Build the ASGI app. `http` lets tests inject an in-process transport."""
    executor = OrdersAgentExecutor()
    task_store = _make_task_store(settings.a2a_task_store_url)
    handler = TenantScopedRequestHandler(agent_executor=executor, task_store=task_store)
    a2a_app = A2AStarletteApplication(
        agent_card=build_agent_card(settings.orders_agent_public_url),
        http_handler=handler,
        context_builder=IdentityContextBuilder(),
    )
    holder: dict[str, StoreClient] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            client = http or await stack.enter_async_context(httpx.AsyncClient(timeout=settings.http_timeout_s))
            store = StoreClient(settings.store_api_url, client)
            holder["store"] = store
            if isinstance(task_store, DatabaseTaskStore):
                await task_store.initialize()
            checkpointer = await stack.enter_async_context(make_checkpointer(settings.orders_agent_db_url))
            executor.graph = build_orders_graph(
                make_chat_model("orders_agent", settings), store, checkpointer, settings.guardrails_enabled
            )
            yield

    async def healthz(_request):
        return JSONResponse({"ok": executor.graph is not None})

    return Starlette(
        routes=[*a2a_app.routes(), Route("/healthz", healthz)],
        middleware=[Middleware(StoreAuthMiddleware, get_store=lambda: holder["store"])],
        lifespan=lifespan,
    )
