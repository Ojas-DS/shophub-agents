"""All configuration comes from environment variables (12-factor).

One Settings class is shared by the three services; each service only reads the
fields it needs. Nothing here has a secret default: the Anthropic key is read by
langchain-anthropic directly from ANTHROPIC_API_KEY and never touches our code.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM -----------------------------------------------------------------
    # "fake" = scripted, deterministic model (no API key, used by tests/evals).
    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_model: str = "claude-haiku-4-5"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 30.0

    # --- Service URLs ----------------------------------------------------------
    store_api_url: str = "http://localhost:8000"
    # URL the orders agent advertises in its Agent Card (how others reach it).
    orders_agent_public_url: str = "http://localhost:8001/"
    # Where the orchestrator looks for Agent Cards (comma separated base URLs).
    agent_card_urls: str = "http://localhost:8001"

    # --- Persistence -------------------------------------------------------------
    # postgresql://...  -> AsyncPostgresSaver
    # sqlite:///path    -> AsyncSqliteSaver (local dev without docker)
    # memory            -> InMemorySaver (unit tests)
    orchestrator_db_url: str = "sqlite:///./data/orchestrator.db"
    orders_agent_db_url: str = "sqlite:///./data/orders_agent.db"
    # SQLAlchemy async URL for the A2A task store (tasks survive restarts too).
    a2a_task_store_url: str = "sqlite+aiosqlite:///./data/a2a_tasks.db"

    # --- Safety ------------------------------------------------------------------
    # Only ever turned off by a test that proves the guardrail is load-bearing.
    guardrails_enabled: bool = True
    http_timeout_s: float = 10.0

    log_level: str = "INFO"

    @property
    def agent_card_url_list(self) -> list[str]:
        return [u.strip() for u in self.agent_card_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
