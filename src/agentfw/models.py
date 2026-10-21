"""Turn a role's model block into a LangChain chat model.

Providers:
  openrouter  ChatOpenAI pointed at https://openrouter.ai/api/v1, key in OPENROUTER_API_KEY
  openai      ChatOpenAI, key in OPENAI_API_KEY
  anthropic   ChatAnthropic (pip install langchain-anthropic), key in ANTHROPIC_API_KEY
  ollama      ChatOllama (pip install langchain-ollama), local, no key
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel

OPENROUTER_URL = "https://openrouter.ai/api/v1"
KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ModelConfigError(Exception):
    pass


def make_model(cfg: Mapping[str, Any], model_id: str | None = None) -> BaseChatModel:
    provider = cfg["provider"]
    model_id = model_id or cfg["id"]
    if not model_id or model_id.startswith("<"):
        raise ModelConfigError(
            f"no model chosen (id is {model_id!r}); set model.id in the role file, model_id in the project's "
            "roles list, or pass --model"
        )
    key_env = KEY_ENV.get(provider)
    if key_env and not os.environ.get(key_env):
        raise ModelConfigError(f"{key_env} isn't set (needed for provider {provider!r})")

    common = {"temperature": cfg.get("temperature", 0.2)}
    if cfg.get("max_output_tokens"):
        common["max_tokens"] = cfg["max_output_tokens"]

    if provider in ("openrouter", "openai"):
        from langchain_openai import ChatOpenAI

        extra = {}
        if provider == "openrouter":
            extra = {"base_url": OPENROUTER_URL,
                     "default_headers": {"X-Title": "agentfw"}}
        return ChatOpenAI(model=model_id, api_key=os.environ[key_env], max_retries=3, timeout=180, **extra, **common)
    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise ModelConfigError("provider 'anthropic' needs: pip install langchain-anthropic") from e
        return ChatAnthropic(model=model_id, max_retries=3, **common)
    if provider == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise ModelConfigError("provider 'ollama' needs: pip install langchain-ollama") from e
        common.pop("max_tokens", None)
        return ChatOllama(model=model_id, **common)
    raise ModelConfigError(f"unknown provider {provider!r}")


def make_fallbacks(cfg: Mapping[str, Any]) -> list[BaseChatModel]:
    """Fallback models from the role's fallback list, skipping placeholders."""
    return [make_model(cfg, m) for m in cfg.get("fallback", []) if m and not m.startswith("<")]
