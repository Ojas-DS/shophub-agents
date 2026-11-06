import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from shophub.common.request_context import CORRELATION_HEADER, correlation_id
from shophub.store_api.models import (
    CANCELLABLE,
    REFUNDABLE,
    Order,
    OrderAction,
    OrderStatus,
    StaffUser,
)
from shophub.store_api.seed import MERCHANTS, TOKENS, build_orders

log = logging.getLogger("store_api")


def create_app() -> FastAPI:
    app = FastAPI(title="ShopHub Mock Store API", version="1.0.0")
    orders: dict[int, Order] = build_orders()

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        correlation_id.set(request.headers.get(CORRELATION_HEADER))
        response = await call_next(request)
        log.info(
            "request",
            extra={"fields": {"method": request.method, "path": request.url.path,
                              "status": response.status_code}},
        )
        return response

    def current_user(authorization: Annotated[str | None, Header()] = None) -> StaffUser:
        if not authorization:
            raise HTTPException(401, "Missing bearer token", {"WWW-Authenticate": "Bearer"})
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Malformed Authorization header", {"WWW-Authenticate": "Bearer"})
        user = TOKENS.get(token.strip())
        if user is None:
            raise HTTPException(401, "Invalid bearer token", {"WWW-Authenticate": "Bearer"})
        return user

    def current_merchant(user: Annotated[StaffUser, Depends(current_user)]) -> str:
        if user.merchant_id is None:
            raise HTTPException(403, "Token is not scoped to a merchant")
        return user.merchant_id

    def owned_order(order_id: int, merchant: str) -> Order:
        order = orders.get(order_id)
        if order is None or order.merchant != merchant:
            raise HTTPException(404, "Order not found")
        return order

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/auth/me")
    def auth_me(user: Annotated[StaffUser, Depends(current_user)]) -> dict:
        merchant = MERCHANTS.get(user.merchant_id) if user.merchant_id else None
        return {"user": user.user, "merchant": merchant.model_dump() if merchant else None}

    @app.get("/orders")
    def list_orders(
        merchant: Annotated[str, Depends(current_merchant)],
        status: OrderStatus | None = None,
    ) -> list[Order]:
        return [
            o for o in sorted(orders.values(), key=lambda o: o.placed_at, reverse=True)
            if o.merchant == merchant and (status is None or o.status == status)
        ]

    @app.get("/orders/lookup")
    def lookup_order(merchant: Annotated[str, Depends(current_merchant)], id: int) -> dict:
        order = orders.get(id)
        if order is None or order.merchant != merchant:
            return {"status": False, "message": "Order not found"}
        return {"status": True, "order": order.model_dump(mode="json")}

    @app.post("/orders/refund")
    def refund(body: OrderAction, merchant: Annotated[str, Depends(current_merchant)]) -> Order:
        order = owned_order(body.id, merchant)
        if order.status not in REFUNDABLE:
            raise HTTPException(409, f"Cannot refund an order in status {order.status.value}")
        order.status = OrderStatus.REFUNDED
        log.info("order refunded", extra={"fields": {"order_id": order.id, "merchant": merchant}})
        return order

    @app.post("/orders/cancel")
    def cancel(body: OrderAction, merchant: Annotated[str, Depends(current_merchant)]) -> Order:
        order = owned_order(body.id, merchant)
        if order.status not in CANCELLABLE:
            raise HTTPException(409, f"Cannot cancel an order in status {order.status.value}")
        order.status = OrderStatus.CANCELLED
        log.info("order cancelled", extra={"fields": {"order_id": order.id, "merchant": merchant}})
        return order

    return app
