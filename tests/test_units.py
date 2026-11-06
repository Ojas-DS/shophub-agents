import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shophub.common.request_context import bearer_token
from shophub.orders_agent.decisions import parse_decision
from shophub.orders_agent.store_client import StoreClient, StoreError
from shophub.store_api.app import create_app


@pytest.fixture
def store():
    return TestClient(create_app())


def h(token):
    return {"Authorization": f"Bearer {token}"}


def test_store_auth_me(store):
    assert store.get("/auth/me", headers=h("acme-staff-token")).json()["merchant"]["id"] == "merchant_a"
    assert store.get("/auth/me", headers=h("platform-auditor-token")).json() == {"user": "auditor", "merchant": None}
    assert store.get("/auth/me").status_code == 401


def test_store_scoping_and_status_codes(store):
    a = h("acme-staff-token")
    assert all(o["merchant"] == "merchant_a" for o in store.get("/orders", headers=a).json())
    assert {o["status"] for o in store.get("/orders?status=PAID", headers=a).json()} == {"PAID"}
    assert store.get("/orders", headers=h("platform-auditor-token")).status_code == 403
    assert store.get("/orders/lookup?id=2001", headers=a).json() == {"status": False, "message": "Order not found"}
    assert store.get("/orders/lookup?id=2001", headers=a).status_code == 200
    assert store.post("/orders/refund", json={"id": 2001}, headers=a).status_code == 404
    assert store.post("/orders/refund", json={"id": 9999}, headers=a).status_code == 404
    assert store.post("/orders/cancel", json={"id": 1002}, headers=a).status_code == 409
    assert store.post("/orders/refund", json={"id": 1001}, headers=a).json()["status"] == "REFUNDED"
    assert store.post("/orders/refund", json={"id": 1001}, headers=a).status_code == 409


def test_seed_has_required_shape():
    from shophub.store_api.seed import TOKENS, build_orders

    orders = build_orders()
    assert len(orders) >= 12 and {o.merchant for o in orders.values()} == {"merchant_a", "merchant_b"}
    assert sum("ignore" in o.customer_note.lower() or "system:" in o.customer_note.lower()
               for o in orders.values()) >= 2
    assert any(u.merchant_id is None for u in TOKENS.values())


@respx.mock
async def test_store_client_treats_200_status_false_as_failure():
    respx.get("http://store/orders/lookup").mock(
        return_value=httpx.Response(200, json={"status": False, "message": "Order not found"}))
    bearer_token.set("Bearer t")
    async with httpx.AsyncClient() as http:
        with pytest.raises(StoreError) as err:
            await StoreClient("http://store", http).lookup_order(1)
    assert err.value.code == "not_found"


@respx.mock
async def test_store_client_maps_http_errors_and_forwards_token():
    route = respx.post("http://store/orders/refund").mock(
        return_value=httpx.Response(409, json={"detail": "Cannot refund an order in status PLACED"}))
    bearer_token.set("Bearer exact-token")
    async with httpx.AsyncClient() as http:
        with pytest.raises(StoreError) as err:
            await StoreClient("http://store", http).refund_order(1)
    assert err.value.code == "conflict" and "PLACED" in err.value.message
    assert route.calls[0].request.headers["Authorization"] == "Bearer exact-token"


async def test_store_client_refuses_to_call_without_credential():
    bearer_token.set(None)
    async with httpx.AsyncClient() as http:
        with pytest.raises(PermissionError):
            await StoreClient("http://store", http).list_orders()


@pytest.mark.parametrize("reply,expected", [
    ("approve", "approve"), ("Approved", "approve"), ("yes, go ahead", "approve"), ("ok do it", "approve"),
    ("reject", "reject"), ("no", "reject"), ("don't do that", "reject"), ("no, do not approve", "reject"),
    ("yes... actually no", "reject"),
    ("", "unclear"), ("what is order 1006?", "unclear"), ("maybe later", "unclear"),
])
def test_parse_decision(reply, expected):
    assert parse_decision(reply) == expected
