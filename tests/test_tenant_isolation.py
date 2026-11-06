import uuid

from conftest import ACME, AUDITOR, BOLT, chat, store_status

from shophub.llm import fake


async def test_merchant_a_cannot_see_merchant_b_order_by_id(stack):
    r = await chat(stack.client, ACME, "Show me order 2001")
    assert r.status == 200
    assert "not found" in r.final.lower()
    assert "Rossi" not in r.final and "999" not in r.final
    assert not any("Rossi" in t for t in fake.TRANSCRIPT)


async def test_merchant_a_cannot_refund_merchant_b_order(stack):
    r = await chat(stack.client, ACME, "Refund order 2001")
    assert not r.awaiting_approval and r.first("proposal") is None
    assert "not found" in r.final.lower()
    assert await store_status(stack, 2001, BOLT) == "PAID"


async def test_customer_search_only_returns_own_tenant(stack):
    r = await chat(stack.client, ACME, "What did customer Maria order last?")
    assert "Maria Garcia" in r.final and "Rossi" not in r.final
    r = await chat(stack.client, BOLT, "What did customer Maria order last?")
    assert "Maria Rossi" in r.final and "Garcia" not in r.final


async def test_listing_never_includes_foreign_orders(stack):
    r = await chat(stack.client, ACME, "List all orders")
    assert "#1001" in r.final
    assert not any(f"#{oid}" in r.final for oid in range(2001, 2007))


async def test_token_is_forwarded_unchanged_on_every_hop(stack):
    stack.recorder.auth_headers.clear()
    await chat(stack.client, ACME, "Which paid orders have not shipped in 3 days?")
    assert len(stack.recorder.auth_headers) >= 3
    assert set(stack.recorder.auth_headers) == {ACME}
    assert "/orders" in stack.recorder.paths


async def test_token_without_merchant_fails_closed(stack):
    r = await chat(stack.client, AUDITOR, "List all orders")
    assert r.status == 403
    assert fake.TRANSCRIPT == []
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": "list orders"}]}}}
    resp = await stack.http.post("http://orders-agent/", json=rpc, headers={"Authorization": AUDITOR})
    assert resp.status_code == 403


async def test_invalid_or_missing_token_is_rejected(stack):
    assert (await chat(stack.client, "Bearer not-a-real-token", "List orders")).status == 401
    resp = await stack.client.post("/chat", json={"message": "List orders"})
    assert resp.status_code == 401


async def test_other_merchant_cannot_touch_a_pending_a2a_task(stack):
    r = await chat(stack.client, ACME, "Refund order 1042")
    task_id = r.first("proposal")["task_id"]

    get = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": task_id}}
    resp = await stack.http.post("http://orders-agent/", json=get, headers={"Authorization": BOLT})
    assert "error" in resp.json() and "result" not in resp.json()

    approve = {"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": uuid.uuid4().hex, "taskId": task_id,
        "parts": [{"kind": "text", "text": "approve"}]}}}
    resp = await stack.http.post("http://orders-agent/", json=approve, headers={"Authorization": BOLT})
    assert "error" in resp.json()
    assert await store_status(stack, 1042) == "PAID"


async def test_conversation_threads_are_scoped_per_merchant(stack):
    await chat(stack.client, ACME, "Refund order 1042", thread_id="shared")
    r = await chat(stack.client, BOLT, "approve", thread_id="shared")
    assert r.first("proposal") is None and "REFUNDED" not in r.final
    assert await store_status(stack, 1042) == "PAID"
    hist = await stack.client.get("/threads/shared", headers={"Authorization": BOLT})
    assert all("1042" not in m["text"] for m in hist.json()["messages"])
