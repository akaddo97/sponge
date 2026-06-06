"""
Provider abstraction for the LLM layer.

Every conversation / call site goes through `get_provider(...)` and consumes
either `chat()` (streaming, multi-turn, tool-using) or `complete()` (sync
single-shot, may include few-shot or multi-turn history). The shapes are
unified across providers so call sites don't branch by name.

Streaming chunk contract (every Provider.chat() yields these dicts):

  {"type": "text", "text": str}
  {"type": "tool_use_start", "tool": {"id", "name", "input_json": ""}}
  {"type": "tool_use_input", "partial_json": str}
  {"type": "tool_use_end",   "tool": {"id", "name", "input": {...parsed...}}}
  {"type": "stop", "stop_reason": str, "usage": {input_tokens, output_tokens, ...}}

`stop_reason` is normalised across providers to one of:

  - "end_turn"   — model finished naturally (also Claude `stop_sequence`,
                   OpenAI `stop`, Gemini `stop`).
  - "tool_use"   — model wants to invoke a tool (OpenAI `tool_calls`,
                   `function_call`).
  - "max_tokens" — hit the token cap (OpenAI `length`).
  - "safety"     — content / safety filter blocked (Claude `refusal`,
                   Gemini `recitation` / `blocklist` / `prohibited_content`
                   / `spii`, OpenAI `content_filter`).
  - "other"      — anything else (unknown values, Gemini `language`,
                   Claude `pause_turn`).

Callers can branch on this canonical set without provider-name checks.

Routes can forward these chunks to the browser as SSE without knowing which
provider produced them. tool_use_end carries the fully-parsed input so the
route can execute the tool without rebuilding JSON.

`system` is passed positionally — Anthropic-style: not a message, separate
field. Each provider translates to its native shape (Claude system block
with optional cache_control; OpenAI prepended system message; Gemini
system_instruction).

Tool definitions use Anthropic shape `{name, description, input_schema}`.
Each provider translates internally so callers see one canonical surface.
"""
from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Iterator, Protocol, TypedDict


try:
    __version__ = _pkg_version("llm-providers")
except PackageNotFoundError:
    # Editable install without metadata, or local-source import without
    # `pip install -e .` — fall back to the in-tree version constant.
    __version__ = "0.1.0"


__all__ = [
    "__version__",
    "Provider",
    "Message",
    "Tool",
    "ClaudeProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OpenAICompatibleProvider",
    "CLAUDE_DEFAULT_MODEL",
    "GEMINI_DEFAULT_MODEL",
    "OPENAI_DEFAULT_MODEL",
    "DEEPSEEK_DEFAULT_MODEL",
    "get_provider",
    "default_provider_name",
]


# Default model per provider. Bump these as providers ship new flagships;
# they're centralised so callers can also `from llm_providers import
# CLAUDE_DEFAULT_MODEL` if they want the same default at their boundary.
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-6"
GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"
OPENAI_DEFAULT_MODEL = "gpt-4o"
# Default for the DeepSeek preset (an OpenAI-compatible endpoint). Other
# OpenAI-compatible presets leave the model unset — you name the one your
# endpoint serves. `deepseek-reasoner` also works; its chain-of-thought lands
# in a separate `reasoning_content` field that complete()/chat() ignore, so
# callers get the answer, not the scratchpad.
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"


# Canonical stop_reason vocabulary — see module docstring.
_STOP_END_TURN = "end_turn"
_STOP_TOOL_USE = "tool_use"
_STOP_MAX_TOKENS = "max_tokens"
_STOP_SAFETY = "safety"
_STOP_OTHER = "other"

_STOP_MAPS: dict[str, dict[str, str]] = {
    "claude": {
        "end_turn": _STOP_END_TURN,
        "stop_sequence": _STOP_END_TURN,
        "tool_use": _STOP_TOOL_USE,
        "max_tokens": _STOP_MAX_TOKENS,
        "refusal": _STOP_SAFETY,
        "pause_turn": _STOP_OTHER,
    },
    "gemini": {
        "stop": _STOP_END_TURN,
        "max_tokens": _STOP_MAX_TOKENS,
        "safety": _STOP_SAFETY,
        "recitation": _STOP_SAFETY,
        "blocklist": _STOP_SAFETY,
        "prohibited_content": _STOP_SAFETY,
        "spii": _STOP_SAFETY,
        "language": _STOP_OTHER,
        "other": _STOP_OTHER,
        "malformed_function_call": _STOP_OTHER,
    },
    "openai": {
        "stop": _STOP_END_TURN,
        "length": _STOP_MAX_TOKENS,
        "tool_calls": _STOP_TOOL_USE,
        "function_call": _STOP_TOOL_USE,
        "content_filter": _STOP_SAFETY,
    },
}


def _normalize_stop_reason(provider: str, raw: str | None) -> str:
    if raw is None:
        return _STOP_OTHER
    return _STOP_MAPS.get(provider, {}).get(str(raw).lower(), _STOP_OTHER)


class Message(TypedDict, total=False):
    role: str  # "user" | "assistant"
    content: str | list  # str (text-only) or content-block list


class Tool(TypedDict):
    name: str
    description: str
    input_schema: dict


class Provider(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[Message],
        system: str | list,
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[dict]: ...

    def complete(
        self,
        messages: list[Message],
        system: str | list = "",
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        """Sync single-shot. Returns assistant text. No streaming, no tools.

        Accepts a messages list (not a single prompt string) because most
        sync call sites build a messages list anyway: few-shot pairs,
        multi-turn history, or a single user turn.
        """
        ...


# --- Claude ---


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        model: str = CLAUDE_DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = None  # lazy

    def _ensure_client(self):
        if self._client is None:
            import anthropic
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def chat(
        self,
        messages: list[Message],
        system: str | list,
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[dict]:
        client = self._ensure_client()
        # Normalize system → blocks with ephemeral cache_control. Caching
        # the system prompt cuts ~6x off token cost on multi-turn chats.
        if isinstance(system, str):
            system_blocks = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_blocks = system

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "system": system_blocks,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        with client.messages.stream(**kwargs) as stream:
            current_tool: dict | None = None

            for event in stream:
                t = event.type

                if t == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool = {
                            "id": block.id,
                            "name": block.name,
                            "input_json": "",
                        }
                        yield {"type": "tool_use_start", "tool": dict(current_tool)}

                elif t == "content_block_delta":
                    d = event.delta
                    if d.type == "text_delta":
                        yield {"type": "text", "text": d.text}
                    elif d.type == "input_json_delta" and current_tool is not None:
                        current_tool["input_json"] += d.partial_json
                        yield {
                            "type": "tool_use_input",
                            "partial_json": d.partial_json,
                        }

                elif t == "content_block_stop":
                    if current_tool is not None:
                        try:
                            parsed = json.loads(current_tool["input_json"]) if current_tool["input_json"] else {}
                        except json.JSONDecodeError:
                            parsed = {}
                        yield {
                            "type": "tool_use_end",
                            "tool": {
                                "id": current_tool["id"],
                                "name": current_tool["name"],
                                "input": parsed,
                            },
                        }
                        current_tool = None

                elif t == "message_stop":
                    final = stream.get_final_message()
                    usage = {
                        "input_tokens": final.usage.input_tokens,
                        "output_tokens": final.usage.output_tokens,
                    }
                    if hasattr(final.usage, "cache_creation_input_tokens"):
                        usage["cache_creation_input_tokens"] = final.usage.cache_creation_input_tokens or 0
                    if hasattr(final.usage, "cache_read_input_tokens"):
                        usage["cache_read_input_tokens"] = final.usage.cache_read_input_tokens or 0
                    yield {
                        "type": "stop",
                        "stop_reason": _normalize_stop_reason("claude", final.stop_reason),
                        "usage": usage,
                    }

    def complete(
        self,
        messages: list[Message],
        system: str | list = "",
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        client = self._ensure_client()
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            # No cache_control on complete() — single-shot calls don't
            # repeat the system prompt across turns, so caching has no payoff.
            # String and block-list system prompts pass through as-is; the
            # Anthropic SDK accepts both shapes natively.
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = client.messages.create(**kwargs)
        return "".join(b.text for b in response.content if b.type == "text").strip()


# --- Gemini ---


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        model: str = GEMINI_DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = None  # lazy

    def _ensure_client(self):
        if self._client is None:
            from google import genai
            key = self._api_key or os.environ.get("GEMINI_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=key)
        return self._client

    @staticmethod
    def _system_text(system: str | list) -> str | None:
        if not system:
            return None
        if isinstance(system, str):
            return system
        # Anthropic-style list of blocks → concat text
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts) or None

    @staticmethod
    def _to_contents(messages: list[Message]) -> list[dict]:
        """Translate canonical messages → Gemini contents.

        - Anthropic role "assistant" → Gemini role "model".
        - String content → single text part.
        - List content (Anthropic blocks) → text parts only (tool blocks
          ignored here; tool-use streaming for Gemini is not v1 scope).
        """
        contents = []
        for msg in messages:
            role = "model" if msg.get("role") == "assistant" else "user"
            content = msg.get("content", "")
            if isinstance(content, str):
                parts = [{"text": content}]
            else:
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append({"text": block.get("text", "")})
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    def complete(
        self,
        messages: list[Message],
        system: str | list = "",
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        client = self._ensure_client()
        from google.genai import types
        cfg_kwargs: dict = {"max_output_tokens": max_tokens}
        sys_text = self._system_text(system)
        if sys_text:
            cfg_kwargs["system_instruction"] = sys_text
        if temperature is not None:
            cfg_kwargs["temperature"] = temperature
        response = client.models.generate_content(
            model=self.model,
            contents=self._to_contents(messages),
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        return (response.text or "").strip()

    def chat(
        self,
        messages: list[Message],
        system: str | list,
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[dict]:
        if tools:
            # v1 scope: text-only streaming. Tool-using sites should route
            # to a provider that supports them.
            raise NotImplementedError(
                "GeminiProvider.chat() does not yet support tools. "
                "Route tool-using sites to claude or openai for now."
            )
        client = self._ensure_client()
        from google.genai import types
        cfg_kwargs: dict = {"max_output_tokens": max_tokens}
        sys_text = self._system_text(system)
        if sys_text:
            cfg_kwargs["system_instruction"] = sys_text
        if temperature is not None:
            cfg_kwargs["temperature"] = temperature

        stream = client.models.generate_content_stream(
            model=self.model,
            contents=self._to_contents(messages),
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        usage_meta = None
        stop_reason = _STOP_END_TURN
        for chunk in stream:
            if getattr(chunk, "text", None):
                yield {"type": "text", "text": chunk.text}
            if getattr(chunk, "usage_metadata", None) is not None:
                usage_meta = chunk.usage_metadata
            cands = getattr(chunk, "candidates", None) or []
            for cand in cands:
                fr = getattr(cand, "finish_reason", None)
                if fr is not None:
                    raw_tail = str(fr).lower().split(".")[-1]
                    stop_reason = _normalize_stop_reason("gemini", raw_tail)

        usage = {"input_tokens": 0, "output_tokens": 0}
        if usage_meta is not None:
            usage["input_tokens"] = getattr(usage_meta, "prompt_token_count", 0) or 0
            usage["output_tokens"] = getattr(usage_meta, "candidates_token_count", 0) or 0
        yield {"type": "stop", "stop_reason": stop_reason, "usage": usage}


# --- OpenAI ---


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        model: str = OPENAI_DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = None  # lazy

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            key = self._api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY not set")
            self._client = OpenAI(api_key=key)
        return self._client

    @staticmethod
    def _system_text(system: str | list) -> str:
        if not system:
            return ""
        if isinstance(system, str):
            return system
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)

    @staticmethod
    def _to_messages(system: str | list, messages: list[Message]) -> list[dict]:
        """Translate canonical (system, messages) → OpenAI messages list.

        Anthropic content-block lists are flattened to text; tool_use /
        tool_result blocks are dropped here (tool-use translation in chat()
        rebuilds them as `tool_calls` / `tool` role messages).
        """
        out = []
        sys_text = OpenAIProvider._system_text(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
            else:
                # Flatten Anthropic blocks; preserve text only for complete().
                # chat() with tools would handle tool_use/tool_result here too.
                text = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                out.append({"role": role, "content": text})
        return out

    @staticmethod
    def _translate_tools(tools: list[Tool]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in tools
        ]

    def complete(
        self,
        messages: list[Message],
        system: str | list = "",
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> str:
        client = self._ensure_client()
        kwargs: dict = {
            "model": self.model,
            "messages": self._to_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        return text.strip()

    def chat(
        self,
        messages: list[Message],
        system: str | list,
        tools: list[Tool] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> Iterator[dict]:
        client = self._ensure_client()
        kwargs: dict = {
            "model": self.model,
            "messages": self._to_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = self._translate_tools(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Track in-progress tool calls by index — OpenAI deltas reference
        # the same tool by index across chunks.
        tool_calls: dict[int, dict] = {}
        stop_reason = _STOP_END_TURN
        usage = {"input_tokens": 0, "output_tokens": 0}

        for chunk in client.chat.completions.create(**kwargs):
            if getattr(chunk, "usage", None) is not None:
                usage["input_tokens"] = getattr(chunk.usage, "prompt_tokens", 0) or 0
                usage["output_tokens"] = getattr(chunk.usage, "completion_tokens", 0) or 0
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                if getattr(delta, "content", None):
                    yield {"type": "text", "text": delta.content}
                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": getattr(tc, "id", "") or "",
                            "name": "",
                            "input_json": "",
                        }
                    fn = getattr(tc, "function", None)
                    # Update id + name first so the start check below sees the
                    # latest state; defer accumulating THIS delta's args until
                    # after the start check, so the flush below carries only
                    # args from PREVIOUS deltas.
                    if fn is not None and getattr(fn, "name", None):
                        tool_calls[idx]["name"] = fn.name
                    if getattr(tc, "id", None):
                        tool_calls[idx]["id"] = tc.id
                    # Emit start once we have id+name. Spec requires tool_use_start
                    # before any tool_use_input for a given tool; if args arrived
                    # in earlier deltas before name (OpenAI doesn't do this today,
                    # but the SDK doesn't promise the ordering), flush them now
                    # as a single tool_use_input so consumers see a continuous
                    # JSON stream.
                    if not tool_calls[idx].get("_started") and tool_calls[idx]["id"] and tool_calls[idx]["name"]:
                        tool_calls[idx]["_started"] = True
                        yield {
                            "type": "tool_use_start",
                            "tool": {
                                "id": tool_calls[idx]["id"],
                                "name": tool_calls[idx]["name"],
                                "input_json": "",
                            },
                        }
                        if tool_calls[idx]["input_json"]:
                            yield {
                                "type": "tool_use_input",
                                "partial_json": tool_calls[idx]["input_json"],
                            }
                    if fn is not None and getattr(fn, "arguments", None):
                        tool_calls[idx]["input_json"] += fn.arguments
                        if tool_calls[idx].get("_started"):
                            yield {
                                "type": "tool_use_input",
                                "partial_json": fn.arguments,
                            }
            fr = getattr(choice, "finish_reason", None)
            if fr is not None:
                stop_reason = _normalize_stop_reason("openai", fr)

        # Close out any in-progress tool calls.
        for tc in tool_calls.values():
            if not tc.get("_started"):
                yield {
                    "type": "tool_use_start",
                    "tool": {"id": tc["id"], "name": tc["name"], "input_json": ""},
                }
            try:
                parsed = json.loads(tc["input_json"]) if tc["input_json"] else {}
            except json.JSONDecodeError:
                parsed = {}
            yield {
                "type": "tool_use_end",
                "tool": {"id": tc["id"], "name": tc["name"], "input": parsed},
            }
        yield {"type": "stop", "stop_reason": stop_reason, "usage": usage}


# --- OpenAI-compatible (DeepSeek, Ollama, vLLM, llama.cpp, Groq, ...) ---


class OpenAICompatibleProvider(OpenAIProvider):
    """One adapter for every OpenAI-compatible ``/chat/completions`` backend.

    DeepSeek, Ollama, vLLM, llama.cpp, LM Studio, Groq, Together, OpenRouter,
    Fireworks, Mistral, or your own gateway — they all speak the same wire
    format the OpenAI SDK emits. So this reuses ``OpenAIProvider``'s request,
    streaming, and tool-use translation wholesale; only the client's
    ``base_url`` and key source differ.

    Point it at anything::

        OpenAICompatibleProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            name="deepseek",
        )

    Self-hosted backends (Ollama, llama.cpp, vLLM) need no key: pass
    ``api_key_env=""`` and a placeholder is sent (the SDK requires a
    non-empty string, the server ignores it). For the common providers use
    the presets via ``get_provider("deepseek")`` etc. — see ``get_provider``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        name: str = "openai-compatible",
    ) -> None:
        if not base_url:
            raise ValueError(
                "OpenAICompatibleProvider requires a base_url "
                "(e.g. https://api.deepseek.com)"
            )
        if not model:
            raise ValueError(
                f"OpenAICompatibleProvider requires a model for {name!r} — "
                "pass model=... (the model id your endpoint serves)"
            )
        super().__init__(model=model, api_key=api_key)
        self.base_url = base_url.rstrip("/")
        self.name = name  # instance attr shadows OpenAIProvider.name ("openai")
        self._api_key_env = api_key_env

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            key = self._api_key
            if not key and self._api_key_env:
                key = os.environ.get(self._api_key_env)
            # Self-hosted backends accept any key; the SDK still wants a string.
            self._client = OpenAI(api_key=key or "not-needed", base_url=self.base_url)
        return self._client


# --- registry + selection ---


_PROVIDERS: dict[str, type] = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
}


# OpenAI-compatible endpoints served through OpenAICompatibleProvider.
# (base_url, api_key_env, default_model). default_model is None where there's
# no single obvious choice — pass model=... for those. base_urls are the
# providers' documented OpenAI-compatible roots; the SDK appends
# /chat/completions. Bring any other endpoint with
# get_provider("openai-compatible", base_url=..., model=...).
_OPENAI_COMPATIBLE_PRESETS: dict[str, tuple[str, str, str | None]] = {
    "deepseek":   ("https://api.deepseek.com",              "DEEPSEEK_API_KEY",   DEEPSEEK_DEFAULT_MODEL),
    "ollama":     ("http://localhost:11434/v1",             "",                   None),
    "groq":       ("https://api.groq.com/openai/v1",        "GROQ_API_KEY",       None),
    "together":   ("https://api.together.xyz/v1",           "TOGETHER_API_KEY",   None),
    "openrouter": ("https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY", None),
    "fireworks":  ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY",  None),
    "mistral":    ("https://api.mistral.ai/v1",             "MISTRAL_API_KEY",    None),
}

# Names that mean "I'll supply base_url + model myself".
_OPENAI_COMPATIBLE_ALIASES = ("openai-compatible", "custom")


def default_provider_name() -> str:
    """Return the configured default provider name.

    Reads `LLM_PROVIDER` env var with `claude` fallback. Other modules
    import this helper so the literal `claude` string lives only here.
    Set `LLM_PROVIDER=deepseek` (+ `DEEPSEEK_API_KEY`) to default to DeepSeek.
    """
    return os.environ.get("LLM_PROVIDER", "claude")


def get_provider(name: str | None = None, **kwargs) -> Provider:
    """Construct a provider by name.

    Built-ins: ``claude``, ``gemini``, ``openai``. OpenAI-compatible presets:
    ``deepseek``, ``ollama``, ``groq``, ``together``, ``openrouter``,
    ``fireworks``, ``mistral`` (each fills in base_url + key env; pass
    ``model=`` where the preset has no default). Anything else OpenAI-shaped:
    ``get_provider("openai-compatible", base_url=..., model=...)``. Extra
    kwargs flow to the constructor.
    """
    if name is None:
        name = default_provider_name()
    if name in _PROVIDERS:
        return _PROVIDERS[name](**kwargs)
    if name in _OPENAI_COMPATIBLE_PRESETS:
        base_url, api_key_env, default_model = _OPENAI_COMPATIBLE_PRESETS[name]
        kwargs.setdefault("base_url", base_url)
        kwargs.setdefault("api_key_env", api_key_env)
        kwargs.setdefault("name", name)
        if default_model is not None:
            kwargs.setdefault("model", default_model)
        return OpenAICompatibleProvider(**kwargs)
    if name in _OPENAI_COMPATIBLE_ALIASES:
        return OpenAICompatibleProvider(**kwargs)  # caller supplies base_url + model
    known = sorted({*_PROVIDERS, *_OPENAI_COMPATIBLE_PRESETS, *_OPENAI_COMPATIBLE_ALIASES})
    raise ValueError(f"unknown provider: {name!r}; known: {known}")
