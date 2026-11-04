"""Request-scoped values carried with contextvars.

Why contextvars and not graph state?
  * The bearer token must be forwarded unchanged on every hop (R3) but must
    NEVER be persisted. Anything in LangGraph state (or `configurable`, which is
    copied into checkpoint metadata) ends up in Postgres. A ContextVar lives only
    for the current asyncio task, i.e. the current HTTP request, so it cannot
    leak to another user or survive a restart.
  * The correlation id (stretch goal S3) follows the same path for the same
    reason: it is per-request metadata, not conversation state.
"""

from contextvars import ContextVar
from dataclasses import dataclass

# Exactly the "Bearer xxx" value the user sent to the chat API. Never modified.
bearer_token: ContextVar[str | None] = ContextVar("bearer_token", default=None)
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

CORRELATION_HEADER = "X-Correlation-ID"


@dataclass(frozen=True)
class Identity:
    """Who is calling, as reported by the store API (the source of truth)."""

    user: str
    merchant_id: str
    merchant_name: str


def outbound_headers() -> dict[str, str]:
    """Headers for any downstream call: the caller's own token + correlation id.

    Raises if no token is set so that a missing credential fails closed instead
    of silently making an unauthenticated call.
    """
    token = bearer_token.get()
    if not token:
        raise PermissionError("No caller credential in request context")
    headers = {"Authorization": token}
    cid = correlation_id.get()
    if cid:
        headers[CORRELATION_HEADER] = cid
    return headers
