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
from typing import Iterator, Protocol, TypedDict


__all__ = [
    "Provider",
    "Message",
    "Tool",
    "ClaudeProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "get_provider",
    "default_provider_name",
]


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
        model: str = "claude-sonnet-4-6",
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
                        "stop_reason": final.stop_reason,
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
            if isinstance(system, str):
                kwargs["system"] = system
            else:
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
        model: str = "gemini-2.5-pro",
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

        stream = client.models.generate_content_stream(
            model=self.model,
            contents=self._to_contents(messages),
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        usage_meta = None
        stop_reason = "end_turn"
        for chunk in stream:
            if getattr(chunk, "text", None):
                yield {"type": "text", "text": chunk.text}
            if getattr(chunk, "usage_metadata", None) is not None:
                usage_meta = chunk.usage_metadata
            cands = getattr(chunk, "candidates", None) or []
            for cand in cands:
                fr = getattr(cand, "finish_reason", None)
                if fr is not None:
                    stop_reason = str(fr).lower().split(".")[-1] or stop_reason

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
        model: str = "gpt-4o",
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

        # Track in-progress tool calls by index — OpenAI deltas reference
        # the same tool by index across chunks.
        tool_calls: dict[int, dict] = {}
        stop_reason = "end_turn"
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
                    if fn is not None:
                        if getattr(fn, "name", None):
                            tool_calls[idx]["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            tool_calls[idx]["input_json"] += fn.arguments
                    if getattr(tc, "id", None):
                        tool_calls[idx]["id"] = tc.id
                    # Emit start once we have id+name; emit input deltas as args stream.
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
                    if fn is not None and getattr(fn, "arguments", None):
                        yield {
                            "type": "tool_use_input",
                            "partial_json": fn.arguments,
                        }
            fr = getattr(choice, "finish_reason", None)
            if fr is not None:
                # Map OpenAI finish_reason → canonical stop_reason naming
                stop_reason = "tool_use" if fr == "tool_calls" else (
                    "end_turn" if fr == "stop" else fr
                )

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


# --- registry + selection ---


_PROVIDERS: dict[str, type] = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
}


def default_provider_name() -> str:
    """Return the configured default provider name.

    Reads `LLM_PROVIDER` env var with `claude` fallback. Other modules
    import this helper so the literal `claude` string lives only here.
    """
    return os.environ.get("LLM_PROVIDER", "claude")


def get_provider(name: str | None = None, **kwargs) -> Provider:
    if name is None:
        name = default_provider_name()
    if name not in _PROVIDERS:
        raise ValueError(f"unknown provider: {name!r}; known: {list(_PROVIDERS)}")
    return _PROVIDERS[name](**kwargs)
