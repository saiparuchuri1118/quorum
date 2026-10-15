# vendor/

## TradingAgents

A **modified copy** of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0; see `TradingAgents/LICENSE`), vendored at upstream version 0.3.1
(commit `a33fd4c`).

Changes made here relative to upstream:

- `tradingagents/agents/utils/caching.py` (new): Anthropic prompt caching helper.
- The five debate agents (`researchers/bull_researcher.py`, `bear_researcher.py`,
  `risk_mgmt/aggressive_debator.py`, `conservative_debator.py`, `neutral_debator.py`)
  now split their prompts at the analyst-report / debate-history boundary and mark
  the reports with `cache_control`.
- `tests/test_prompt_caching.py` (new).

Everything else is unmodified upstream code and remains the work of its authors.
