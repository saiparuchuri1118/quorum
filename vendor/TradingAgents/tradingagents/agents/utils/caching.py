"""Anthropic prompt caching for the debate agents.

Why here and not everywhere
---------------------------
Caching is a *prefix* match: the cache key is the exact bytes up to a
``cache_control`` breakpoint, and any change before that point invalidates
everything after it. That makes it worth applying exactly where a large block
of bytes is re-sent verbatim across several calls.

In this pipeline that is the debate layer. Every researcher and every risk
analyst is handed all four analyst reports in full -- roughly 13.5k tokens on a
typical run -- and then a short, changing tail of conversation history. With one
debate round and one risk round that block is re-sent 5 times; with two rounds
each, 10 times. It is the single largest repeated payload in the run.

The prompts were already ordered correctly for this (reports first, mutable
history last), so the only change needed is splitting each prompt at the
existing stable/volatile boundary and marking the first half.

Deliberately NOT applied to the analysts
----------------------------------------
Their system prompts are ~2k tokens, and Claude Haiku 4.5 -- the default quick
model -- has a 4096-token minimum cacheable prefix. A breakpoint there would
silently never cache: no error, just ``cache_creation_input_tokens: 0``. The
analyst tool loop does accumulate a cacheable prefix as tool results pile up,
but marking it means reaching into the ChatPromptTemplate + bind_tools path, so
it is left as a separate change.

Minimum cacheable prefix is model-dependent and NOT monotonic across
generations: 512 tokens on Opus 5 / Fable 5, 1024 on Sonnet 5 / Opus 4.8,
2048 on Opus 4.7, 4096 on Opus 4.6 / Opus 4.5 / Haiku 4.5.

Provider safety
---------------
``cache_control`` is Anthropic-specific. Every other provider in the registry
would either reject the content block or silently pass an unknown key through to
its API, so the helper degrades to a plain string unless the bound client is
actually an Anthropic one.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

# Rough chars-per-token for English prose. Only used to warn when a prefix looks
# too small to ever cache -- the real tokenizer is not worth importing for a
# diagnostic.
_CHARS_PER_TOKEN = 4

# Per-model minimum cacheable prefix, in tokens. A marker on anything shorter is
# silently ignored by the API.
_MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-opus-4-7": 2048,
    "claude-haiku-3-5": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
}
_DEFAULT_MIN_CACHEABLE = 1024


def supports_cache_control(llm: Any) -> bool:
    """Whether this client speaks Anthropic's ``cache_control`` content blocks.

    Checked by walking the MRO rather than importing ChatAnthropic at module
    scope, so the core package keeps working when the Anthropic extra is absent.
    """
    return any(
        base.__name__ in ("ChatAnthropic", "ChatAnthropicMessages")
        for base in type(llm).__mro__
    )


def _min_cacheable_tokens(llm: Any) -> int:
    model = str(getattr(llm, "model", "") or getattr(llm, "model_name", ""))
    for name, minimum in _MIN_CACHEABLE_TOKENS.items():
        if model.startswith(name):
            return minimum
    return _DEFAULT_MIN_CACHEABLE


def cached_prompt(llm: Any, stable: str, volatile: str, *, label: str = "") -> list[BaseMessage]:
    """Build a message list with a cache breakpoint between the two halves.

    ``stable`` must be byte-identical across the calls that should share the
    cache -- it is everything up to and including the analyst reports. ``volatile``
    is the changing tail (conversation history, the other side's latest argument).

    Falls back to a single plain-text message for non-Anthropic providers, and
    warns rather than silently no-op'ing when the stable half is too short to
    reach this model's minimum.
    """
    if not supports_cache_control(llm):
        return [HumanMessage(content=stable + volatile)]

    approx_tokens = len(stable) // _CHARS_PER_TOKEN
    minimum = _min_cacheable_tokens(llm)
    if approx_tokens < minimum:
        # Cheaper to send one plain block than to pay a write premium on a
        # prefix the API will refuse to store.
        logger.debug(
            "%s: cacheable prefix ~%d tokens is below the %d-token minimum for %s; "
            "sending uncached",
            label or "cached_prompt", approx_tokens, minimum,
            getattr(llm, "model", "?"),
        )
        return [HumanMessage(content=stable + volatile)]

    return [
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": stable,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": volatile},
            ]
        )
    ]


def cache_usage(response: Any) -> dict[str, int]:
    """Extract cache counters from a response, for logging and tests.

    ``input_tokens`` is only the *uncached remainder* -- total prompt size is
    the sum of all three, which is why a run can look suspiciously cheap.
    """
    usage = getattr(response, "usage_metadata", None) or {}
    details = usage.get("input_token_details", {}) or {}
    return {
        "input": usage.get("input_tokens", 0) or 0,
        "cache_read": details.get("cache_read", 0) or 0,
        "cache_write": details.get("cache_creation", 0) or 0,
    }
