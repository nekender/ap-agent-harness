"""Two model providers, each through its own SDK, behind one small interface.

The conversation is stored in one provider-neutral ("canonical") shape:

    {"role": "user", "text": "post invoice-01"}
    {"role": "assistant", "text": "...", "tool_calls": [{"id": ..., "name": ..., "args": {...}}],
     "claude_blocks": [...],                    # only on Claude replies: its content, verbatim
     "meta": {"model": ..., "usage": {...}}}    # which model answered, and its token usage
    {"role": "tool", "tool_call_id": ..., "name": ..., "result": {...}}

Each provider converts that whole history into its own format on every call and turns its
reply back into one canonical assistant message. That's all switching providers needs:
the history never belongs to a provider.
"""

from __future__ import annotations

import json
import os
import threading
import uuid

RETRIES = 4
MAX_WAIT = 30.0  # seconds


class ProviderError(Exception):
    pass


class Cancelled(Exception):
    pass


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _with_retries(call, sdk, cancel: threading.Event, notify):
    """Run call(), retrying rate limits, overloads and network errors with backoff.

    Both SDKs share the same error classes, so `sdk` is the anthropic or openai module.
    The SDKs' own retries are off so that a stop interrupts the waiting.
    """
    for attempt in range(RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            if isinstance(exc, sdk.RateLimitError):
                wait = float(exc.response.headers.get("retry-after") or 0)
            elif isinstance(exc, (sdk.InternalServerError, sdk.APIConnectionError)):
                wait = 0.0  # 5xx (incl. Anthropic's 529 overloaded), network errors, timeouts
            else:
                raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
            if attempt == RETRIES:
                raise ProviderError(f"{type(exc).__name__} after {RETRIES + 1} attempts: {exc}") from exc
            wait = min(max(wait, 2.0 ** (attempt + 1)), MAX_WAIT)
            notify(f"{type(exc).__name__}; retrying in {wait:.0f}s (attempt {attempt + 2}/{RETRIES + 1})")
            if cancel.wait(wait):
                raise Cancelled() from exc


# -- OpenAI (Chat Completions) ----------------------------------------------------------------


class OpenAIProvider:
    name = "openai"

    def __init__(self):
        import openai

        self._sdk = openai
        self.model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
        self.client = openai.OpenAI(timeout=120, max_retries=0)

    @staticmethod
    def to_messages(history: list[dict], system: str) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for msg in history:
            if msg["role"] == "user":
                out.append({"role": "user", "content": msg["text"]})
            elif msg["role"] == "assistant":
                item = {"role": "assistant", "content": msg.get("text") or None}
                if msg.get("tool_calls"):
                    item["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                        }
                        for call in msg["tool_calls"]
                    ]
                elif not item["content"]:
                    continue  # an empty reply carries nothing
                out.append(item)
            elif msg["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": json.dumps(msg["result"])})
        return out

    def complete(self, history, system, tools, cancel, notify) -> dict:
        kwargs = dict(
            model=self.model,
            messages=self.to_messages(history, system),
            tools=[{"type": "function", "function": tool} for tool in tools],
        )
        response = _with_retries(lambda: self.client.chat.completions.create(**kwargs), self._sdk, cancel, notify)
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError("the reply was cut off (output token limit)")
        message = choice.message
        calls = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": call.function.arguments}
            calls.append({"id": call.id or _new_call_id(), "name": call.function.name, "args": args})
        usage = response.usage
        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0 if usage.prompt_tokens_details else 0
        meta = {
            "model": response.model,
            "usage": {
                "input_tokens": usage.prompt_tokens - cached,
                "cache_read_input_tokens": cached,
                "output_tokens": usage.completion_tokens,
            },
        }
        return {"role": "assistant", "text": message.content or "", "tool_calls": calls, "meta": meta}


# -- Claude (Anthropic Messages API) ------------------------------------------------------------


class ClaudeProvider:
    """Claude replies carry thinking blocks that must go back to Claude unchanged, so each
    Claude reply keeps its content blocks verbatim in "claude_blocks". Replies from the other
    provider are rebuilt as plain text + tool_use blocks. History is append-only and the
    system prompt and tools never change, so earlier thinking stays valid across switches.
    """

    name = "claude"

    def __init__(self):
        import anthropic

        self._sdk = anthropic
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
        self.client = anthropic.Anthropic(timeout=180, max_retries=0)

    @staticmethod
    def to_messages(history: list[dict]) -> list[dict]:
        out: list[dict] = []

        def add_user_block(block):
            # Tool results and any text after them share one user turn, results first.
            if out and out[-1]["role"] == "user":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})

        for msg in history:
            if msg["role"] == "user":
                add_user_block({"type": "text", "text": msg["text"]})
            elif msg["role"] == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": json.dumps(msg["result"]),
                }
                if "error" in msg["result"]:
                    block["is_error"] = True
                add_user_block(block)
            elif msg["role"] == "assistant":
                blocks = msg.get("claude_blocks")
                if blocks is None:  # written by another provider
                    blocks = [{"type": "text", "text": msg["text"]}] if msg.get("text") else []
                    blocks += [
                        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                        for c in msg.get("tool_calls", [])
                    ]
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
        return out

    @staticmethod
    def _block_param(block) -> dict | None:
        """A response block as the request param that replays it unchanged."""
        if block.type == "thinking":
            return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
        if block.type == "redacted_thinking":
            return {"type": "redacted_thinking", "data": block.data}
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        return None  # e.g. a "fallback" marker block: an audit marker, safe to drop

    def complete(self, history, system, tools, cancel, notify) -> dict:
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=self.to_messages(history),
            tools=[
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ],
            output_config={"effort": "medium"},
            # If a safety classifier declines, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        response = _with_retries(lambda: self.client.beta.messages.create(**kwargs), self._sdk, cancel, notify)
        if response.stop_reason == "refusal":
            raise ProviderError(f"Claude declined the request ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise ProviderError("the reply was cut off (output token limit)")

        blocks = [p for p in map(self._block_param, response.content) if p]
        return {
            "role": "assistant",
            "text": "".join(b["text"] for b in blocks if b["type"] == "text"),
            "tool_calls": [
                {"id": b["id"], "name": b["name"], "args": dict(b["input"])} for b in blocks if b["type"] == "tool_use"
            ],
            "claude_blocks": blocks,
            "meta": {
                "model": response.model,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
                    "cache_creation_input_tokens": response.usage.cache_creation_input_tokens or 0,
                    "output_tokens": response.usage.output_tokens,
                },
            },
        }


PROVIDERS = {"claude": ClaudeProvider, "openai": OpenAIProvider}
API_KEYS = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
