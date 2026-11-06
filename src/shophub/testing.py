import asyncio
import contextlib
import contextvars
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from asgi_lifespan import LifespanManager

from shophub.config import Settings

STORE_URL = "http://store"
ORDERS_URL = "http://orders-agent"
ORCH_URL = "http://orchestrator"


class HostRoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps: dict[str, object]):
        self._transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    def mount(self, host: str, app: object) -> None:
        self._transports[host] = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no in-process app for host {request.url.host!r}", request=request)
        return await asyncio.create_task(transport.handle_async_request(request), context=contextvars.Context())


@dataclass
class Stack:
    settings: Settings
    http: httpx.AsyncClient
    client: httpx.AsyncClient
    store_app: object


def make_test_settings(**overrides) -> Settings:
    base = dict(
        llm_provider="fake",
        store_api_url=STORE_URL,
        orders_agent_public_url=f"{ORDERS_URL}/",
        agent_card_urls=ORDERS_URL,
        orchestrator_db_url="memory",
        orders_agent_db_url="memory",
        a2a_task_store_url="memory",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@contextlib.asynccontextmanager
async def running_stack(settings: Settings | None = None, store_app=None) -> AsyncIterator[Stack]:
    from shophub.orchestrator.api import create_app as create_orchestrator
    from shophub.orders_agent.server import create_app as create_orders_agent
    from shophub.store_api.app import create_app as create_store

    settings = settings or make_test_settings()
    store_app = store_app or create_store()
    transport = HostRoutingTransport({"store": store_app})
    async with httpx.AsyncClient(transport=transport, timeout=30) as http:
        orders_app = create_orders_agent(settings, http=http)
        orch_app = create_orchestrator(settings, http=http)
        async with LifespanManager(orders_app):
            transport.mount("orders-agent", orders_app)
            async with LifespanManager(orch_app):
                transport.mount("orchestrator", orch_app)
                async with httpx.AsyncClient(transport=transport, base_url=ORCH_URL, timeout=30) as client:
                    yield Stack(settings=settings, http=http, client=client, store_app=store_app)
