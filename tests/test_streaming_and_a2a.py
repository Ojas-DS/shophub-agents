import uuid

from conftest import ACME, chat

from shophub.testing import running_stack, make_test_settings


async def test_sse_event_order(stack):
    r = await chat(stack.client, ACME, "Which paid orders have not shipped in 3 days?")
    names = r.names
    assert names[0] == "meta" and names[-1] == "final"
    assert names.index("tool_started") < names.index("remote_status") < names.index("tool_finished")
    assert names.count("token") > 3
    assert "".join(d["text"] for e, d in r.events if e == "token") == r.final
    assert "#1001" in r.final and "#1042" in r.final and "#1006" not in r.final


async def test_agent_card_is_public_and_describes_skills(stack):
    card = (await stack.http.get("http://orders-agent/.well-known/agent-card.json")).json()
    assert card["name"] == "orders_agent"
    assert card["url"] == "http://orders-agent/"
    assert {s["id"] for s in card["skills"]} == {"order_lookup", "order_search", "order_actions"}
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert card["capabilities"]["streaming"] is True


async def test_orchestrator_discovers_agents_from_cards(stack):
    agents = (await stack.client.get("/agents", headers={"Authorization": ACME})).json()
    assert agents[0]["name"] == "orders_agent" and agents[0]["tool"] == "ask_orders_agent"


async def test_a2a_endpoint_requires_token(stack):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": "list orders"}]}}}
    assert (await stack.http.post("http://orders-agent/", json=rpc)).status_code == 401


async def test_a2a_jsonrpc_direct_call_works(stack):
    rpc = {"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": "show order 1001"}]}}}
    body = (await stack.http.post("http://orders-agent/", json=rpc, headers={"Authorization": ACME})).json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 7
    assert body["result"]["status"]["state"] == "completed"
    text = body["result"]["status"]["message"]["parts"][0]["text"]
    assert "#1001" in text and "Maria Garcia" in text


async def test_legacy_envelope_is_reported_as_failure(stack):
    r = await chat(stack.client, ACME, "What is the status of order 5555?")
    assert "not found" in r.final.lower()
    assert "5555" in r.final


async def test_unreachable_agent_degrades_gracefully():
    async with running_stack(make_test_settings(agent_card_urls="http://nowhere")) as s:
        r = await chat(s.client, ACME, "List all orders")
        assert r.status == 200 and "unavailable" in r.final.lower()


async def test_off_topic_question_is_answered_without_delegation(stack):
    r = await chat(stack.client, ACME, "hello, who are you?")
    assert "tool_started" not in r.names and "support assistant" in r.final
