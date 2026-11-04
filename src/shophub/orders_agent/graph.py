"""The Orders Agent's LangGraph: a tool-calling loop with a human approval gate.

            ┌──────────── read tool results ────────────┐
            ▼                                            │
  START ─► agent ──(list_orders / lookup_order)──► read_tools
            │
            ├──(refund_order / cancel_order)──► approval_gate ──► END
            │                                     │  interrupt(): pause until a
            └──(plain answer)──► END              │  human replies approve/reject
                                                  ▼
                                          execute or discard

Human-in-the-loop (R5) lives HERE, inside the agent that owns the action, not in
the chat API. Any future agent (payments, invoices, addresses) brings its own gate
and the orchestrator needs no changes - it only sees the A2A `input-required` state.
"""

import json
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from shophub.common.request_context import current_identity
from shophub.orders_agent.decisions import parse_decision
from shophub.orders_agent.prompts import ORDERS_SYSTEM_PROMPT
from shophub.orders_agent.store_client import StoreClient, StoreError
from shophub.orders_agent.tools import ACTION_TOOLS, READ_TOOLS, build_tools
from shophub.store_api.models import CANCELLABLE, REFUNDABLE, OrderStatus

ACTIONS: dict[str, dict[str, Any]] = {
    "refund_order": {"verb": "refund", "allowed": REFUNDABLE, "new_status": OrderStatus.REFUNDED},
    "cancel_order": {"verb": "cancel", "allowed": CANCELLABLE, "new_status": OrderStatus.CANCELLED},
}


class OrdersState(MessagesState):
    # Minimal extra state: the outcome of an approval, reported back over A2A.
    outcome: dict[str, Any] | None


def proposal_text(p: dict[str, Any]) -> str:
    return (
        f"Approval needed: {p['action'].upper()} order #{p['order_id']} for {p['merchant_name']} "
        f"(customer {p['customer_name']}).\n"
        f"Change: status {p['current_status']} -> {p['new_status']}. Amount: ${p['amount']:.2f} {p['currency']}.\n"
        "Reply 'approve' to execute it or 'reject' to discard it. Nothing has changed yet."
    )


def build_orders_graph(model: BaseChatModel, store: StoreClient, checkpointer: BaseCheckpointSaver,
                       guardrails_enabled: bool = True):
    tools = build_tools(store, guardrails_enabled)
    read_tools = [t for t in tools if t.name in READ_TOOLS]
    llm = model.bind_tools(tools)

    async def agent(state: OrdersState) -> dict:
        identity = current_identity.get()
        system = SystemMessage(ORDERS_SYSTEM_PROMPT.format(merchant_name=identity.merchant_name if identity else "your store"))
        response = await llm.ainvoke([system, *state["messages"]])
        return {"messages": [response]}

    def route(state: OrdersState) -> Literal["read_tools", "approval_gate", "__end__"]:
        last = state["messages"][-1]
        calls = getattr(last, "tool_calls", None) or []
        if not calls:
            return END
        if any(c["name"] in ACTION_TOOLS for c in calls):
            return "approval_gate"
        return "read_tools"

    async def build_proposal(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Validate against LIVE store data (caller's token) and describe the change."""
        spec = ACTIONS[name]
        order = await store.lookup_order(int(args["order_id"]))  # raises StoreError (legacy envelope handled)
        status = OrderStatus(order["status"])
        if status not in spec["allowed"]:
            allowed = ", ".join(sorted(s.value for s in spec["allowed"]))
            raise StoreError(
                f"Order #{order['id']} is {status.value}; it can only be {spec['verb']}ed from {allowed}.", "conflict"
            )
        identity = current_identity.get()
        return {
            "type": "action_proposal",
            "action": spec["verb"],
            "order_id": order["id"],
            "merchant_id": identity.merchant_id,
            "merchant_name": identity.merchant_name,
            "customer_name": order["customer_name"],
            "current_status": status.value,
            "new_status": spec["new_status"].value,
            "amount": float(order["total"]),
            "currency": "USD",
            "reason": args.get("reason"),
        }

    async def approval_gate(state: OrdersState) -> dict:
        calls = state["messages"][-1].tool_calls
        call = next(c for c in calls if c["name"] in ACTION_TOOLS)
        # Only one action per approval; every other call gets an explicit answer.
        skipped = [
            ToolMessage(json.dumps({"ok": False, "error": "Only one action can be proposed at a time."}),
                        tool_call_id=c["id"], name=c["name"])
            for c in calls if c["id"] != call["id"]
        ]

        # NOTE: on resume LangGraph re-runs this node from the top, so the proposal
        # is rebuilt from live data with the approver's token. If the order changed
        # meanwhile (e.g. already refunded) we refuse instead of executing stale intent.
        try:
            proposal = await build_proposal(call["name"], call["args"])
        except StoreError as exc:
            msg = f"I can't {ACTIONS[call['name']]['verb']} order #{call['args'].get('order_id')}: {exc.message}"
            return {
                "messages": [ToolMessage(json.dumps({"ok": False, "error": exc.message}),
                                         tool_call_id=call["id"], name=call["name"]), *skipped, AIMessage(msg)],
                "outcome": {"decision": "invalid", "error": exc.message},
            }

        # ---- PAUSE: first pass returns the proposal to the caller (A2A input-required).
        # ---- On resume, interrupt() returns the human's raw reply text.
        reply = interrupt(proposal)
        decision = parse_decision(str(reply))

        if decision != "approve":
            verb = "rejected" if decision == "reject" else "not approved (no explicit approval)"
            text = f"Proposal {verb}. Order #{proposal['order_id']} was not changed."
            result = {"ok": False, "error": f"Proposal {verb}; nothing executed."}
            return {
                "messages": [ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"]),
                             *skipped, AIMessage(text)],
                "outcome": {"decision": "rejected" if decision == "reject" else "unclear", "proposal": proposal},
            }

        try:
            if call["name"] == "refund_order":
                updated = await store.refund_order(proposal["order_id"])
            else:
                updated = await store.cancel_order(proposal["order_id"])
            result = {"ok": True, "order_id": updated["id"], "status": updated["status"]}
            text = (f"Done: order #{updated['id']} is now {updated['status']} "
                    f"(${proposal['amount']:.2f} {proposal['currency']}).")
            outcome = {"decision": "approved", "executed": True, "proposal": proposal}
        except StoreError as exc:
            result = {"ok": False, "error": exc.message}
            text = f"Approved, but the store refused it: {exc.message} Nothing was changed."
            outcome = {"decision": "approved", "executed": False, "error": exc.message, "proposal": proposal}

        return {
            "messages": [ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"]),
                         *skipped, AIMessage(text)],
            "outcome": outcome,
        }

    g = StateGraph(OrdersState)
    g.add_node("agent", agent)
    g.add_node("read_tools", ToolNode(read_tools, handle_tool_errors=True))
    g.add_node("approval_gate", approval_gate)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["read_tools", "approval_gate", END])
    g.add_edge("read_tools", "agent")
    g.add_edge("approval_gate", END)
    return g.compile(checkpointer=checkpointer)
