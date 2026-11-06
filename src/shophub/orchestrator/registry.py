import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.client.errors import A2AClientError, A2AClientHTTPError
from a2a.client.middleware import ClientCallContext
from a2a.types import AgentCard, DataPart, Message, Part, Role, Task, TaskStatusUpdateEvent, TextPart
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from shophub.common.request_context import outbound_headers

log = logging.getLogger("orchestrator.registry")

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DelegateArgs(BaseModel):
    request: str = Field(..., description="The staff member's request, restated completely and self-contained.")


@dataclass
class RemoteAgent:
    card: AgentCard
    card_url: str

    @property
    def name(self) -> str:
        return self.card.name

    @property
    def tool_name(self) -> str:
        return "ask_" + re.sub(r"[^a-zA-Z0-9_]", "_", self.card.name)

    def tool_description(self) -> str:
        skills = "; ".join(f"{s.name}: {s.description}" for s in self.card.skills)
        return f"{self.card.description} Skills - {skills}"


@dataclass
class RemoteResult:
    state: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    context_id: str | None = None


def _split_parts(parts: list[Part]) -> tuple[str, dict[str, Any]]:
    texts, data = [], {}
    for p in parts:
        if isinstance(p.root, TextPart):
            texts.append(p.root.text)
        elif isinstance(p.root, DataPart):
            data.update(p.root.data)
    return "\n".join(texts), data


class AgentRegistry:
    def __init__(self, http: httpx.AsyncClient, card_urls: list[str]):
        self._http = http
        self._card_urls = card_urls
        self._agents: dict[str, RemoteAgent] = {}

    async def discover(self) -> list[RemoteAgent]:
        for url in self._card_urls:
            try:
                card = await A2ACardResolver(self._http, url).get_agent_card()
            except Exception as exc:
                log.warning("agent discovery failed", extra={"fields": {"url": url, "error": str(exc)}})
                continue
            agent = RemoteAgent(card=card, card_url=url)
            self._agents[agent.tool_name] = agent
            log.info("discovered agent", extra={"fields": {"agent": card.name, "url": card.url,
                                                           "skills": [s.id for s in card.skills]}})
        return list(self._agents.values())

    async def ensure_discovered(self) -> None:
        if not self._agents:
            await self.discover()

    @property
    def agents(self) -> list[RemoteAgent]:
        return list(self._agents.values())

    def get(self, tool_name: str) -> RemoteAgent | None:
        return self._agents.get(tool_name)

    def tools(self) -> list[StructuredTool]:
        async def _schema_only(request: str) -> str:
            raise RuntimeError("executed by the orchestrator graph")

        return [
            StructuredTool.from_function(coroutine=_schema_only, name=a.tool_name,
                                         description=a.tool_description(), args_schema=DelegateArgs)
            for a in self.agents
        ]

    async def send(self, agent: RemoteAgent, text: str, *, context_id: str, task_id: str | None = None,
                   on_event: EventCallback | None = None) -> RemoteResult:
        factory = ClientFactory(ClientConfig(httpx_client=self._http, streaming=bool(agent.card.capabilities.streaming)))
        client = factory.create(agent.card)
        message = Message(
            role=Role.user, message_id=uuid.uuid4().hex, context_id=context_id, task_id=task_id,
            parts=[Part(root=TextPart(text=text))],
        )
        call_ctx = ClientCallContext(state={"http_kwargs": {"headers": outbound_headers()}})

        last_task: Task | None = None
        try:
            async for event in client.send_message(message, context=call_ctx):
                if isinstance(event, Message):
                    body, data = _split_parts(event.parts)
                    return RemoteResult("completed", body, data, None, event.context_id)
                task, update = event
                last_task = task
                if on_event and isinstance(update, TaskStatusUpdateEvent) and update.status.message:
                    meta = update.status.message.metadata or {}
                    if meta.get("event"):
                        await on_event(meta)
        except A2AClientHTTPError as exc:
            log.warning("A2A HTTP error", extra={"fields": {"agent": agent.name, "status": exc.status_code}})
            return RemoteResult("failed", f"The {agent.name} refused the request (HTTP {exc.status_code}).")
        except A2AClientError as exc:
            log.warning("A2A error", extra={"fields": {"agent": agent.name, "error": str(exc)}})
            return RemoteResult("failed", f"The {agent.name} could not handle the request: {exc}")
        except httpx.HTTPError as exc:
            return RemoteResult("failed", f"The {agent.name} is unreachable ({type(exc).__name__}).")

        if last_task is None:
            return RemoteResult("failed", f"The {agent.name} returned no result.")
        body, data = _split_parts(last_task.status.message.parts) if last_task.status.message else ("", {})
        return RemoteResult(last_task.status.state.value, body, data, last_task.id, last_task.context_id)
