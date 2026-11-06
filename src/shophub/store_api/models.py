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


REFUNDABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.DELIVERED}
)
CANCELLABLE: frozenset[OrderStatus] = frozenset({OrderStatus.PLACED, OrderStatus.PAID})


class Merchant(BaseModel):
    id: str
    name: str


class StaffUser(BaseModel):
    user: str
    merchant_id: str | None


class Order(BaseModel):
    id: int
    merchant: str
    customer_name: str
    customer_email: str
    customer_phone: str
    status: OrderStatus
    total: float = Field(ge=0)
    placed_at: datetime
    customer_note: str = ""


class OrderAction(BaseModel):
    id: int
