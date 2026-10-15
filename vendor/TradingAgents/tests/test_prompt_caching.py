"""Coverage for tradingagents.agents.utils.caching.

Focus: the two failure modes that would be silent otherwise --
(1) a non-Anthropic client getting an Anthropic-only content-block shape,
(2) a stable prefix too short for the bound model's cache minimum.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.caching import (
    cache_usage,
    cached_prompt,
    supports_cache_control,
)


class _FakeChatAnthropic:
    """Stand-in with the same class name ChatAnthropic would have, without
    importing the real (optional) dependency."""
    model = "claude-sonnet-5"


_FakeChatAnthropic.__name__ = "ChatAnthropic"


class _FakeChatOpenAI:
    """A real class (not a MagicMock) so __mro__ walking behaves like it does
    on the actual langchain_openai.ChatOpenAI -- MagicMock's __class__.__mro__
    is read-only and can't be reassigned to simulate a different provider."""

    def __init__(self, model="gpt-5.4-mini"):
        self.model_name = model


def _openai_like(model="gpt-5.4-mini"):
    return _FakeChatOpenAI(model)


def test_supports_cache_control_true_for_anthropic_mro():
    assert supports_cache_control(_FakeChatAnthropic()) is True


def test_supports_cache_control_false_for_other_providers():
    assert supports_cache_control(_openai_like()) is False


def test_non_anthropic_gets_plain_string_never_a_content_block_list():
    msgs = cached_prompt(_openai_like(), "STABLE " * 2000, "volatile")
    assert isinstance(msgs[0].content, str)
    assert "cache_control" not in msgs[0].content


def test_anthropic_long_enough_prefix_gets_split_with_breakpoint():
    llm = _FakeChatAnthropic()
    llm.model = "claude-sonnet-5"  # 1024-token minimum
    stable = "x" * 4096 * 2  # ~2048 tokens, well over the 1024 minimum
    msgs = cached_prompt(llm, stable, "the changing tail", label="test")
    content = msgs[0].content
    assert isinstance(content, list)
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[0]["text"] == stable
    assert content[1]["text"] == "the changing tail"
    assert "cache_control" not in content[1]


def test_short_prefix_below_model_minimum_falls_back_to_plain():
    """Haiku 4.5's minimum is 4096 tokens; a short report set must not emit a
    marker that the API would silently ignore anyway."""
    llm = _FakeChatAnthropic()
    llm.model = "claude-haiku-4-5"
    short_stable = "short report " * 20  # well under 4096 tokens
    msgs = cached_prompt(llm, short_stable, "tail", label="test")
    assert isinstance(msgs[0].content, str)


def test_stable_half_is_byte_identical_across_calls():
    """The whole scheme depends on this: two calls with the same stable half
    must produce the same first block, regardless of the volatile half."""
    llm = _FakeChatAnthropic()
    stable = "REPORT BLOCK " * 500
    m1 = cached_prompt(llm, stable, "round 1 history")
    m2 = cached_prompt(llm, stable, "round 2 history, longer now")
    assert m1[0].content[0]["text"] == m2[0].content[0]["text"]
    assert m1[0].content[1]["text"] != m2[0].content[1]["text"]


def test_cache_usage_reads_nested_input_token_details():
    response = MagicMock()
    response.usage_metadata = {
        "input_tokens": 340,
        "input_token_details": {"cache_read": 13500, "cache_creation": 0},
    }
    assert cache_usage(response) == {"input": 340, "cache_read": 13500, "cache_write": 0}


def test_cache_usage_degrades_to_zeros_without_usage_metadata():
    response = MagicMock(spec=[])
    assert cache_usage(response) == {"input": 0, "cache_read": 0, "cache_write": 0}


@pytest.mark.parametrize(
    "model,tokens,should_cache",
    [
        ("claude-opus-5", 600, True),        # 512 min
        ("claude-sonnet-5", 600, False),     # 1024 min
        ("claude-sonnet-5", 1200, True),
        ("claude-haiku-4-5", 3500, False),   # 4096 min
        ("claude-haiku-4-5", 4200, True),
        ("claude-unknown-future-model", 1200, True),  # default 1024
    ],
)
def test_per_model_minimums_match_the_documented_table(model, tokens, should_cache):
    llm = _FakeChatAnthropic()
    llm.model = model
    stable = "x" * (tokens * 4)  # ~4 chars/token
    msgs = cached_prompt(llm, stable, "tail")
    is_split = isinstance(msgs[0].content, list)
    assert is_split == should_cache, f"{model} @ ~{tokens}tok: expected cache={should_cache}"
