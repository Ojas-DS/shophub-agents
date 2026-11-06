from typing import Literal

from langchain_core.language_models import BaseChatModel

from shophub.config import Settings

AgentRole = Literal["orchestrator", "orders_agent"]


def make_chat_model(role: AgentRole, settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "fake":
        from shophub.llm.fake import ScriptedChatModel, orchestrator_policy, orders_agent_policy

        policy = orchestrator_policy if role == "orchestrator" else orders_agent_policy
        return ScriptedChatModel(policy=policy, role=role)

    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.anthropic_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_s,
            max_tokens=1024,
            max_retries=2,
        )

    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")
