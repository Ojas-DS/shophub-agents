from conftest import ACME, chat, store_status


async def test_refund_produces_proposal_and_does_not_execute(stack):
    r = await chat(stack.client, ACME, "Refund order 1042", thread_id="h1")
    p = r.first("proposal")["proposal"]
    assert r.awaiting_approval
    assert p == {
        "type": "action_proposal", "action": "refund", "order_id": 1042,
        "merchant_id": "merchant_a", "merchant_name": "Acme Outfitters", "customer_name": "Liam Chen",
        "current_status": "PAID", "new_status": "REFUNDED", "amount": 89.0, "currency": "USD", "reason": None,
    }
    for fragment in ("#1042", "Acme Outfitters", "PAID -> REFUNDED", "$89.00"):
        assert fragment in r.final
    assert await store_status(stack, 1042) == "PAID"


async def test_approve_executes(stack):
    await chat(stack.client, ACME, "Refund order 1042", thread_id="h2")
    r = await chat(stack.client, ACME, "approve", thread_id="h2")
    assert "REFUNDED" in r.final and not r.awaiting_approval
    assert await store_status(stack, 1042) == "REFUNDED"


async def test_reject_does_not_execute(stack):
    await chat(stack.client, ACME, "Cancel order 1005", thread_id="h3")
    r = await chat(stack.client, ACME, "No, reject that", thread_id="h3")
    assert "rejected" in r.final.lower() and "not changed" in r.final.lower()
    assert await store_status(stack, 1005) == "PLACED"
    await chat(stack.client, ACME, "approve", thread_id="h3")
    assert await store_status(stack, 1005) == "PLACED"


async def test_cancel_approved_in_natural_language(stack):
    await chat(stack.client, ACME, "please cancel order 1005", thread_id="h4")
    r = await chat(stack.client, ACME, "yes, go ahead", thread_id="h4")
    assert "CANCELLED" in r.final
    assert await store_status(stack, 1005) == "CANCELLED"


async def test_unrelated_reply_discards_proposal_and_answers_new_question(stack):
    await chat(stack.client, ACME, "Refund order 1001", thread_id="h5")
    r = await chat(stack.client, ACME, "Actually, show me order 1006", thread_id="h5")
    assert r.first("notice") is not None
    assert "#1006" in r.final
    assert await store_status(stack, 1001) == "PAID"


async def test_invalid_transition_gets_no_proposal(stack):
    r = await chat(stack.client, ACME, "Cancel order 1002")
    assert r.first("proposal") is None and not r.awaiting_approval
    assert "DELIVERED" in r.final
    assert await store_status(stack, 1002) == "DELIVERED"


async def test_unknown_order_gets_no_proposal(stack):
    r = await chat(stack.client, ACME, "Refund order 9999")
    assert r.first("proposal") is None and "not found" in r.final.lower()


async def test_approval_revalidates_against_live_state(stack):
    await chat(stack.client, ACME, "Refund order 1042", thread_id="h6")
    await stack.http.post("http://store/orders/refund", json={"id": 1042}, headers={"Authorization": ACME})
    r = await chat(stack.client, ACME, "approve", thread_id="h6")
    assert "can only be refunded" in r.final
    assert "Done" not in r.final
