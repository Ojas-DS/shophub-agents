import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Part, TaskState, TextPart
from a2a.utils import new_agent_parts_message, new_agent_text_message, new_task
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from shophub.common.guardrails import redact_pii
from shophub.common.request_context import Identity, bearer_token, current_identity
from shophub.orders_agent.graph import proposal_text
from shophub.orders_agent.security import OWNER_KEY

log = logging.getLogger("orders_agent.executor")


class OrdersAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.graph = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        identity: Identity | None = context.call_context.state.get("identity") if context.call_context else None
        if identity is None:
            raise PermissionError("unauthenticated A2A call reached the executor")
        headers = context.call_context.state.get("headers", {})
        bearer_token.set(headers.get("authorization"))
        current_identity.set(identity)

        task = context.current_task
        if task is None:
            task = new_task(context.message)
            task.metadata = {OWNER_KEY: identity.merchant_id}
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        config = {
            "configurable": {"thread_id": f"orders:{identity.merchant_id}:{task.id}"},
            "recursion_limit": 12,
        }
        user_text = redact_pii(context.get_user_input())

        snapshot = await self.graph.aget_state(config)
        if snapshot.interrupts:
            graph_input = Command(resume=user_text)
        else:
            graph_input = {"messages": [HumanMessage(user_text)], "outcome": None}

        await updater.start_work()
        try:
            async for ev in self.graph.astream_events(graph_input, config, version="v2"):
                kind = ev["event"]
                if kind in ("on_tool_start", "on_tool_end"):
                    name = ev.get("name")
                    status = "tool_started" if kind == "on_tool_start" else "tool_finished"
                    msg = new_agent_text_message(f"{status}: {name}", task.context_id, task.id)
                    msg.metadata = {"event": status, "tool": name, "agent": "orders_agent"}
                    await updater.update_status(TaskState.working, message=msg)

            snapshot = await self.graph.aget_state(config)
            if snapshot.interrupts:
                proposal = snapshot.interrupts[0].value
                msg = new_agent_parts_message(
                    [Part(root=TextPart(text=proposal_text(proposal))), Part(root=DataPart(data={"proposal": proposal}))],
                    task.context_id, task.id,
                )
                await updater.requires_input(msg, final=True)
                return

            last = next((m for m in reversed(snapshot.values["messages"]) if isinstance(m, AIMessage)), None)
            answer = last.text if last else "Done."
            outcome = snapshot.values.get("outcome")
            parts = [Part(root=TextPart(text=answer))]
            if outcome:
                parts.append(Part(root=DataPart(data={"outcome": outcome})))
            await updater.complete(new_agent_parts_message(parts, task.context_id, task.id))
        except Exception as exc:
            log.exception("orders agent run failed")
            await updater.failed(new_agent_text_message(
                f"The orders agent hit an internal error ({type(exc).__name__}).", task.context_id, task.id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()
