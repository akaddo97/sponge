"""Stable internal alias for the LLM provider abstraction.

Sponge uses the `llm-providers` package (pluggable Claude / Gemini / OpenAI
behind a single Protocol). Until that package ships to PyPI, a vendored
copy lives in `sponge/_vendored/llm_providers.py`.

When `pip install llm-providers` becomes available, change ONE line:

    # before
    from sponge._vendored.llm_providers import ...
    # after
    from llm_providers import ...

Every other import in the codebase goes through `sponge._llm`, so the
swap is contained here.
"""
from sponge._vendored.llm_providers import (
    ClaudeProvider,
    GeminiProvider,
    Message,
    OpenAIProvider,
    Provider,
    Tool,
    default_provider_name,
    get_provider,
)

__all__ = [
    "ClaudeProvider",
    "GeminiProvider",
    "Message",
    "OpenAIProvider",
    "Provider",
    "Tool",
    "default_provider_name",
    "get_provider",
]
