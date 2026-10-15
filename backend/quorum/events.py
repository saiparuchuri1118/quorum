"""The wire contract between the agent runtime and the 3D frontend.

Everything the frontend renders comes from this event stream and nothing else.
A live run streams these over a WebSocket; a recorded run replays the identical
sequence from a JSONL file. That symmetry is the whole design:

  - The demo never depends on API keys, network, or LLM latency.
  - Timeline scrubbing is free: seek = replay events up to seq N.
  - Frontend development costs zero tokens.
  - A regression in the scene is reproducible from a committed session file.

Envelope
--------
Every event is a JSON object with four keys::

    {"seq": 12, "t": 4.317, "type": "tool_call", "payload": {...}}

``seq``  monotonic, gap-free, starts at 0. The frontend's ordering authority.
``t``    seconds since ``run_started``, float. Drives replay pacing.
``type`` one of EVENT_TYPES.
``payload`` type-specific, documented per constructor below.

Adding an event type
--------------------
Add a constructor here, add the name to EVENT_TYPES, and mirror the payload in
web/events.js. Never emit a raw dict from the adapter -- go through a
constructor so a typo becomes an error here rather than a silently dead node in
the scene.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

EVENT_TYPES = (
    "run_started",      # graph topology + run parameters; always seq 0
    "node_entered",     # an agent node began work
    "node_exited",      # an agent node finished (or errored)
    "edge_flow",        # state propagated along an edge -> particle animation
    "tool_call",        # an agent invoked a data tool
    "tool_result",      # that tool returned (or failed)
    "token_delta",      # incremental text from a node -> typewriter effect
    "debate_turn",      # one bull/bear exchange in the research debate
    "risk_turn",        # one aggressive/conservative/neutral risk exchange
    "report_section",   # a completed report body became available
    "decision",         # the Portfolio Manager's final structured decision
    "run_finished",     # totals; always the last event
    "run_error",        # unrecoverable failure; terminal
)

# Node lifecycle states the frontend maps to materials.
NODE_STATES = ("pending", "active", "complete", "error")

# Which visual group a node belongs to. Drives layout and palette.
GROUPS = ("analysts", "research", "trading", "risk", "portfolio")


@dataclass(frozen=True)
class Event:
    seq: int
    t: float
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class EventStream:
    """Assigns seq/t, validates the type, and fans out to sinks.

    Sinks are callables taking one ``Event``. The recorder appends to JSONL; the
    API server pushes to connected sockets. A run can have both, which is how a
    live demo also produces a replayable artifact.

    ``clock`` is injectable so synthetic runs can fabricate a timeline without
    actually sleeping -- a 6-minute demo generates in milliseconds.
    """

    def __init__(self, clock=None):
        self._seq = 0
        self._sinks: list = []
        self._clock = clock or time.monotonic
        self._t0: float | None = None

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def emit(self, type_: str, **payload) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(
                f"unknown event type {type_!r}; add it to EVENT_TYPES and mirror "
                f"the payload in web/events.js"
            )
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        event = Event(seq=self._seq, t=round(now - self._t0, 4), type=type_, payload=payload)
        self._seq += 1
        for sink in self._sinks:
            sink(event)
        return event

    # -- constructors -------------------------------------------------------
    # One per event type. Keyword-only so a positional-argument slip can't
    # silently swap two fields of the same type.

    def run_started(
        self,
        *,
        run_id: str,
        ticker: str,
        trade_date: str,
        asset_type: str,
        topology: dict,
        provider: str,
        deep_model: str,
        quick_model: str,
        debate_rounds: int,
        risk_rounds: int,
        synthetic: bool = False,
    ) -> Event:
        """Always seq 0. ``topology`` is the full node/edge spec -- the frontend
        lays out whatever the backend describes rather than hardcoding the graph,
        so changing the analyst selection reshapes the scene for free.

        ``synthetic`` marks a fabricated session. The UI must badge these
        prominently: a demo that invents analysis and renders it identically to a
        real run is presenting fake financial output as genuine, which is not a
        thing to leave to the viewer to infer.
        """
        return self.emit(
            "run_started",
            run_id=run_id,
            ticker=ticker,
            trade_date=trade_date,
            asset_type=asset_type,
            topology=topology,
            provider=provider,
            deep_model=deep_model,
            quick_model=quick_model,
            debate_rounds=debate_rounds,
            risk_rounds=risk_rounds,
            synthetic=synthetic,
        )

    def node_entered(self, *, node: str) -> Event:
        return self.emit("node_entered", node=node)

    def node_exited(self, *, node: str, state: str = "complete") -> Event:
        if state not in NODE_STATES:
            raise ValueError(f"bad node state {state!r}, expected one of {NODE_STATES}")
        return self.emit("node_exited", node=node, state=state)

    def edge_flow(self, *, src: str, dst: str, kind: str = "state") -> Event:
        """A discrete packet of state moving src -> dst. ``kind`` tints the
        particle: "state" (normal handoff), "rebuttal" (debate counter-argument),
        "verdict" (a manager's ruling)."""
        return self.emit("edge_flow", src=src, dst=dst, kind=kind)

    def tool_call(self, *, node: str, call_id: str, tool: str, vendor: str, args: dict) -> Event:
        """``vendor`` is known at call time -- ``route_to_vendor`` resolves the
        chain before dispatch -- so the scene can animate the outbound leg to the
        right satellite rather than waiting for the result to learn where the
        request went."""
        return self.emit(
            "tool_call", node=node, call_id=call_id, tool=tool, vendor=vendor, args=args
        )

    def tool_result(
        self, *, call_id: str, tool: str, ok: bool, vendor: str, size: int, preview: str = ""
    ) -> Event:
        """``preview`` is a short excerpt for the HUD -- never the full payload.
        Tool results can be 100KB of CSV; the scene only needs to show that data
        came back, from whom, and roughly how much."""
        return self.emit(
            "tool_result",
            call_id=call_id,
            tool=tool,
            ok=ok,
            vendor=vendor,
            size=size,
            preview=preview[:280],
        )

    def token_delta(self, *, node: str, text: str) -> Event:
        return self.emit("token_delta", node=node, text=text)

    def debate_turn(self, *, side: str, round_: int, text: str) -> Event:
        if side not in ("bull", "bear"):
            raise ValueError(f"debate side must be bull|bear, got {side!r}")
        return self.emit("debate_turn", side=side, round=round_, text=text)

    def risk_turn(self, *, side: str, round_: int, text: str) -> Event:
        if side not in ("aggressive", "conservative", "neutral"):
            raise ValueError(f"bad risk side {side!r}")
        return self.emit("risk_turn", side=side, round=round_, text=text)

    def report_section(
        self, *, key: str, title: str, body: str, rating: str | None = None
    ) -> Event:
        """A finished report. Full body -- the HUD renders it as markdown, and
        session files are meant to be self-contained.

        ``rating`` carries the 5-tier call for the two reports that make one
        (the Research Manager's plan, the PM's decision). It is extracted
        server-side, where the structured output is still typed, rather than
        regex'd back out of rendered markdown in the browser: the debate
        resolution animation depends on knowing which side carried the argument,
        and that should not hinge on the render format staying stable.
        """
        return self.emit("report_section", key=key, title=title, body=body, rating=rating)

    def decision(
        self,
        *,
        rating: str,
        executive_summary: str,
        investment_thesis: str,
        price_target: float | None = None,
        time_horizon: str | None = None,
    ) -> Event:
        return self.emit(
            "decision",
            rating=rating,
            executive_summary=executive_summary,
            investment_thesis=investment_thesis,
            price_target=price_target,
            time_horizon=time_horizon,
        )

    def run_finished(
        self, *, duration: float, llm_calls: int, tool_calls: int, tokens_in: int, tokens_out: int
    ) -> Event:
        return self.emit(
            "run_finished",
            duration=round(duration, 2),
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def run_error(self, *, node: str | None, message: str) -> Event:
        return self.emit("run_error", node=node, message=message)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class JsonlRecorder:
    """Append-only sink. One JSON object per line, so a session file streams and
    tails cleanly and a truncated write costs at most the final event."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")

    def __call__(self, event: Event) -> None:
        self._fh.write(event.to_json() + "\n")
        self._fh.flush()  # a crashed run must still leave a replayable prefix

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "JsonlRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_session(path: str | Path) -> Iterator[Event]:
    """Replay a recorded session. Validates seq continuity, because a gap means
    the scene would silently miss a state transition and desync."""
    expected = 0
    with Path(path).open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if raw["seq"] != expected:
                raise ValueError(
                    f"{path}:{lineno} seq gap: expected {expected}, got {raw['seq']}"
                )
            expected += 1
            yield Event(seq=raw["seq"], t=raw["t"], type=raw["type"], payload=raw["payload"])
