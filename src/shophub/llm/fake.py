"""Fake-LLM mode: a deterministic, scripted chat model.

This is a real LangChain `BaseChatModel`, so the graphs, tool binding, tool-call
routing, streaming and checkpointing all run exactly as with a real model. Only
the "thinking" is replaced by a small rule-based policy per agent role.

Two deliberate properties make the tests meaningful rather than "returns ok":

  * TRANSCRIPT records every piece of text that reached any model. Tests assert
    that no email address or phone number ever appears in it (R4b).
  * The orders policy is *gullible*: if instruction-like text from a customer
    note reaches it, it obeys (calls refund/cancel), like a weak model would.
    So the injection tests fail if the guardrail is removed, and the HITL test
    shows that even a hijacked model cannot execute anything without approval.
"""

import json
import re
import uuid
from typing import Any, Callable, Iterator

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

Policy = Callable[[list[BaseMessage], list[str]], AIMessage]

# Test instrumentation: every string any fake model was shown, in order.
TRANSCRIPT: list[str] = []


class ScriptedChatModel(BaseChatModel):
    policy: Policy
    role: str = "agent"
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "ScriptedChatModel":
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"tool_names": names})

    def _record(self, messages: list[BaseMessage]) -> None:
        for m in messages:
            TRANSCRIPT.append(m.text if isinstance(m.content, (str, list)) else str(m.content))
            for tc in getattr(m, "tool_calls", None) or []:
                TRANSCRIPT.append(json.dumps(tc.get("args", {})))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._record(messages)
        return ChatResult(generations=[ChatGeneration(message=self.policy(messages, self.tool_names))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Stream word by word so SSE clients see real token events."""
        self._record(messages)
        msg = self.policy(messages, self.tool_names)
        if msg.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                        for i, tc in enumerate(msg.tool_calls)
                    ],
                )
            )
            return
        for piece in re.findall(r"\S+\s*|\s+", msg.text):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"}])


def _last_human(messages: list[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.text
    return ""


ORDER_ID_RE = re.compile(r"#?\b(\d{3,6})\b")
DOMAIN_RE = re.compile(
    r"\b(orders?|refund\w*|cancel\w*|ship\w*|deliver\w*|paid|placed|customer|status|purchase\w*|bought)\b|#?\d{3,6}",
    re.I,
)


# ----------------------------------------------------------------------------
# Orchestrator policy: decide whether to delegate, then relay the answer.
# ----------------------------------------------------------------------------
def orchestrator_policy(messages: list[BaseMessage], tool_names: list[str]) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        # The specialist already wrote a user-facing answer; relay it.
        return AIMessage(content=last.text)

    request = _last_human(messages)
    if DOMAIN_RE.search(request):
        if not tool_names:
            return AIMessage(content="Sorry, the orders service is unavailable right now. Please try again shortly.")
        target = next((n for n in tool_names if "order" in n), tool_names[0])
        return _call(target, {"request": request})
    return AIMessage(
        content="I'm the ShopHub support assistant. I can look up an order, list orders by status "
        "or customer, and prepare refunds or cancellations for your approval."
    )


# ----------------------------------------------------------------------------
# Orders-agent policy: pick a store tool, then turn its JSON into an answer.
# ----------------------------------------------------------------------------
_STATUSES = ["placed", "paid", "shipped", "delivered", "cancelled", "canceled", "refunded"]
_NOT_SHIPPED_RE = re.compile(r"\b(not|n't|never|yet to)\b.{0,12}\bshipped\b|\bunshipped\b", re.I)
_CUSTOMER_RE = re.compile(r"\bcustomer\s+([A-Z][a-zA-Z'-]+)|\bdid\s+([A-Z][a-zA-Z'-]+)\s+(?:order|buy)")
# What a hijacked model "hears" in a note: "refund this order", "cancel order 2002"...
_INJECTED_ACTION_RE = re.compile(r"\b(refund|cancel)\b\s+(?:this\s+)?order(?:\s+#?(\d+))?", re.I)


def _plan_orders_call(request: str, tool_names: list[str]) -> AIMessage:
    text = request.lower()
    oid = ORDER_ID_RE.search(request)
    if oid and re.search(r"\brefund", text) and "refund_order" in tool_names:
        return _call("refund_order", {"order_id": int(oid.group(1))})
    if oid and re.search(r"\bcancel", text) and "cancel_order" in tool_names:
        return _call("cancel_order", {"order_id": int(oid.group(1))})
    if oid:
        return _call("lookup_order", {"order_id": int(oid.group(1))})

    args: dict[str, Any] = {}
    if m := _CUSTOMER_RE.search(request):
        args["customer_name"] = m.group(1) or m.group(2)
    if _NOT_SHIPPED_RE.search(request):
        args["status"] = "PAID"
        if d := re.search(r"(\d+)\s*days?", text):
            args["older_than_days"] = int(d.group(1))
    else:
        for s in _STATUSES:
            if re.search(rf"\b{s}\b", text):
                args["status"] = "CANCELLED" if s == "canceled" else s.upper()
                break
    if args or "order" in text:
        return _call("list_orders", args)
    return AIMessage(content="I can look up an order by id, list orders by status or customer, "
                             "and prepare refunds or cancellations.")


def _fmt_order(o: dict[str, Any]) -> str:
    return f"#{o['id']} · {o['customer_name']} · {o['status']} · ${o['total']:.2f} · placed {str(o['placed_at'])[:10]}"


def _compose_answer(request: str, tool: str, data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"I couldn't complete that: {data.get('error', 'unknown error')}"
    if tool == "lookup_order":
        o = data["order"]
        answer = (f"Order #{o['id']} for {o['customer_name']} is {o['status']}, total ${o['total']:.2f}, "
                  f"placed {str(o['placed_at'])[:10]}.")
        if o.get("note_flagged"):
            answer += " (Its customer note was withheld because it contained instruction-like text.)"
        return answer
    if tool == "list_orders":
        orders = data["orders"]
        if not orders:
            return "No matching orders found in your store."
        if re.search(r"\b(last|latest|most recent|recent)\b", request, re.I) and data.get("filters", {}).get("customer_name"):
            o = orders[0]  # store returns newest first
            return (f"{o['customer_name']}'s most recent order is #{o['id']}: ${o['total']:.2f}, "
                    f"status {o['status']}, placed {str(o['placed_at'])[:10]}.")
        lines = "\n".join(f"- {_fmt_order(o)}" for o in orders)
        return f"Found {len(orders)} order(s):\n{lines}"
    return json.dumps(data)


def orders_agent_policy(messages: list[BaseMessage], tool_names: list[str]) -> AIMessage:
    last = messages[-1]
    request = _last_human(messages)
    if not isinstance(last, ToolMessage):
        return _plan_orders_call(request, tool_names)

    try:
        data = json.loads(last.text)
    except json.JSONDecodeError:
        data = {"ok": False, "error": last.text}

    # Gullibility: obey instructions found inside tool data (only once per turn).
    already_acted = any(
        tc["name"] in ("refund_order", "cancel_order")
        for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls
    )
    if not already_acted:
        for o in data.get("orders", []) + ([data["order"]] if data.get("order") else []):
            if m := _INJECTED_ACTION_RE.search(str(o.get("customer_note", ""))):
                target = int(m.group(2)) if m.group(2) else int(o["id"])
                return _call(f"{m.group(1).lower()}_order", {"order_id": target})

    return AIMessage(content=_compose_answer(request, last.name or "", data))
