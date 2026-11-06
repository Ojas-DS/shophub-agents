from conftest import ACME, chat, store_status

from shophub.store_api.app import create_app as create_store
from shophub.testing import running_stack, make_test_settings


async def test_pending_approval_survives_restart(tmp_path):
    settings = make_test_settings(
        orchestrator_db_url=f"sqlite:///{tmp_path}/orch.db",
        orders_agent_db_url=f"sqlite:///{tmp_path}/orders.db",
        a2a_task_store_url=f"sqlite+aiosqlite:///{tmp_path}/a2a.db",
    )
    store = create_store()

    async with running_stack(settings, store_app=store) as s:
        r = await chat(s.client, ACME, "Refund order 1042", thread_id="restart-demo")
        assert r.awaiting_approval

    async with running_stack(settings, store_app=store) as s:
        hist = (await s.client.get("/threads/restart-demo", headers={"Authorization": ACME})).json()
        assert hist["awaiting_approval"] is True
        assert any("Refund order 1042" in m["text"] for m in hist["messages"])

        r = await chat(s.client, ACME, "approve", thread_id="restart-demo")
        assert "REFUNDED" in r.final
        assert await store_status(s, 1042) == "REFUNDED"


async def test_bearer_token_is_never_persisted(tmp_path):
    settings = make_test_settings(
        orchestrator_db_url=f"sqlite:///{tmp_path}/orch.db",
        orders_agent_db_url=f"sqlite:///{tmp_path}/orders.db",
        a2a_task_store_url=f"sqlite+aiosqlite:///{tmp_path}/a2a.db",
    )
    async with running_stack(settings) as s:
        await chat(s.client, ACME, "Refund order 1042", thread_id="t")
        await chat(s.client, ACME, "approve", thread_id="t")
    for db in tmp_path.glob("*.db"):
        assert b"acme-staff-token" not in db.read_bytes(), db.name
