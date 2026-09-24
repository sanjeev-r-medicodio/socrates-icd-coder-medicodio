"""Single entry point for structured model calls, with a replaceable backend.

    parse(tier="fast" | "reasoning", system=..., user=..., schema=PydanticModel) -> dict

The default backend is the Anthropic SDK, configured from .env
(ANTHROPIC_API_KEY, CLAUDE_MODEL_FAST, CLAUDE_MODEL_REASONING). Nothing is
read or constructed until the first call, so importing this module never
fails for lack of a key.

A host application routes every call through its own client (logging,
provider fallback, record/replay, cost tracking) with:

    llm.set_backend(fn)   # fn(tier, system, user, schema) -> dict
"""
import os
import threading
from typing import Callable, Optional, Type

from pydantic import BaseModel

from search_engine._env import load_dotenv, require_env

Backend = Callable[[str, str, str, Type[BaseModel]], dict]

TIER_ENV = {"fast": "CLAUDE_MODEL_FAST", "reasoning": "CLAUDE_MODEL_REASONING"}
# Reasoning-tier models think adaptively and thinking counts against
# max_tokens; the structured output itself is small.
MAX_TOKENS = {"fast": 2048, "reasoning": 4096}

_backend: Optional[Backend] = None
_client = None
_client_lock = threading.Lock()


def set_backend(fn: Optional[Backend]) -> None:
    """Route all model calls through `fn`; None restores the Anthropic default."""
    global _backend
    _backend = fn


def parse(*, tier: str, system: str, user: str, schema: Type[BaseModel]) -> dict:
    if tier not in TIER_ENV:
        raise ValueError(f"tier must be one of {sorted(TIER_ENV)}, got {tier!r}")
    return (_backend or _anthropic_backend)(tier, system, user, schema)


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            load_dotenv()
            import anthropic
            _client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY", "search_engine.llm"))
        return _client


def _anthropic_backend(tier: str, system: str, user: str, schema: Type[BaseModel]) -> dict:
    load_dotenv()
    model = require_env(TIER_ENV[tier], "search_engine.llm")
    response = _get_client().messages.parse(
        model=model,
        max_tokens=MAX_TOKENS[tier],
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    if response.parsed_output is None:
        raise RuntimeError(
            f"{model}: no parsed output (stop_reason={response.stop_reason!r}); "
            "if 'max_tokens', thinking used the whole budget"
        )
    return response.parsed_output.model_dump()


def configured_model(tier: str) -> Optional[str]:
    load_dotenv()
    return os.environ.get(TIER_ENV[tier])
