from typing import Any

import httpx

from shophub.common.request_context import outbound_headers


class StoreError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class StoreClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._http = http

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, f"{self._base}{path}", headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            raise StoreError(f"The store service is unreachable ({type(exc).__name__}).", "unavailable") from exc

        if resp.status_code == 401:
            raise StoreError("Your session is not valid. Please sign in again.", "unauthorized")
        if resp.status_code == 403:
            raise StoreError("This account is not linked to a merchant store.", "forbidden")
        if resp.status_code == 404:
            raise StoreError("Order not found in your store.", "not_found")
        if resp.status_code == 409:
            raise StoreError(resp.json().get("detail", "That change is not allowed in the order's current state."), "conflict")
        if resp.status_code >= 400:
            raise StoreError(f"The store returned an error (HTTP {resp.status_code}).", "store_error")
        return resp.json()

    async def whoami(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/me")

    async def list_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return await self._request("GET", "/orders", params=params)

    async def lookup_order(self, order_id: int) -> dict[str, Any]:
        body = await self._request("GET", "/orders/lookup", params={"id": order_id})
        if not isinstance(body, dict) or body.get("status") is not True or "order" not in body:
            message = body.get("message", "Order not found") if isinstance(body, dict) else "Unexpected response"
            raise StoreError(f"Order {order_id}: {message} in your store.", "not_found")
        return body["order"]

    async def refund_order(self, order_id: int) -> dict[str, Any]:
        return await self._request("POST", "/orders/refund", json={"id": order_id})

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self._request("POST", "/orders/cancel", json={"id": order_id})
