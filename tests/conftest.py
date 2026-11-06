import json
import os
import uuid

import pytest

from shophub.llm import fake
from shophub.store_api.app import create_app as create_store
from shophub.testing import running_stack, make_test_settings

os.environ.pop("ANTHROPIC_API_KEY", None)

ACME = "Bearer acme-staff-token"
BOLT = "Bearer bolt-staff-token"
AUDITOR = "Bearer platform-auditor-token"


class RecordingASGI:
    def __init__(self, app):
        self.app = app
        self.auth_headers: list[str | None] = []
        self.paths: list[str] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict((k.decode(), v.decode()) for k, v in scope["headers"])
            self.auth_headers.append(headers.get("authorization"))
            self.paths.append(scope["path"])
        await self.app(scope, receive, send)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


class ChatResult:
    def __init__(self, status: int, events: list[tuple[str, dict]]):
        self.status = status
        self.events = events

    @property
    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def first(self, name: str) -> dict | None:
        return next((d for e, d in self.events if e == name), None)

    @property
    def final(self) -> str:
        f = self.first("final")
        return f["text"] if f else ""

    @property
    def awaiting_approval(self) -> bool:
        f = self.first("final")
        return bool(f and f["awaiting_approval"])


async def chat(client, token: str, message: str, thread_id: str | None = None) -> ChatResult:
    resp = await client.post("/chat", json={"message": message, "thread_id": thread_id or uuid.uuid4().hex[:8]},
                             headers={"Authorization": token})
    return ChatResult(resp.status_code, parse_sse(resp.text) if resp.status_code == 200 else [])


async def store_status(stack, order_id: int, token: str = ACME) -> str:
    r = await stack.http.get(f"http://store/orders/lookup?id={order_id}", headers={"Authorization": token})
    return r.json()["order"]["status"]


@pytest.fixture(autouse=True)
def _clear_transcript():
    fake.TRANSCRIPT.clear()
    yield


@pytest.fixture
def recording_store():
    return RecordingASGI(create_store())


@pytest.fixture
async def stack(recording_store):
    async with running_stack(make_test_settings(), store_app=recording_store) as s:
        s.recorder = recording_store
        yield s
