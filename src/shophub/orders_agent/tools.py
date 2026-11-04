"""Tool schemas the orders agent's model can call.

Read tools (list_orders, lookup_order) execute immediately and return JSON.
Action tools (refund_order, cancel_order) are schema-only: the graph routes
those calls to the approval gate instead of running them (see graph.py). The
model believes it is "refunding"; in reality it can only ever *propose*.

Every tool returns {"ok": true, ...} or {"ok": false, "error": "..."} so errors
reach the model as data it can explain, rather than as exceptions (task §5).
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from shophub.common.guardrails import safe_order
from shophub.orders_agent.store_client import StoreClient, StoreError
from shophub.store_api.models import OrderStatus

READ_TOOLS = ("list_orders", "lookup_order")
ACTION_TOOLS = ("refund_order", "cancel_order")


class ListOrdersArgs(BaseModel):
    status: OrderStatus | None = Field(None, description="Only orders in this status.")
    customer_name: str | None = Field(
        None, description="Case-insensitive match on part of the customer's name, e.g. 'Maria'."
    )
    older_than_days: int | None = Field(
        None, ge=0, le=365, description="Only orders placed more than this many days ago."
    )


class LookupOrderArgs(BaseModel):
    order_id: int = Field(..., description="Numeric order id, e.g. 1042.")


class OrderActionArgs(BaseModel):
    order_id: int = Field(..., description="Numeric order id the staff member named.")
    reason: str | None = Field(None, max_length=200, description="Optional short reason given by staff.")


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, default=str)


def _err(message: str, code: str = "error") -> str:
    return json.dumps({"ok": False, "error": message, "code": code})


def build_tools(store: StoreClient, guardrails_enabled: bool = True) -> list[BaseTool]:
    async def list_orders(status: OrderStatus | None = None, customer_name: str | None = None,
                          older_than_days: int | None = None) -> str:
        try:
            orders = await store.list_orders(status.value if status else None)
        except StoreError as exc:
            return _err(exc.message, exc.code)
        if customer_name:
            needle = customer_name.casefold()
            orders = [o for o in orders if needle in o["customer_name"].casefold()]
        if older_than_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            orders = [o for o in orders if datetime.fromisoformat(str(o["placed_at"])) < cutoff]
        filters = {"status": status.value if status else None, "customer_name": customer_name,
                   "older_than_days": older_than_days}
        return _ok(filters=filters, count=len(orders),
                   orders=[safe_order(o, guardrails_enabled) for o in orders])

    async def lookup_order(order_id: int) -> str:
        try:
            order = await store.lookup_order(order_id)
        except StoreError as exc:
            return _err(exc.message, exc.code)
        return _ok(order=safe_order(order, guardrails_enabled))

    async def _never_called(**_: Any) -> str:  # pragma: no cover - graph intercepts these
        raise RuntimeError("Action tools are executed only by the approval gate")

    return [
        StructuredTool.from_function(
            coroutine=list_orders, name="list_orders", args_schema=ListOrdersArgs,
            description="List this store's orders, newest first. Filter by status, customer name, or age.",
        ),
        StructuredTool.from_function(
            coroutine=lookup_order, name="lookup_order", args_schema=LookupOrderArgs,
            description="Fetch a single order of this store by id.",
        ),
        StructuredTool.from_function(
            coroutine=_never_called, name="refund_order", args_schema=OrderActionArgs,
            description="Propose a refund for an order. Requires explicit human approval before anything changes.",
        ),
        StructuredTool.from_function(
            coroutine=_never_called, name="cancel_order", args_schema=OrderActionArgs,
            description="Propose cancelling an order. Requires explicit human approval before anything changes.",
        ),
    ]
