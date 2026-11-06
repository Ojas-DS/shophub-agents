import logging
from typing import Any, AsyncGenerator

from a2a.server.apps.jsonrpc.jsonrpc_app import DefaultCallContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import TaskNotFoundError
from a2a.utils.errors import ServerError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from shophub.common.request_context import (
    CORRELATION_HEADER,
    Identity,
    bearer_token,
    correlation_id,
)
from shophub.orders_agent.store_client import StoreClient, StoreError

log = logging.getLogger("orders_agent.security")

OWNER_KEY = "shophub_owner_merchant"
IDENTITY_SCOPE_KEY = "shophub.identity"
PUBLIC_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json", "/healthz")


def _rpc_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}},
        status_code=status,
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


class StoreAuthMiddleware:
    def __init__(self, app, get_store):
        self.app = app
        self.get_store = get_store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        token = headers.get("authorization")
        correlation_id.set(headers.get(CORRELATION_HEADER))
        if not token:
            return await _rpc_error(401, "Missing bearer token")(scope, receive, send)

        bearer_token.set(token)
        store: StoreClient = self.get_store()
        try:
            me = await store.whoami()
        except StoreError as exc:
            status = 401 if exc.code == "unauthorized" else 502
            return await _rpc_error(status, exc.message)(scope, receive, send)
        if not me.get("merchant"):
            log.warning("rejected token without merchant", extra={"fields": {"user": me.get("user")}})
            return await _rpc_error(403, "Token is not scoped to a merchant")(scope, receive, send)

        scope[IDENTITY_SCOPE_KEY] = Identity(
            user=me["user"], merchant_id=me["merchant"]["id"], merchant_name=me["merchant"]["name"]
        )
        await self.app(scope, receive, send)


class IdentityContextBuilder(DefaultCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        ctx = super().build(request)
        ctx.state["identity"] = request.scope.get(IDENTITY_SCOPE_KEY)
        return ctx


class TenantScopedRequestHandler(DefaultRequestHandler):
    async def _assert_owner(self, task_id: str | None, context: ServerCallContext | None) -> None:
        if not task_id:
            return
        identity: Identity | None = context.state.get("identity") if context else None
        task = await self.task_store.get(task_id, context)
        if task is None:
            return
        owner = (task.metadata or {}).get(OWNER_KEY)
        if identity is None or owner != identity.merchant_id:
            log.warning("cross-tenant task access blocked", extra={"fields": {"task_id": task_id}})
            raise ServerError(error=TaskNotFoundError())

    async def on_get_task(self, params, context=None):
        await self._assert_owner(params.id, context)
        return await super().on_get_task(params, context)

    async def on_cancel_task(self, params, context=None):
        await self._assert_owner(params.id, context)
        return await super().on_cancel_task(params, context)

    async def on_message_send(self, params, context=None):
        await self._assert_owner(params.message.task_id, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params, context=None) -> AsyncGenerator[Any, None]:
        await self._assert_owner(params.message.task_id, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_resubscribe_to_task(self, params, context=None) -> AsyncGenerator[Any, None]:
        await self._assert_owner(params.id, context)
        async for event in super().on_resubscribe_to_task(params, context):
            yield event
