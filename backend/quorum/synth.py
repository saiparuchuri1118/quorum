"""Synthetic run generator -- a full, realistic event stream with no API calls.

Why this exists before the real adapter: the frontend is 70% of this project and
the scene needs a dense, correctly-shaped stream to develop against. Waiting six
minutes and spending tokens per iteration would make the visual work miserable.
This produces the same event sequence a real run does, on a fabricated clock, in
milliseconds.

It is also the demo path. A recorded session replays identically every time,
which is what you want on a stage or in a portfolio: no keys, no network, no
chance the LLM says something odd in front of an audience.

The analysis content here is INVENTED. It is written to be structurally
realistic -- right shape, right vocabulary, plausible numbers -- so the HUD has
something meaty to render. It is not real analysis of any security, and every
session it produces carries ``synthetic: true`` so the UI can badge it.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from .events import EventStream, JsonlRecorder
from .topology import ANALYSTS, ANALYST_VENDORS, RISK_SEATS, build_topology


class VirtualClock:
    """A clock the generator advances by hand.

    Lets a 6-minute run generate instantly while still producing the timestamps
    a real run would have, so replay pacing looks right.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Fabricated content
# ---------------------------------------------------------------------------

TOOL_SEQUENCES: dict[str, tuple[tuple[str, str, dict], ...]] = {
    "market": (
        ("get_stock_data", "yfinance", {"symbol": "{ticker}", "start_date": "{start}", "end_date": "{date}"}),
        ("get_indicators", "yfinance", {"symbol": "{ticker}", "indicator": "rsi", "curr_date": "{date}"}),
        ("get_indicators", "yfinance", {"symbol": "{ticker}", "indicator": "macd", "curr_date": "{date}"}),
        ("get_indicators", "yfinance", {"symbol": "{ticker}", "indicator": "close_50_sma", "curr_date": "{date}"}),
        ("get_verified_market_snapshot", "yfinance", {"symbol": "{ticker}", "curr_date": "{date}"}),
    ),
    "social": (
        ("get_news", "yfinance", {"ticker": "{ticker}", "start_date": "{start}", "end_date": "{date}"}),
        ("fetch_stocktwits_messages", "stocktwits", {"ticker": "{ticker}", "limit": 30}),
        ("fetch_reddit_posts", "reddit", {"ticker": "{ticker}"}),
    ),
    "news": (
        ("get_news", "yfinance", {"ticker": "{ticker}", "start_date": "{start}", "end_date": "{date}"}),
        ("get_global_news", "yfinance", {"curr_date": "{date}", "look_back_days": 7}),
        ("get_macro_indicators", "fred", {"indicator": "fed_funds_rate", "curr_date": "{date}"}),
        ("get_macro_indicators", "fred", {"indicator": "core_pce", "curr_date": "{date}"}),
        ("get_prediction_markets", "polymarket", {"topic": "Fed rate cut", "limit": 6}),
    ),
    "fundamentals": (
        ("get_fundamentals", "yfinance", {"ticker": "{ticker}", "curr_date": "{date}"}),
        ("get_income_statement", "yfinance", {"ticker": "{ticker}", "freq": "quarterly"}),
        ("get_balance_sheet", "yfinance", {"ticker": "{ticker}", "freq": "quarterly"}),
        ("get_cashflow", "yfinance", {"ticker": "{ticker}", "freq": "quarterly"}),
    ),
}

REPORTS: dict[str, str] = {
    "market": """## Technical picture

Price closed at **$183.42**, above both the 50 SMA ($171.08) and 200 SMA
($148.96) — the medium- and long-term trends agree, and the 50/200 spread has
been widening for eleven weeks.

RSI at **68.3** is approaching but has not crossed the conventional 70
threshold. In a trend this persistent, RSI has stayed elevated for extended
stretches, so treat it as confirmation of momentum rather than a reversal
signal on its own.

MACD (4.81) sits above its signal line (3.97); the histogram has been positive
for nine sessions but is flattening, which is the first hint of momentum
decelerating without reversing.

Price is riding the upper Bollinger band ($186.20). ATR of $6.14 implies a
normal daily range of roughly 3.3%, so a stop placed tighter than ~$177 would
likely be taken out by noise alone.

| Indicator | Value | Read |
|---|---:|---|
| Close | 183.42 | — |
| 50 SMA | 171.08 | Bullish |
| 200 SMA | 148.96 | Bullish |
| RSI | 68.3 | Extended, not overbought |
| MACD / Signal | 4.81 / 3.97 | Bullish, decelerating |
| ATR | 6.14 | Elevated volatility |
""",
    "social": """**Overall Sentiment:** **Mildly Bullish** (Score: 6.2/10)
**Confidence:** Medium

**StockTwits** — 30 messages, 19 Bullish / 7 Bearish / 4 unlabeled (73/27
split). Moderately bullish, short of the ≥90/10 territory where the ratio
starts reading as contrarian risk. Recurring theme is the upcoming earnings
print rather than any product news.

**Reddit** — 12 posts across r/wallstreetbets, r/stocks, r/investing. The
highest-engagement thread (410 upvotes, 227 comments) argues the current
multiple already prices in two years of datacenter growth. Notably more
cautious than the StockTwits stream.

**News** — 14 articles. Institutional framing is neutral-to-positive and
focused on supply agreements; no negative catalysts in the window.

**Divergence worth flagging:** retail (StockTwits) is meaningfully more bullish
than the most-engaged Reddit discussion, and news framing is neutral. That
spread usually means retail enthusiasm is running ahead of the reported facts —
a caution flag on entry timing, not on the thesis itself.
""",
    "news": """## Macro and event backdrop

**Policy** — Fed funds at 4.33%. Core PCE at 2.6% year over year, down 40bp
over six months but still above target. The 10y/2y spread has re-steepened to
+18bp after fourteen months inverted, which historically precedes a policy
pivot rather than coinciding with one.

**Prediction markets** — Polymarket prices a **68%** chance of at least one cut
by the September meeting ($4.2M volume, +6.1pp over the week). A recession-in-2026
market sits at 22% on thinner volume.

**Sector** — Two sovereign AI infrastructure agreements were announced in the
window, both multi-year. Export-control headlines were quiet, which is itself
informative given how much of the bear case rests on them.

| Factor | Reading | Direction |
|---|---|---|
| Fed funds | 4.33% | Neutral |
| Core PCE | 2.6% YoY | Improving |
| Yield curve | +18bp | Re-steepening |
| Cut by Sept (implied) | 68% | Supportive |
| Export controls | Quiet | Neutral |
""",
    "fundamentals": """## Fundamental position

Market cap $4.47T. Trailing P/E 58.2, forward P/E 34.7 — the compression
implies consensus expects earnings to grow into the multiple rather than the
multiple contracting to meet earnings.

Revenue $148.5B TTM, gross margin 74.6%, operating margin 62.1%. Margins at
this level in a hardware business are unusual and are the single most important
number to watch: they are what the entire valuation rests on, and they are the
most likely thing to normalize.

Balance sheet is unlevered in any meaningful sense — debt/equity 12.4, current
ratio 4.10, free cash flow $60.9B. There is no financial-distress path here;
the risk is entirely multiple compression, not solvency.

**Concentration is the real red flag.** Three customers account for roughly
40%+ of revenue. That is a structural fragility no margin profile offsets: a
capex pause at any one of them lands directly on the top line.

| Metric | Value |
|---|---:|
| Market cap | $4.47T |
| P/E (TTM) | 58.2 |
| Forward P/E | 34.7 |
| Gross margin | 74.6% |
| Operating margin | 62.1% |
| Debt / equity | 12.4 |
| Free cash flow | $60.9B |
""",
}

BULL_TURNS = (
    "Datacenter revenue grew 60% year over year and the current-generation ramp "
    "is still early. Two sovereign agreements landed inside the analysis window "
    "alone, and those are multi-year committed spend, not pipeline. The forward "
    "P/E of 34.7 against that growth rate is not demanding.",
    "On concentration — yes, three customers are 40% of revenue, but those "
    "customers have collectively guided capex *up* for the coming year. "
    "Concentration is only fragility if the counterparties are fragile, and "
    "these are the best-capitalized buyers in the market.",
    "The bear case needs margins to compress. Nothing in the data shows that: "
    "gross margin held at 74.6% through the ramp, which is when you would "
    "expect pressure. Pricing power is intact and the technical picture "
    "confirms institutional accumulation, not distribution.",
)

BEAR_TURNS = (
    "A 58x trailing multiple prices in flawless execution. RSI at 68 with a "
    "flattening MACD histogram says momentum is decelerating while price sits "
    "on the upper band — that is a poor entry regardless of the thesis.",
    "Committed spend is not the same as delivered revenue, and 'guided up' is "
    "not a contract. The concentration risk is that three buyers can change "
    "their minds simultaneously because they are responding to the same signal. "
    "Correlated counterparties are worse than diversified weak ones.",
    "Margins held through the ramp because supply was constrained. Once supply "
    "catches demand, pricing normalizes — that is the entire history of "
    "semiconductors. And the most-engaged retail discussion has already flipped "
    "cautious, which is usually the first cohort to move.",
)

RISK_TURNS: dict[str, tuple[str, ...]] = {
    "aggressive": (
        "Trimming here means paying up to rebuild the position later. The trend "
        "is intact on every timeframe and the macro backdrop is turning "
        "supportive with a 68% implied cut probability. Size into it.",
        "The conservative case is asking us to underwrite a margin scenario "
        "nobody has evidence for yet. Waiting for confirmation on a name moving "
        "this fast means buying 15% higher.",
    ),
    "conservative": (
        "Entering at the upper Bollinger band with RSI at 68 is paying the "
        "worst available price for a thesis that will still be true in a month. "
        "ATR of $6.14 means a normal week can hand you a 7% drawdown on no news.",
        "Customer concentration at 40% is not a footnote. A single capex pause "
        "is a revision to the whole model. Half size, and only on a pullback to "
        "the 50 SMA.",
    ),
    "neutral": (
        "Both sides are arguing about entry, not about the thesis — that tells "
        "you the disagreement is tactical. Scale in: establish a partial "
        "position now so a continued move is not entirely missed, and hold "
        "reserve for the pullback the conservative case correctly identifies.",
        "Set the stop below the 50 SMA rather than at an ATR multiple. That "
        "respects the volatility the aggressive case is right about while "
        "honoring the invalidation level the conservative case is right about.",
    ),
}

RESEARCH_PLAN = """**Recommendation**: Overweight

**Rationale**: The bull case carried the argument on fundamentals and the bear
case carried it on timing, and neither refuted the other. Growth, margins, and
the macro backdrop are genuinely supportive; the 58x trailing multiple and an
entry at the upper Bollinger band with decelerating momentum are genuinely poor
timing. The bear's strongest point went unanswered: customer concentration is
correlated concentration, since the three major buyers respond to the same
demand signal and could revise together.

That combination argues for exposure, not conviction sizing — hence Overweight
rather than Buy.

**Strategic Actions**: Build to a target weight in two tranches. Establish the
first half now to avoid missing a continued move; hold the second for a
retracement toward the 50 SMA near $171. Invalidation is a sustained close
below the 50 SMA, which would break the trend the entire technical case rests
on. Size the total position so a full stop-out costs no more than 2% of
portfolio value.
"""

TRADER_PLAN = """**Action**: Buy

**Reasoning**: The research plan's Overweight rating and two-tranche structure
are sound given that the disagreement in the debate was about entry timing
rather than the thesis. Establishing partial exposure now respects the momentum
the technical report documents, while the reserved tranche respects the
concentration and multiple risk the bear case established.

**Entry Price**: 183.42

**Stop Loss**: 170.5

**Position Sizing**: 3% of portfolio now, 3% reserved for a retracement to the
50 SMA

FINAL TRANSACTION PROPOSAL: **BUY**
"""

PM_DECISION = {
    "rating": "Overweight",
    "executive_summary": (
        "Establish a half-weight position at market and reserve an equal tranche "
        "for a retracement toward the 50 SMA near $171. Stop on a sustained close "
        "below $170.50, which invalidates the trend the technical case rests on. "
        "Horizon is two to three quarters, reassessed at the next earnings print."
    ),
    "investment_thesis": (
        "The risk debate converged faster than the research debate did: all three "
        "seats agreed the thesis is intact and disagreed only about entry. The "
        "neutral analyst's framing is the correct resolution — scale in rather "
        "than choosing between the aggressive and conservative extremes.\n\n"
        "Two findings drive the rating down from Buy. First, customer "
        "concentration is correlated: three buyers at 40%+ of revenue who respond "
        "to the same demand signal can revise together, which is a materially "
        "worse risk profile than the raw concentration number suggests. Second, "
        "the sentiment divergence — retail meaningfully more bullish than both "
        "the most-engaged discussion and the news framing — is a recognized "
        "late-entry pattern.\n\n"
        "Neither is a reason to avoid the position. Both are reasons to size it "
        "as exposure rather than conviction."
    ),
    "price_target": 210.0,
    "time_horizon": "2-3 quarters",
}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _chunk(text: str, rng: random.Random, lo: int = 18, hi: int = 46):
    """Split prose into token-ish deltas for the typewriter effect."""
    words = text.split(" ")
    i = 0
    while i < len(words):
        n = rng.randint(lo, hi)
        yield " ".join(words[i : i + n]) + (" " if i + n < len(words) else "")
        i += n


def generate(
    stream: EventStream,
    clock: VirtualClock,
    *,
    ticker: str = "NVDA",
    trade_date: str = "2026-01-15",
    start_date: str = "2026-01-08",
    asset_type: str = "stock",
    analysts: tuple[str, ...] = ("market", "social", "news", "fundamentals"),
    debate_rounds: int = 2,
    risk_rounds: int = 2,
    seed: int = 7,
) -> None:
    rng = random.Random(seed)
    topology = build_topology(analysts, asset_type=asset_type)
    keys = [n["id"] for n in topology["nodes"] if n["group"] == "analysts"]

    llm_calls = tool_calls = tokens_in = tokens_out = 0

    stream.run_started(
        run_id=f"synth-{ticker.lower()}-{trade_date}",
        ticker=ticker,
        trade_date=trade_date,
        asset_type=asset_type,
        topology=topology,
        provider="openai",
        deep_model="gpt-5.5",
        quick_model="gpt-5.4-mini",
        debate_rounds=debate_rounds,
        risk_rounds=risk_rounds,
        synthetic=True,
    )
    clock.advance(0.8)

    # -- analyst chain -------------------------------------------------------
    for idx, key in enumerate(keys):
        label, report_key = ANALYSTS[key]
        stream.node_entered(node=key)
        clock.advance(rng.uniform(0.6, 1.4))
        llm_calls += 1

        for call_no, (tool, vendor, arg_tmpl) in enumerate(TOOL_SEQUENCES.get(key, ())):
            call_id = f"{key}-{call_no}"
            args = {
                k: (v.format(ticker=ticker, date=trade_date, start=start_date) if isinstance(v, str) else v)
                for k, v in arg_tmpl.items()
            }
            stream.tool_call(node=key, call_id=call_id, tool=tool, vendor=vendor, args=args)
            tool_calls += 1
            clock.advance(rng.uniform(0.9, 3.1))
            size = rng.randint(1200, 48000)
            stream.tool_result(
                call_id=call_id,
                tool=tool,
                ok=True,
                vendor=vendor,
                size=size,
                preview=f"{size:,} bytes from {vendor}",
            )
            clock.advance(rng.uniform(0.4, 1.2))
            llm_calls += 1
            tokens_in += rng.randint(1800, 6400)

        body = REPORTS[key].strip()
        for delta in _chunk(body, rng):
            stream.token_delta(node=key, text=delta)
            clock.advance(rng.uniform(0.18, 0.5))
        tokens_out += len(body) // 4

        stream.report_section(key=report_key, title=label, body=body)
        stream.node_exited(node=key, state="complete")
        clock.advance(0.5)
        if idx + 1 < len(keys):
            stream.edge_flow(src=key, dst=keys[idx + 1])
            clock.advance(0.6)

    # -- research debate -----------------------------------------------------
    stream.edge_flow(src=keys[-1], dst="bull")
    clock.advance(0.7)

    for round_no in range(debate_rounds):
        for side, turns in (("bull", BULL_TURNS), ("bear", BEAR_TURNS)):
            node = side
            other = "bear" if side == "bull" else "bull"
            stream.node_entered(node=node)
            clock.advance(rng.uniform(0.5, 1.0))
            llm_calls += 1
            text = turns[round_no % len(turns)]
            for delta in _chunk(text, rng, 12, 28):
                stream.token_delta(node=node, text=delta)
                clock.advance(rng.uniform(0.22, 0.55))
            tokens_in += rng.randint(4200, 9000)
            tokens_out += len(text) // 4
            stream.debate_turn(side=side, round_=round_no + 1, text=text)
            stream.node_exited(node=node, state="complete")
            # The rebuttal edge is what makes the debate legible in 3D.
            stream.edge_flow(src=node, dst=other, kind="rebuttal")
            clock.advance(rng.uniform(0.5, 0.9))

    for side in ("bull", "bear"):
        stream.edge_flow(src=side, dst="research_manager")
    clock.advance(0.6)

    stream.node_entered(node="research_manager")
    llm_calls += 1
    clock.advance(rng.uniform(1.5, 2.5))
    for delta in _chunk(RESEARCH_PLAN.strip(), rng):
        stream.token_delta(node="research_manager", text=delta)
        clock.advance(rng.uniform(0.2, 0.5))
    tokens_in += 14000
    tokens_out += len(RESEARCH_PLAN) // 4
    stream.report_section(
        key="investment_plan", title="Research Manager",
        body=RESEARCH_PLAN.strip(), rating="Overweight",
    )
    stream.node_exited(node="research_manager", state="complete")
    stream.edge_flow(src="research_manager", dst="trader", kind="verdict")
    clock.advance(0.8)

    # -- trader --------------------------------------------------------------
    stream.node_entered(node="trader")
    llm_calls += 1
    clock.advance(rng.uniform(1.0, 2.0))
    for delta in _chunk(TRADER_PLAN.strip(), rng):
        stream.token_delta(node="trader", text=delta)
        clock.advance(rng.uniform(0.2, 0.45))
    tokens_in += 9000
    tokens_out += len(TRADER_PLAN) // 4
    stream.report_section(key="trader_investment_plan", title="Trader", body=TRADER_PLAN.strip())
    stream.node_exited(node="trader", state="complete")
    stream.edge_flow(src="trader", dst="aggressive")
    clock.advance(0.7)

    # -- risk triangle -------------------------------------------------------
    ring = [rid for rid, _ in RISK_SEATS]
    for round_no in range(risk_rounds):
        for i, rid in enumerate(ring):
            stream.node_entered(node=rid)
            clock.advance(rng.uniform(0.5, 1.0))
            llm_calls += 1
            turns = RISK_TURNS[rid]
            text = turns[round_no % len(turns)]
            for delta in _chunk(text, rng, 12, 26):
                stream.token_delta(node=rid, text=delta)
                clock.advance(rng.uniform(0.22, 0.5))
            tokens_in += rng.randint(5000, 11000)
            tokens_out += len(text) // 4
            stream.risk_turn(side=rid, round_=round_no + 1, text=text)
            stream.node_exited(node=rid, state="complete")
            nxt = ring[(i + 1) % len(ring)]
            last = round_no == risk_rounds - 1 and i == len(ring) - 1
            if not last:
                stream.edge_flow(src=rid, dst=nxt, kind="rebuttal")
            clock.advance(rng.uniform(0.4, 0.8))

    for rid in ring:
        stream.edge_flow(src=rid, dst="portfolio_manager")
    clock.advance(0.7)

    # -- portfolio manager ---------------------------------------------------
    stream.node_entered(node="portfolio_manager")
    llm_calls += 1
    clock.advance(rng.uniform(2.0, 3.2))
    pm_body = (
        f"**Rating**: {PM_DECISION['rating']}\n\n"
        f"**Executive Summary**: {PM_DECISION['executive_summary']}\n\n"
        f"**Investment Thesis**: {PM_DECISION['investment_thesis']}\n\n"
        f"**Price Target**: {PM_DECISION['price_target']}\n\n"
        f"**Time Horizon**: {PM_DECISION['time_horizon']}"
    )
    for delta in _chunk(pm_body, rng):
        stream.token_delta(node="portfolio_manager", text=delta)
        clock.advance(rng.uniform(0.2, 0.5))
    tokens_in += 22000
    tokens_out += len(pm_body) // 4
    stream.report_section(
        key="final_trade_decision", title="Portfolio Manager",
        body=pm_body, rating=PM_DECISION["rating"],
    )
    stream.decision(**PM_DECISION)
    stream.node_exited(node="portfolio_manager", state="complete")
    stream.edge_flow(src="portfolio_manager", dst="verdict", kind="verdict")
    clock.advance(1.0)

    stream.run_finished(
        duration=clock.now,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate a synthetic Quorum session.")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--date", default="2026-01-15")
    ap.add_argument("--asset-type", default="stock", choices=("stock", "crypto"))
    ap.add_argument("--debate-rounds", type=int, default=2)
    ap.add_argument("--risk-rounds", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None, help="output .jsonl (default: sessions/<ticker>-demo.jsonl)")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "sessions" / f"{args.ticker.lower()}-demo.jsonl"
    )

    clock = VirtualClock()
    stream = EventStream(clock=clock)
    with JsonlRecorder(out) as rec:
        stream.add_sink(rec)
        generate(
            stream,
            clock,
            ticker=args.ticker,
            trade_date=args.date,
            asset_type=args.asset_type,
            debate_rounds=args.debate_rounds,
            risk_rounds=args.risk_rounds,
            seed=args.seed,
        )

    n = sum(1 for _ in out.open(encoding="utf-8"))
    print(f"wrote {n} events -> {out}")
    print(f"simulated duration: {clock.now/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
