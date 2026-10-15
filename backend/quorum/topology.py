"""Graph topology, described as data.

The frontend receives this inside ``run_started`` and lays out whatever it is
given. Nothing about the graph shape is hardcoded in the scene, so dropping an
analyst, adding a debate participant, or reordering the pipeline reshapes the
visualization with no frontend change.

Node ids mirror TradingAgents' internal wire keys (``social``, not
``sentiment``) so events map cleanly onto the upstream graph once the real
adapter is wired in. Labels are what humans read.
"""

from __future__ import annotations

# Analyst wire key -> (display label, state key holding its finished report).
# Wire keys match tradingagents.graph.analyst_execution.ANALYST_NODE_SPECS.
ANALYSTS: dict[str, tuple[str, str]] = {
    "market": ("Market Analyst", "market_report"),
    "social": ("Sentiment Analyst", "sentiment_report"),
    "news": ("News Analyst", "news_report"),
    "fundamentals": ("Fundamentals Analyst", "fundamentals_report"),
}

# Which data vendors each analyst reaches for. Vendors render as satellites the
# tool-call particles fly out to, so the scene shows the pipeline touching the
# outside world rather than agents thinking in a vacuum.
ANALYST_VENDORS: dict[str, tuple[str, ...]] = {
    "market": ("yfinance",),
    "social": ("yfinance", "stocktwits", "reddit"),
    "news": ("yfinance", "fred", "polymarket"),
    "fundamentals": ("yfinance",),
}

VENDOR_LABELS: dict[str, str] = {
    "yfinance": "Yahoo Finance",
    "alpha_vantage": "Alpha Vantage",
    "stocktwits": "StockTwits",
    "reddit": "Reddit",
    "fred": "FRED",
    "polymarket": "Polymarket",
}

RISK_SEATS: tuple[tuple[str, str], ...] = (
    ("aggressive", "Aggressive Analyst"),
    ("conservative", "Conservative Analyst"),
    ("neutral", "Neutral Analyst"),
)


def build_topology(
    selected_analysts: tuple[str, ...] = ("market", "social", "news", "fundamentals"),
    *,
    asset_type: str = "stock",
) -> dict:
    """Return the node/edge spec for a run.

    Crypto runs drop the Fundamentals Analyst, mirroring the upstream CLI's
    ``filter_analysts_for_asset_type`` -- company fundamentals don't exist for a
    coin, and a node that can never light up is worse than an absent one.
    """
    keys = [k for k in selected_analysts if k in ANALYSTS]
    if asset_type == "crypto":
        keys = [k for k in keys if k != "fundamentals"]
    if not keys:
        raise ValueError("at least one analyst must be selected")

    nodes: list[dict] = []
    edges: list[dict] = []

    # -- analysts: a sequential chain, each with its own tool loop ------------
    for key in keys:
        label, report_key = ANALYSTS[key]
        nodes.append(
            {
                "id": key,
                "label": label,
                "group": "analysts",
                "report_key": report_key,
                "vendors": list(ANALYST_VENDORS.get(key, ())),
            }
        )
    for a, b in zip(keys, keys[1:]):
        edges.append({"src": a, "dst": b, "kind": "sequence"})

    # -- vendors: satellites, not pipeline members ---------------------------
    used_vendors: list[str] = []
    for key in keys:
        for vendor in ANALYST_VENDORS.get(key, ()):
            if vendor not in used_vendors:
                used_vendors.append(vendor)
    for vendor in used_vendors:
        nodes.append(
            {
                "id": f"vendor:{vendor}",
                "label": VENDOR_LABELS.get(vendor, vendor),
                "group": "vendors",
            }
        )

    # -- research debate -----------------------------------------------------
    nodes += [
        {"id": "bull", "label": "Bull Researcher", "group": "research"},
        {"id": "bear", "label": "Bear Researcher", "group": "research"},
        {
            "id": "research_manager",
            "label": "Research Manager",
            "group": "research",
            "report_key": "investment_plan",
            "role": "judge",
        },
    ]
    edges += [
        {"src": keys[-1], "dst": "bull", "kind": "sequence"},
        # The debate edge is bidirectional and carries the run's visual weight:
        # it is the only place the pipeline argues with itself.
        {"src": "bull", "dst": "bear", "kind": "debate"},
        {"src": "bear", "dst": "bull", "kind": "debate"},
        {"src": "bull", "dst": "research_manager", "kind": "sequence"},
        {"src": "bear", "dst": "research_manager", "kind": "sequence"},
    ]

    # -- trader --------------------------------------------------------------
    nodes.append(
        {
            "id": "trader",
            "label": "Trader",
            "group": "trading",
            "report_key": "trader_investment_plan",
        }
    )
    edges.append({"src": "research_manager", "dst": "trader", "kind": "verdict"})

    # -- risk triangle: a 3-cycle, then all three feed the PM ----------------
    for rid, label in RISK_SEATS:
        nodes.append({"id": rid, "label": label, "group": "risk"})
    edges.append({"src": "trader", "dst": "aggressive", "kind": "sequence"})
    ring = [rid for rid, _ in RISK_SEATS]
    for a, b in zip(ring, ring[1:] + ring[:1]):
        edges.append({"src": a, "dst": b, "kind": "debate"})
    for rid, _ in RISK_SEATS:
        edges.append({"src": rid, "dst": "portfolio_manager", "kind": "sequence"})

    # -- portfolio manager + terminal verdict --------------------------------
    nodes += [
        {
            "id": "portfolio_manager",
            "label": "Portfolio Manager",
            "group": "portfolio",
            "report_key": "final_trade_decision",
            "role": "judge",
        },
        {"id": "verdict", "label": "Decision", "group": "portfolio", "role": "terminal"},
    ]
    edges.append({"src": "portfolio_manager", "dst": "verdict", "kind": "verdict"})

    return {"nodes": nodes, "edges": edges}


def agent_nodes(topology: dict) -> list[str]:
    """Ids of nodes that actually execute (everything but vendors/terminal)."""
    return [
        n["id"]
        for n in topology["nodes"]
        if n["group"] != "vendors" and n.get("role") != "terminal"
    ]
