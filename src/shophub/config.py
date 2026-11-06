from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_model: str = "claude-haiku-4-5"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 30.0

    store_api_url: str = "http://localhost:8000"
    orders_agent_public_url: str = "http://localhost:8001/"
    agent_card_urls: str = "http://localhost:8001"

    orchestrator_db_url: str = "sqlite:///./data/orchestrator.db"
    orders_agent_db_url: str = "sqlite:///./data/orders_agent.db"
    a2a_task_store_url: str = "sqlite+aiosqlite:///./data/a2a_tasks.db"

    guardrails_enabled: bool = True
    http_timeout_s: float = 10.0

    log_level: str = "INFO"

    @property
    def agent_card_url_list(self) -> list[str]:
        return [u.strip() for u in self.agent_card_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
