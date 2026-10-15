"""Adapter: a live TradingAgents run -> the Quorum event stream.

This is the piece that makes a session real. It subscribes to LangGraph's
stream and translates supersteps into the typed events the frontend consumes,
so a recorded live run is byte-compatible with a synthetic one and every
consumer downstream is unchanged.

Stream modes
------------
We ask for ``["updates", "messages"]`` rather than the ``values`` mode the
upstream CLI uses:

``updates``   yields ``{node_name: state_delta}`` -- it says *which node*
              produced *what*, so nothing has to be inferred by diffing
              consecutive full-state snapshots.
``messages``  yields ``(chunk, metadata)`` with ``metadata["langgraph_node"]``,
              which is the only way to get token-level deltas attributed to the
              node that emitted them. That is what drives the typewriter.

Node identity
-------------
LangGraph node names are the human labels ("Market Analyst"); our topology uses
wire keys ("market"). ``NODE_IDS`` maps between them. Non-agent nodes (the
``tools_*`` executors and the ``Msg Clear *`` scrubbers) are intentionally not
mapped -- they are plumbing, not participants, and shouldn't appear in the scene.

Known gaps
----------
* Vendor attribution on a tool call is *resolved*, not *observed*: we ask the
  routing layer which vendor it would pick. If the chain falls back mid-call,
  the event names the primary rather than the vendor that actually served it.
* Analysts run a tool loop, so a node is entered once but its LLM is invoked
  several times. ``node_exited`` is deferred until the node's report key is
  populated, which is the point it actually stops looping.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from .events import EventStream
from .topology import ANALYSTS, build_topology

logger = logging.getLogger(__name__)

# LangGraph node label -> our topology id.
NODE_IDS: dict[str, str] = {
    **{label: key for key, (label, _) in ANALYSTS.items()},
    "Bull Researcher": "bull",
    "Bear Researcher": "bear",
    "Research Manager": "research_manager",
    "Trader": "trader",
    "Aggressive Analyst": "aggressive",
    "Conservative Analyst": "conservative",
    "Neutral Analyst": "neutral",
    "Portfolio Manager": "portfolio_manager",
}

# State key that holds each node's finished report.
REPORT_KEYS: dict[str, str] = {
    **{key: report for key, (_, report) in ANALYSTS.items()},
    "research_manager": "investment_plan",
    "trader": "trader_investment_plan",
    "portfolio_manager": "final_trade_decision",
}

RISK_SPEAKER_IDS = {
    "Aggressive": "aggressive",
    "Conservative": "conservative",
    "Neutral": "neutral",
}


def _resolve_vendor(tool_name: str) -> str:
    """Which vendor the routing layer would serve this tool from.

    Resolved rather than observed -- see the module docstring. Falls back to
    "unknown" so a tool we don't recognise still animates instead of vanishing.
    """
    try:
        from tradingagents.dataflows.interface import get_category_for_method, get_vendor

        category = get_category_for_method(tool_name)
        vendor = get_vendor(category, tool_name)
        # A comma-separated chain resolves to its primary; "default" means the
        # category's first available vendor.
        first = vendor.split(",")[0].strip()
        return "yfinance" if first == "default" else first
    except Exception:
        return "unknown"


def _text_of(content: Any) -> str:
    """Normalise LangChain message content to a plain string.

    Providers return either a string or a list of typed blocks; we only want the
    text ones. Mirrors ``llm_clients.base_client.normalize_content``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


class GraphAdapter:
    """Consumes a LangGraph stream and emits Quorum events."""

    def __init__(self, stream: EventStream, *, debate_rounds: int, risk_rounds: int):
        self.s = stream
        self.debate_rounds = max(1, debate_rounds)
        self.risk_rounds = max(1, risk_rounds)

        self.entered: set[str] = set()
        self.exited: set[str] = set()
        self.last_agent: str | None = None
        self.seen_calls: set[str] = set()
        self.call_nodes: dict[str, str] = {}
        self.debate_count = 0
        self.risk_count = 0
        self.tool_calls = 0
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    # -- lifecycle ----------------------------------------------------------

    def _enter(self, node_id: str) -> None:
        if node_id in self.entered:
            return
        self.entered.add(node_id)
        # Derive the handoff from whichever agent ran last; the graph's edges
        # are implicit in execution order.
        if self.last_agent and self.last_agent != node_id:
            kind = "verdict" if self.last_agent in ("research_manager", "portfolio_manager") else "state"
            self.s.edge_flow(src=self.last_agent, dst=node_id, kind=kind)
        self.s.node_entered(node=node_id)
        self.last_agent = node_id

    def _exit(self, node_id: str) -> None:
        if node_id in self.exited:
            return
        self.exited.add(node_id)
        self.s.node_exited(node=node_id, state="complete")

    # -- stream handlers ----------------------------------------------------

    def on_message(self, chunk: Any, meta: dict) -> None:
        """A token (or tool-call fragment) from a specific node."""
        node_id = NODE_IDS.get(meta.get("langgraph_node", ""))
        if not node_id:
            return
        self._enter(node_id)

        text = _text_of(getattr(chunk, "content", None))
        if text:
            self.s.token_delta(node=node_id, text=text)
            self.tokens_out += max(1, len(text) // 4)

        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            self.tokens_in += usage.get("input_tokens", 0) or 0

    def on_update(self, node_name: str, delta: dict) -> None:
        """A node finished a superstep. ``delta`` is what it wrote to state."""
        if not isinstance(delta, dict):
            return

        # Tool executor nodes carry ToolMessages; attribute them to the caller.
        if node_name.startswith("tools_"):
            self._handle_tool_results(delta)
            return
        if node_name.startswith("Msg Clear"):
            return

        node_id = NODE_IDS.get(node_name)
        if not node_id:
            return
        self._enter(node_id)
        self._handle_tool_calls(node_id, delta)

        # Debate / risk turns land as state, not as a report.
        if "investment_debate_state" in delta:
            self._handle_debate(delta["investment_debate_state"])
        if "risk_debate_state" in delta:
            self._handle_risk(delta["risk_debate_state"])

        report_key = REPORT_KEYS.get(node_id)
        if report_key and delta.get(report_key):
            self._emit_report(node_id, report_key, delta[report_key])
            self._exit(node_id)

    # -- pieces -------------------------------------------------------------

    def _handle_tool_calls(self, node_id: str, delta: dict) -> None:
        for msg in delta.get("messages", []) or []:
            for call in getattr(msg, "tool_calls", None) or []:
                call_id = call.get("id") or f"{node_id}-{self.tool_calls}"
                if call_id in self.seen_calls:
                    continue
                self.seen_calls.add(call_id)
                self.call_nodes[call_id] = node_id
                tool = call.get("name", "unknown")
                self.tool_calls += 1
                self.s.tool_call(
                    node=node_id, call_id=call_id, tool=tool,
                    vendor=_resolve_vendor(tool), args=call.get("args", {}) or {},
                )

    def _handle_tool_results(self, delta: dict) -> None:
        for msg in delta.get("messages", []) or []:
            call_id = getattr(msg, "tool_call_id", None)
            if not call_id:
                continue
            body = _text_of(getattr(msg, "content", ""))
            tool = getattr(msg, "name", "") or "unknown"
            # The routing layer returns prose sentinels rather than raising, so
            # "no data" is a string prefix, not an exception.
            ok = not body.startswith(("NO_DATA_AVAILABLE", "DATA_UNAVAILABLE", "Error"))
            self.s.tool_result(
                call_id=call_id, tool=tool, ok=ok,
                vendor=_resolve_vendor(tool), size=len(body), preview=body[:280],
            )

    def _handle_debate(self, state: dict) -> None:
        count = state.get("count", 0)
        if count <= self.debate_count:
            return
        self.debate_count = count
        response = state.get("current_response", "") or ""
        side = "bull" if response.startswith("Bull") else "bear"
        # Two speakers per round.
        self.s.debate_turn(
            side=side, round_=math.ceil(count / 2),
            text=response.split(":", 1)[-1].strip()[:4000],
        )
        self.s.edge_flow(src=side, dst="bear" if side == "bull" else "bull", kind="rebuttal")

    def _handle_risk(self, state: dict) -> None:
        count = state.get("count", 0)
        if count <= self.risk_count:
            return
        self.risk_count = count
        speaker = state.get("latest_speaker", "") or ""
        side = RISK_SPEAKER_IDS.get(speaker.split()[0] if speaker else "", "aggressive")
        text = state.get(f"current_{side}_response", "") or ""
        # Three speakers per round.
        self.s.risk_turn(
            side=side, round_=math.ceil(count / 3),
            text=text.split(":", 1)[-1].strip()[:4000],
        )

    def _emit_report(self, node_id: str, report_key: str, body: str) -> None:
        label = next((lbl for lbl, nid in NODE_IDS.items() if nid == node_id), node_id)
        rating = None
        if node_id in ("research_manager", "portfolio_manager"):
            try:
                from tradingagents.agents.utils.rating import parse_rating

                rating = parse_rating(body)
            except Exception:
                rating = None
        self.s.report_section(key=report_key, title=label, body=body, rating=rating)

        if node_id == "portfolio_manager":
            self.s.decision(
                rating=rating or "Hold",
                executive_summary=_between(body, "**Executive Summary**:", "**Investment Thesis**"),
                investment_thesis=_between(body, "**Investment Thesis**:", "**Price Target**"),
                price_target=_float_after(body, "**Price Target**:"),
                time_horizon=_between(body, "**Time Horizon**:", None) or None,
            )
            self.s.edge_flow(src="portfolio_manager", dst="verdict", kind="verdict")


def _between(text: str, start: str, end: str | None) -> str:
    """Pull a rendered section out of the PM's markdown.

    Depends on ``render_pm_decision``'s header shape, which is a hardcoded
    format upstream -- but degrades to "" rather than raising if it drifts.
    """
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i) if end else -1
    return text[i : j if j > 0 else len(text)].strip()


def _float_after(text: str, label: str) -> float | None:
    raw = _between(text, label, "\n")
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def run_live(
    stream: EventStream,
    *,
    ticker: str,
    trade_date: str,
    asset_type: str = "stock",
    analysts: tuple[str, ...] = ("market", "social", "news", "fundamentals"),
    config: dict | None = None,
) -> dict:
    """Execute a real TradingAgents run, emitting events as it goes.

    Returns the final graph state. Raises whatever the graph raises, after
    emitting ``run_error`` so a partial session file is still coherent.
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    keys = [k for k in analysts if k in ANALYSTS]
    if asset_type == "crypto":
        keys = [k for k in keys if k != "fundamentals"]

    ta = TradingAgentsGraph(tuple(keys), config=cfg, debug=False)

    stream.run_started(
        run_id=f"live-{ticker.lower()}-{trade_date}-{int(time.time())}",
        ticker=ticker,
        trade_date=trade_date,
        asset_type=asset_type,
        topology=build_topology(tuple(keys), asset_type=asset_type),
        provider=cfg["llm_provider"],
        deep_model=cfg["deep_think_llm"],
        quick_model=cfg["quick_think_llm"],
        debate_rounds=cfg["max_debate_rounds"],
        risk_rounds=cfg["max_risk_discuss_rounds"],
        synthetic=False,
    )

    adapter = GraphAdapter(
        stream,
        debate_rounds=cfg["max_debate_rounds"],
        risk_rounds=cfg["max_risk_discuss_rounds"],
    )

    init_state = ta.propagator.create_initial_state(
        ticker, trade_date, asset_type=asset_type,
        past_context=ta.memory_log.get_past_context(ticker),
        instrument_context=ta.resolve_instrument_context(ticker, asset_type),
    )
    args = ta.propagator.get_graph_args()
    args.pop("stream_mode", None)

    started = time.monotonic()
    final: dict = {}
    try:
        for mode, payload in ta.graph.stream(
            init_state, stream_mode=["updates", "messages"], **args
        ):
            if mode == "messages":
                chunk, meta = payload
                adapter.on_message(chunk, meta or {})
            elif mode == "updates":
                for node_name, delta in (payload or {}).items():
                    adapter.on_update(node_name, delta)
                    if isinstance(delta, dict):
                        final.update(delta)
    except Exception as exc:
        stream.run_error(node=adapter.last_agent, message=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        stream.run_finished(
            duration=time.monotonic() - started,
            llm_calls=len(adapter.entered),
            tool_calls=adapter.tool_calls,
            tokens_in=adapter.tokens_in,
            tokens_out=adapter.tokens_out,
        )
    return final
