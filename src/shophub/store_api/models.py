"""Data structures for the mock store. Every seed value has an explicit type."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    PLACED = "PLACED"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


# The order state machine: which actions are legal from which states.
# The store enforces this (409 otherwise); the agent pre-checks it only to give
# a better message, the store stays the source of truth.
REFUNDABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.DELIVERED}
)
CANCELLABLE: frozenset[OrderStatus] = frozenset({OrderStatus.PLACED, OrderStatus.PAID})


class Merchant(BaseModel):
    id: str
    name: str


class StaffUser(BaseModel):
    """What a bearer token resolves to. merchant_id=None => platform user."""

    user: str
    merchant_id: str | None


class Order(BaseModel):
    id: int
    merchant: str  # merchant id, e.g. "merchant_a"
    customer_name: str
    customer_email: str
    customer_phone: str
    status: OrderStatus
    total: float = Field(ge=0)
    placed_at: datetime
    customer_note: str = ""


class OrderAction(BaseModel):
    """Body of POST /orders/refund and /orders/cancel."""

    id: int
