"""Record a real TradingAgents run to a Quorum session file.

    python -m quorum.record --check                 # validate wiring, spend nothing
    python -m quorum.record --ticker NVDA           # the real thing

``--check`` constructs the graph, resolves the instrument identity, and pulls a
verified market snapshot -- exercising dependency imports, the API key, and data
access -- without making a single LLM call. Run it first; a misconfigured
provider should fail in two seconds, not eight minutes into a paid run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .adapter import run_live
from .events import EventStream, JsonlRecorder

SESSIONS = Path(__file__).resolve().parents[2] / "sessions"

# Fallback provider -> key-env map, used only when TradingAgents itself can't be
# imported (which --check reports as its first failure anyway). The real lookup
# defers to upstream's canonical table, because guessing
# f"{PROVIDER}_API_KEY" is wrong for several providers -- Kimi reads
# MOONSHOT_API_KEY, the dual-region providers have separate CN vars, and Bedrock
# uses the AWS credential chain rather than a single key.
_FALLBACK_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "ollama": None,
    "openai_compatible": None,
}


def key_env_for(provider: str) -> str | None:
    """Env var holding this provider's key, or None if it needs no key."""
    try:
        from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

        if provider in PROVIDER_API_KEY_ENV:
            return PROVIDER_API_KEY_ENV[provider]
    except ImportError:
        pass
    return _FALLBACK_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")


def _preflight(args) -> int:
    """Validate everything a real run needs, without invoking a model."""
    print("checking dependencies...")
    try:
        import tradingagents  # noqa: F401
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as exc:
        print(f"  FAIL: TradingAgents is not importable: {exc}")
        print('  fix: pip install -e path/to/TradingAgents')
        return 1
    print(f"  ok: tradingagents {getattr(tradingagents, '__version__', '(dev)')}")

    env = key_env_for(args.provider)
    if env:
        if os.environ.get(env):
            print(f"  ok: {env} is set")
        else:
            print(f"  FAIL: {env} is not set")
            print(f"  fix: add {env}=... to a .env file or export it")
            return 1
    else:
        print(f"  ok: provider {args.provider!r} needs no key")

    print("checking market data...")
    try:
        from tradingagents.agents.utils.agent_utils import resolve_instrument_identity
        from tradingagents.dataflows.config import set_config

        set_config({**DEFAULT_CONFIG})
        identity = resolve_instrument_identity(args.ticker)
        if identity.get("company_name"):
            print(f"  ok: {args.ticker} -> {identity['company_name']}"
                  f" ({identity.get('sector', 'n/a')})")
        else:
            # Fail-open by design upstream, so this is a warning, not an error.
            print(f"  WARN: no identity resolved for {args.ticker} "
                  f"(rate-limited, or an unknown symbol)")
    except Exception as exc:
        print(f"  WARN: identity lookup failed: {type(exc).__name__}: {exc}")

    try:
        from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot

        snap = build_verified_market_snapshot(args.ticker, args.date, 5)
        line = next((l for l in snap.splitlines() if l.startswith("| Close")), "")
        print(f"  ok: verified snapshot for {args.date} {line.strip()}")
    except Exception as exc:
        print(f"  FAIL: no market data for {args.ticker} on {args.date}: {exc}")
        return 1

    print("\npreflight passed - a real run should work.")
    print(f"next: python -m quorum.record --ticker {args.ticker} --date {args.date}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a live TradingAgents run.")
    ap.add_argument("--ticker", default="NVDA")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--asset-type", default="stock", choices=("stock", "crypto"))
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--deep", default=None, help="deep-thinking model")
    ap.add_argument("--quick", default=None, help="quick-thinking model")
    ap.add_argument("--debate-rounds", type=int, default=1)
    ap.add_argument("--risk-rounds", type=int, default=1)
    ap.add_argument("--analysts", default="market,social,news,fundamentals")
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true", help="preflight only; no LLM calls")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.date is None:
        from datetime import date
        args.date = date.today().isoformat()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.check:
        return _preflight(args)

    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = {**DEFAULT_CONFIG, "llm_provider": args.provider,
           "max_debate_rounds": args.debate_rounds,
           "max_risk_discuss_rounds": args.risk_rounds}
    if args.deep:
        cfg["deep_think_llm"] = args.deep
    if args.quick:
        cfg["quick_think_llm"] = args.quick

    out = Path(args.out) if args.out else SESSIONS / f"{args.ticker.lower()}-live.jsonl"
    analysts = tuple(a.strip() for a in args.analysts.split(",") if a.strip())

    print(f"recording {args.ticker} @ {args.date} via {args.provider} "
          f"({cfg['deep_think_llm']} / {cfg['quick_think_llm']})")
    print(f"  -> {out}")
    print("  this makes real API calls and costs real tokens. Ctrl-C to abort.\n")

    stream = EventStream()
    with JsonlRecorder(out) as rec:
        stream.add_sink(rec)
        # Progress to stderr so the session file stays clean on stdout redirect.
        stream.add_sink(lambda e: print(f"  [{e.t:7.2f}s] {e.type}", file=sys.stderr)
                        if e.type != "token_delta" else None)
        try:
            run_live(
                stream, ticker=args.ticker, trade_date=args.date,
                asset_type=args.asset_type, analysts=analysts, config=cfg,
            )
        except KeyboardInterrupt:
            print("\naborted - partial session retained (it is still replayable).")
            return 130

    n = sum(1 for _ in out.open(encoding="utf-8"))
    print(f"\nwrote {n} events -> {out}")
    print(f"view: http://localhost:8777/web/index.html?session=../sessions/{out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
