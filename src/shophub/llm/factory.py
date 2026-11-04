"""The ONE place a chat model is created. Swap provider via LLM_PROVIDER."""

from typing import Literal

from langchain_core.language_models import BaseChatModel

from shophub.config import Settings

AgentRole = Literal["orchestrator", "orders_agent"]


def make_chat_model(role: AgentRole, settings: Settings) -> BaseChatModel:
    """Return a LangChain chat model for an agent role.

    fake      -> deterministic scripted model (no key, no network). Each role gets
                 its own policy, because the two agents have different jobs.
    anthropic -> ChatAnthropic; needs ANTHROPIC_API_KEY in the environment.
    """
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
