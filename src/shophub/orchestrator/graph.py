"""The orchestrator's LangGraph (R1).

  START ─┬─(a remote task is waiting for this user's reply)──► resume_remote ─┬─► END
         │                                                                     └─(reply was not a
         │                                                                        decision)─► agent
         └─────────────────────────────────────────────────────────────────────────────► agent
  agent ──(tool call ask_<agent>)──► call_agents ──(answer)──► agent ──► END
                                          └──(remote task input-required)──► END  (proposal shown)

State is kept minimal: the message history plus `pending`, a pointer to a remote
A2A task in `input-required` state. The orchestrator never interprets a proposal
or a decision - it only knows the generic A2A rule "if a remote task is waiting
for input, the user's next message goes to that task". That keeps human-in-the-loop
inside the agent that owns the action, so any future agent works unchanged.
"""

import json
import uuid
from typing import Any, Literal, TypedDict

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from shophub.common.guardrails import redact_pii
from shophub.common.request_context import current_identity
from shophub.orchestrator.prompts import ORCHESTRATOR_SYSTEM_PROMPT, describe_agents
from shophub.orchestrator.registry import AgentRegistry


ANSWER_TAG = "orchestrator_answer"


class PendingTask(TypedDict):
    agent_tool: str
    task_id: str
    context_id: str
    proposal: dict[str, Any]


class OrchestratorState(MessagesState):
    pending: PendingTask | None


def context_id_for(config: RunnableConfig) -> str:
    """Stable A2A contextId per conversation, derived from the (tenant-scoped) thread id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, config["configurable"]["thread_id"]))


def build_orchestrator_graph(model: BaseChatModel, registry: AgentRegistry, checkpointer: BaseCheckpointSaver):

    async def emit(name: str, data: dict[str, Any]) -> None:
        await adispatch_custom_event(name, data)  # surfaces in astream_events -> SSE

    async def agent(state: OrchestratorState) -> dict:
        await registry.ensure_discovered()  # agents may have started after us
        identity = current_identity.get()
        system = SystemMessage(ORCHESTRATOR_SYSTEM_PROMPT.format(
            merchant_name=identity.merchant_name if identity else "your store",
            agents=describe_agents(registry.agents),
        ))
        llm = model.bind_tools(registry.tools()) if registry.agents else model
        # The tag lets the chat API stream only THIS model's tokens to the user.
        response = await llm.ainvoke([system, *state["messages"]], config={"tags": [ANSWER_TAG]})
        return {"messages": [response]}

    def route_entry(state: OrchestratorState) -> Literal["resume_remote", "agent"]:
        return "resume_remote" if state.get("pending") else "agent"

    def route_agent(state: OrchestratorState) -> Literal["call_agents", "__end__"]:
        return "call_agents" if getattr(state["messages"][-1], "tool_calls", None) else END

    async def call_agents(state: OrchestratorState, config: RunnableConfig) -> dict:
        out: list = []
        pending: PendingTask | None = None
        proposal_text = ""
        for call in state["messages"][-1].tool_calls:
            remote = registry.get(call["name"])
            if remote is None or pending is not None:
                reason = "unknown agent" if remote is None else "another action is awaiting approval"
                out.append(ToolMessage(json.dumps({"ok": False, "error": reason}), tool_call_id=call["id"], name=call["name"]))
                continue

            await emit("tool_started", {"tool": call["name"], "agent": remote.name, "args": call["args"]})

            async def relay(meta: dict[str, Any]) -> None:
                await emit("remote_status", meta)

            result = await registry.send(remote, str(call["args"].get("request", "")),
                                         context_id=context_id_for(config), on_event=relay)
            await emit("tool_finished", {"tool": call["name"], "agent": remote.name, "state": result.state})

            text = redact_pii(result.text)  # untrusted: re-redact anything crossing the boundary
            if result.state == "input-required" and result.task_id:
                pending = PendingTask(agent_tool=call["name"], task_id=result.task_id,
                                      context_id=result.context_id or context_id_for(config),
                                      proposal=result.data.get("proposal", {}))
                proposal_text = text
                await emit("proposal", {"agent": remote.name, "task_id": result.task_id,
                                        "proposal": pending["proposal"], "text": text})
                out.append(ToolMessage(text, tool_call_id=call["id"], name=call["name"]))
            elif result.state == "completed":
                out.append(ToolMessage(text, tool_call_id=call["id"], name=call["name"]))
            else:
                out.append(ToolMessage(json.dumps({"ok": False, "error": text or result.state}),
                                       tool_call_id=call["id"], name=call["name"]))

        if pending:
            # Show the proposal verbatim (no LLM rewording of amounts/ids) and stop the turn.
            out.append(AIMessage(proposal_text))
        return {"messages": out, "pending": pending}

    def route_after_call(state: OrchestratorState) -> Literal["agent", "__end__"]:
        return END if state.get("pending") else "agent"

    async def resume_remote(state: OrchestratorState) -> dict:
        """Forward the user's raw reply to the waiting remote task. No LLM involved."""
        pending = state["pending"]
        remote = registry.get(pending["agent_tool"])
        reply = next(m.text for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        if remote is None:
            await registry.discover()
            remote = registry.get(pending["agent_tool"])
        if remote is None:
            return {"messages": [AIMessage("The agent handling your pending request is unavailable; "
                                           "nothing was changed. Please try again.")], "pending": None}

        await emit("tool_started", {"tool": pending["agent_tool"], "agent": remote.name, "resume_task": pending["task_id"]})

        async def relay(meta: dict[str, Any]) -> None:
            await emit("remote_status", meta)

        result = await registry.send(remote, reply, context_id=pending["context_id"],
                                     task_id=pending["task_id"], on_event=relay)
        await emit("tool_finished", {"tool": pending["agent_tool"], "agent": remote.name, "state": result.state})

        if result.state == "input-required":  # agent asked again; keep waiting
            return {"messages": [AIMessage(redact_pii(result.text))], "pending": pending}
        decision = (result.data.get("outcome") or {}).get("decision")
        if decision == "unclear":
            # The reply was not an approve/reject: the proposal was discarded by the
            # owning agent, and the message is treated as a new request.
            await emit("notice", {"text": redact_pii(result.text)})
            return {"pending": None, "messages": []}
        text = result.text if result.state == "completed" else f"Nothing was changed: {result.text}"
        return {"messages": [AIMessage(redact_pii(text))], "pending": None}

    def route_after_resume(state: OrchestratorState) -> Literal["agent", "__end__"]:
        last = state["messages"][-1]
        # If resume produced no new AI message (unclear decision), answer the new request.
        return "agent" if isinstance(last, HumanMessage) else END

    g = StateGraph(OrchestratorState)
    g.add_node("agent", agent)
    g.add_node("call_agents", call_agents)
    g.add_node("resume_remote", resume_remote)
    g.add_conditional_edges(START, route_entry, ["resume_remote", "agent"])
    g.add_conditional_edges("agent", route_agent, ["call_agents", END])
    g.add_conditional_edges("call_agents", route_after_call, ["agent", END])
    g.add_conditional_edges("resume_remote", route_after_resume, ["agent", END])
    return g.compile(checkpointer=checkpointer)
