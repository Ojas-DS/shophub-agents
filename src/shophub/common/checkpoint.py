"""R7 - one factory for the LangGraph checkpointer, chosen by URL.

postgresql://...  -> AsyncPostgresSaver   (docker compose / production)
sqlite:///path    -> AsyncSqliteSaver     (local dev without docker, restart test)
memory            -> InMemorySaver        (fast unit tests)
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


@asynccontextmanager
async def make_checkpointer(url: str) -> AsyncIterator[BaseCheckpointSaver]:
    if url == "memory":
        yield InMemorySaver()
        return

    if url.startswith(("postgresql://", "postgres://")):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()  # idempotent: creates/migrates checkpoint tables
            yield saver
        return

    if url.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = url.removeprefix("sqlite:///")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            yield saver
        return

    raise ValueError(f"Unsupported checkpointer URL: {url!r}")
