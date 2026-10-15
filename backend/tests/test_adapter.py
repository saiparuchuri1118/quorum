"""Adapter translation tests.

These feed the adapter LangGraph-shaped payloads directly, so the full
translation path is covered without a graph, a provider key, or a paid run.
The shapes here mirror what ``stream_mode=["updates","messages"]`` actually
yields; if upstream changes those, these tests are where it should surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quorum.adapter import GraphAdapter, _between, _float_after, _text_of  # noqa: E402
from quorum.events import EventStream  # noqa: E402


class Collector:
    """Sink that keeps every event for assertion."""

    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)

    def types(self):
        return [e.type for e in self.events]

    def of(self, type_):
        return [e for e in self.events if e.type == type_]


@pytest.fixture()
def rig():
    stream = EventStream(clock=lambda: 0.0)
    sink = Collector()
    stream.add_sink(sink)
    return GraphAdapter(stream, debate_rounds=2, risk_rounds=2), sink


def ai(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def tool_msg(call_id, name, content):
    return SimpleNamespace(content=content, tool_call_id=call_id, name=name)


# -- node lifecycle ----------------------------------------------------------


def test_node_entered_emitted_once_across_tool_loop(rig):
    adapter, sink = rig
    for _ in range(3):
        adapter.on_update("Market Analyst", {"messages": [ai("thinking")]})
    assert len(sink.of("node_entered")) == 1
    assert sink.of("node_entered")[0].payload["node"] == "market"


def test_node_exits_only_when_its_report_lands(rig):
    adapter, sink = rig
    adapter.on_update("Market Analyst", {"messages": [ai("still working")]})
    assert not sink.of("node_exited")
    adapter.on_update("Market Analyst", {"market_report": "## Technical picture\nbody"})
    assert [e.payload["node"] for e in sink.of("node_exited")] == ["market"]
    assert sink.of("report_section")[0].payload["key"] == "market_report"


def test_handoff_between_agents_emits_edge_flow(rig):
    adapter, sink = rig
    adapter.on_update("Market Analyst", {"market_report": "x"})
    adapter.on_update("News Analyst", {"messages": [ai()]})
    flows = [(e.payload["src"], e.payload["dst"]) for e in sink.of("edge_flow")]
    assert ("market", "news") in flows


def test_plumbing_nodes_are_not_participants(rig):
    adapter, sink = rig
    adapter.on_update("Msg Clear Market", {"messages": []})
    assert not sink.of("node_entered")


# -- tools -------------------------------------------------------------------


def test_tool_call_and_result_are_paired_by_id(rig):
    adapter, sink = rig
    adapter.on_update("Market Analyst", {
        "messages": [ai(tool_calls=[
            {"id": "c1", "name": "get_stock_data", "args": {"symbol": "NVDA"}}
        ])]
    })
    adapter.on_update("tools_market", {
        "messages": [tool_msg("c1", "get_stock_data", "Date,Open,Close\n...")]
    })
    call = sink.of("tool_call")[0].payload
    res = sink.of("tool_result")[0].payload
    assert call["call_id"] == res["call_id"] == "c1"
    assert call["node"] == "market"
    assert res["ok"] is True


def test_duplicate_tool_call_ids_are_not_re_emitted(rig):
    adapter, sink = rig
    msg = ai(tool_calls=[{"id": "c1", "name": "get_stock_data", "args": {}}])
    adapter.on_update("Market Analyst", {"messages": [msg]})
    adapter.on_update("Market Analyst", {"messages": [msg]})
    assert len(sink.of("tool_call")) == 1


def test_no_data_sentinel_marks_result_not_ok(rig):
    """The routing layer returns prose sentinels rather than raising, so a
    failed lookup is a string prefix — not an exception the adapter can catch."""
    adapter, sink = rig
    adapter.on_update("tools_market", {
        "messages": [tool_msg("c9", "get_stock_data",
                              "NO_DATA_AVAILABLE: No usable market data for 'ZZZZ'")]
    })
    assert sink.of("tool_result")[0].payload["ok"] is False


# -- debate ------------------------------------------------------------------


def test_debate_turns_derive_side_and_round_from_count(rig):
    adapter, sink = rig
    for count, speaker in ((1, "Bull"), (2, "Bear"), (3, "Bull"), (4, "Bear")):
        adapter.on_update("Bull Researcher" if speaker == "Bull" else "Bear Researcher", {
            "investment_debate_state": {
                "count": count, "current_response": f"{speaker} Analyst: argument {count}"
            }
        })
    turns = [(e.payload["side"], e.payload["round"]) for e in sink.of("debate_turn")]
    # Two speakers per round.
    assert turns == [("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2)]


def test_debate_turn_emits_rebuttal_edge(rig):
    adapter, sink = rig
    adapter.on_update("Bull Researcher", {
        "investment_debate_state": {"count": 1, "current_response": "Bull Analyst: x"}
    })
    reb = [e for e in sink.of("edge_flow") if e.payload["kind"] == "rebuttal"]
    assert (reb[0].payload["src"], reb[0].payload["dst"]) == ("bull", "bear")


def test_stale_debate_state_does_not_double_count(rig):
    """Every superstep echoes the debate state; only a rising count is a turn."""
    adapter, sink = rig
    state = {"count": 1, "current_response": "Bull Analyst: x"}
    adapter.on_update("Bull Researcher", {"investment_debate_state": state})
    adapter.on_update("Bull Researcher", {"investment_debate_state": state})
    assert len(sink.of("debate_turn")) == 1


def test_risk_turns_use_three_speakers_per_round(rig):
    adapter, sink = rig
    seats = [("Aggressive", "aggressive"), ("Conservative", "conservative"), ("Neutral", "neutral")]
    for i in range(6):
        speaker, sid = seats[i % 3]
        adapter.on_update(f"{speaker} Analyst", {
            "risk_debate_state": {
                "count": i + 1, "latest_speaker": speaker,
                f"current_{sid}_response": f"{speaker} Analyst: point {i}",
            }
        })
    rounds = [(e.payload["side"], e.payload["round"]) for e in sink.of("risk_turn")]
    assert rounds[0] == ("aggressive", 1)
    assert rounds[3] == ("aggressive", 2)


# -- ratings and the final decision ------------------------------------------


def test_research_manager_report_carries_a_rating(rig):
    adapter, sink = rig
    adapter.on_update("Research Manager", {
        "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: because."
    })
    assert sink.of("report_section")[0].payload["rating"] == "Overweight"


def test_portfolio_manager_emits_a_parsed_decision(rig):
    adapter, sink = rig
    body = (
        "**Rating**: Buy\n\n"
        "**Executive Summary**: Enter at market.\n\n"
        "**Investment Thesis**: Growth is intact.\n\n"
        "**Price Target**: 210.0\n\n"
        "**Time Horizon**: 2-3 quarters"
    )
    adapter.on_update("Portfolio Manager", {"final_trade_decision": body})
    d = sink.of("decision")[0].payload
    assert d["rating"] == "Buy"
    assert d["executive_summary"] == "Enter at market."
    assert d["price_target"] == 210.0
    assert d["time_horizon"] == "2-3 quarters"
    assert sink.of("edge_flow")[-1].payload["dst"] == "verdict"


# -- token streaming ---------------------------------------------------------


def test_message_chunks_become_token_deltas(rig):
    adapter, sink = rig
    adapter.on_message(ai("Price closed at "), {"langgraph_node": "Market Analyst"})
    adapter.on_message(ai("$183.42."), {"langgraph_node": "Market Analyst"})
    deltas = [e.payload["text"] for e in sink.of("token_delta")]
    assert "".join(deltas) == "Price closed at $183.42."
    assert sink.of("node_entered")[0].payload["node"] == "market"


def test_messages_from_unmapped_nodes_are_ignored(rig):
    adapter, sink = rig
    adapter.on_message(ai("noise"), {"langgraph_node": "tools_market"})
    assert not sink.of("token_delta")


# -- content normalisation ---------------------------------------------------


def test_text_of_handles_provider_block_lists():
    """Reasoning models return typed blocks; only text blocks are report content."""
    assert _text_of("plain") == "plain"
    assert _text_of([
        {"type": "reasoning", "summary": "hidden"},
        {"type": "text", "text": "visible"},
    ]) == "visible"
    assert _text_of(None) == ""


def test_section_extraction_degrades_instead_of_raising():
    assert _between("no markers here", "**X**:", None) == ""
    assert _float_after("**Price Target**: not-a-number", "**Price Target**:") is None
