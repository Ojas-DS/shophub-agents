from contextvars import ContextVar
from dataclasses import dataclass

bearer_token: ContextVar[str | None] = ContextVar("bearer_token", default=None)
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

CORRELATION_HEADER = "X-Correlation-ID"


@dataclass(frozen=True)
class Identity:
    user: str
    merchant_id: str
    merchant_name: str


current_identity: ContextVar[Identity | None] = ContextVar("current_identity", default=None)


def outbound_headers() -> dict[str, str]:
    token = bearer_token.get()
    if not token:
        raise PermissionError("No caller credential in request context")
    headers = {"Authorization": token}
    cid = correlation_id.get()
    if cid:
        headers[CORRELATION_HEADER] = cid
    return headers
