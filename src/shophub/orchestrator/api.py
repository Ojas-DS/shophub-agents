import contextlib
import json
import logging
import uuid
from typing import Annotated, Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from shophub.common.checkpoint import make_checkpointer
from shophub.common.guardrails import redact_pii
from shophub.common.request_context import (
    CORRELATION_HEADER,
    Identity,
    bearer_token,
    correlation_id,
    current_identity,
)
from shophub.config import Settings
from shophub.llm.factory import make_chat_model
from shophub.orchestrator.graph import ANSWER_TAG, build_orchestrator_graph
from shophub.orchestrator.registry import AgentRegistry

log = logging.getLogger("orchestrator.api")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    thread_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def create_app(settings: Settings, http: httpx.AsyncClient | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            client = http or await stack.enter_async_context(httpx.AsyncClient(timeout=settings.http_timeout_s))
            registry = AgentRegistry(client, settings.agent_card_url_list)
            await registry.discover()
            checkpointer = await stack.enter_async_context(make_checkpointer(settings.orchestrator_db_url))
            app.state.http = client
            app.state.registry = registry
            app.state.graph = build_orchestrator_graph(
                make_chat_model("orchestrator", settings), registry, checkpointer)
            yield

    app = FastAPI(title="ShopHub Support Chat (Orchestrator)", version="1.0.0", lifespan=lifespan)

    async def require_identity(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Identity:
        if not authorization:
            raise HTTPException(401, "Missing bearer token", {"WWW-Authenticate": "Bearer"})
        bearer_token.set(authorization)
        correlation_id.set(request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex)
        try:
            resp = await request.app.state.http.get(
                f"{settings.store_api_url.rstrip('/')}/auth/me",
                headers={"Authorization": authorization, CORRELATION_HEADER: correlation_id.get()},
            )
        except httpx.HTTPError:
            raise HTTPException(503, "Auth service unavailable")
        if resp.status_code == 401:
            raise HTTPException(401, "Invalid bearer token", {"WWW-Authenticate": "Bearer"})
        if resp.status_code != 200:
            raise HTTPException(503, "Auth service error")
        me = resp.json()
        if not me.get("merchant"):
            log.warning("rejected token without merchant", extra={"fields": {"user": me.get("user")}})
            raise HTTPException(403, "This account is not linked to a merchant store")
        return Identity(user=me["user"], merchant_id=me["merchant"]["id"], merchant_name=me["merchant"]["name"])

    def scoped_thread(identity: Identity, thread_id: str) -> str:
        return f"{identity.merchant_id}:{identity.user}:{thread_id}"

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        return {"ok": True, "agents": [a.name for a in request.app.state.registry.agents]}

    @app.get("/agents")
    async def agents(request: Request, identity: Annotated[Identity, Depends(require_identity)]) -> list[dict]:
        await request.app.state.registry.ensure_discovered()
        return [
            {"name": a.name, "tool": a.tool_name, "url": a.card.url, "card_url": a.card_url,
             "skills": [{"id": s.id, "name": s.name} for s in a.card.skills]}
            for a in request.app.state.registry.agents
        ]

    @app.get("/threads/{thread_id}")
    async def thread_history(request: Request, thread_id: str,
                             identity: Annotated[Identity, Depends(require_identity)]) -> dict:
        config = {"configurable": {"thread_id": scoped_thread(identity, thread_id)}}
        snap = await request.app.state.graph.aget_state(config)
        msgs = snap.values.get("messages", []) if snap.values else []
        return {
            "thread_id": thread_id,
            "awaiting_approval": bool(snap.values.get("pending")) if snap.values else False,
            "messages": [{"role": m.type, "text": m.text} for m in msgs
                         if isinstance(m, (HumanMessage, AIMessage)) and m.text],
        }

    @app.post("/chat")
    async def chat(body: ChatRequest, request: Request,
                   identity: Annotated[Identity, Depends(require_identity)]) -> StreamingResponse:
        thread_id = body.thread_id or uuid.uuid4().hex[:12]
        graph = request.app.state.graph
        token, cid = bearer_token.get(), correlation_id.get()

        async def stream() -> AsyncIterator[str]:
            bearer_token.set(token)
            correlation_id.set(cid)
            current_identity.set(identity)
            config = {"configurable": {"thread_id": scoped_thread(identity, thread_id)}, "recursion_limit": 12}
            yield sse("meta", {"thread_id": thread_id, "correlation_id": cid})
            log.info("chat turn", extra={"fields": {"merchant": identity.merchant_id, "thread": thread_id}})
            try:
                inputs = {"messages": [HumanMessage(redact_pii(body.message))]}
                async for ev in graph.astream_events(inputs, config, version="v2"):
                    kind = ev["event"]
                    if kind == "on_chat_model_stream" and ANSWER_TAG in ev.get("tags", []):
                        piece = ev["data"]["chunk"].text
                        if piece:
                            yield sse("token", {"text": piece})
                    elif kind == "on_custom_event":
                        yield sse(ev["name"], ev["data"])
                snap = await graph.aget_state(config)
                last = next((m for m in reversed(snap.values["messages"]) if isinstance(m, AIMessage) and m.text), None)
                yield sse("final", {"text": last.text if last else "",
                                    "awaiting_approval": bool(snap.values.get("pending"))})
            except Exception as exc:
                log.exception("chat turn failed")
                yield sse("error", {"message": f"Internal error ({type(exc).__name__}). Nothing was changed."})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", CORRELATION_HEADER: cid})

    return app
