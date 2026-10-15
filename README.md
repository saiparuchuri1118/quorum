# Quorum

A 3D visualization of a multi-agent trading-analysis pipeline. Analysts gather
evidence, a bull and a bear argue over it, a trader proposes, three risk seats
fight about sizing, and a portfolio manager rules. Quorum renders that as a
spatial graph you can watch, scrub, and inspect.

Built on a modified copy (see `vendor/README.md`) of [TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0) for the agent runtime.

> **Research scaffold, not a trading system.** Nothing here is financial advice,
> and it should not be wired to a live brokerage account. Demo sessions contain
> fabricated analysis and are labeled `synthetic` in the UI.

> **Never paste an API key into a chat, an issue, or a commit.** If one is ever
> exposed, revoke it and issue a new one — don't keep using it. `backend/.env`
> is gitignored; put keys there and nowhere else. `quorum.server`'s static
> handler is an explicit allowlist (`web/`, `sessions/`) for exactly this
> reason — an earlier version served the whole project tree, which made
> `backend/.env` fetchable over HTTP at `/backend/.env`. Fixed, with a
> regression test (`tests/test_server.py`) that asserts it stays fixed.

---

## The one design decision everything follows from

**The backend never talks to the frontend directly. It emits events.**

```
agent runtime ──► EventStream ──┬──► JsonlRecorder ──► sessions/*.jsonl
                                └──► WebSocket ──────► browser
```

The frontend is a pure function of that event stream. Which buys:

- **Demos that cannot fail** — replay from disk, no keys, no network, no LLM latency.
- **Scrubbing for free** — seek(t) = reset consumers, re-apply the prefix.
- **Zero-cost frontend work** — iterate on the scene without spending tokens.
- **Reproducible visual bugs** — commit the session file that triggers them.

A live run and a recorded run are the same code path. That is the whole trick.

---

## Running it

Requires Python 3.10+. No install step, no dependencies — the event layer and
the generator are stdlib-only.

```bash
python -m quorum.synth
```

from `backend/`, which writes `sessions/nvda-demo.jsonl`. Then, from the same
directory, start the dev server:

```bash
python -m quorum.server --port 8777
```

Then visit **http://localhost:8777/**.

This is `quorum.server`, not a plain static file server — it also exposes the
recording API the search box uses (see below). Opening `index.html` from the
filesystem will not work regardless of which server you use — ES modules and
`fetch` both require an HTTP origin.

Load a different session with `?session=../sessions/other.jsonl`.

### Searching for a ticker

Type a symbol into the box in the top bar and hit **Analyze**. That calls
`POST /api/record` on the dev server, which spawns a real recording in the
background and redirects you to the live view once it's accepted — the same
path as running `quorum.record` by hand, just triggered from the page.

Only one recording runs at a time; a second request while one is in flight
gets rejected (409) rather than queued, since this is a "just you, on your
machine" tool and your API key pays for every run. It uses cheap defaults
(Haiku 4.5 for both tiers, one debate round, one risk round) — this is the "try
a ticker" path, not the "produce the recording you'll ship" path; for that, use
`quorum.record` directly with a stronger deep model and more rounds (below).

If no key is configured for the default provider, the request fails with 422
and the exact fix (`ANTHROPIC_API_KEY is not set` — add it to `backend/.env`)
rather than starting a run that would fail eight minutes in.

**This is single-user and local only.** The server binds `127.0.0.1`, has no
auth, and every request that reaches it can trigger a paid run against your
key. Do not put it behind a public tunnel without adding rate limiting and
auth first — see the "Not built yet" list.

### Recording a real run

The bundled demo is synthetic — the analysis in it was written by hand so the
UI had something to render, and every such session is badged in the corner. To
produce a genuine one you need TradingAgents installed and a provider key:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ../vendor/TradingAgents
```

Validate the wiring before spending anything — this constructs the graph,
resolves the ticker, and pulls a verified market snapshot without making a
single LLM call:

```bash
python -m quorum.record --check --ticker NVDA
```

Then record for real:

```bash
python -m quorum.record --ticker NVDA --provider anthropic --quick claude-haiku-4-5 --deep claude-sonnet-5
```

Keys go in `backend/.env` (copy `.env.example`). Never paste one into a chat,
an issue, or a commit — if a key is ever exposed, revoke it and issue a new one
rather than continuing to use it.

That writes `sessions/nvda-live.jsonl` with `synthetic: false`, so the badge
disappears on its own. A run makes ~30+ LLM calls across four analysts, a
two-sided debate, and a three-way risk committee, so it costs real tokens and
takes several minutes. `--debate-rounds 1 --risk-rounds 1` keeps it cheap.

"Real" here means the events genuinely came from the pipeline — real market
data, real model reasoning. It does not mean the analysis is good. TradingAgents
is explicit that it is a research scaffold whose output varies with model,
temperature, and date.

**Controls** — space plays/pauses, `R` restarts, `M` mutes, drag the scrubber to
seek, click any node to pin its report, drag to orbit, scroll to zoom. The
camera moves itself between phases and yields to you for a few seconds after any
manual orbit.

Sound is on by default and starts on your first click (browsers require a
gesture before audio).

---

## The debate

The bull/bear exchange is the set piece, because structured disagreement is the
one thing this architecture does that a progress bar fundamentally cannot show.

When the debate opens, the camera takes a composed shot standing off the
bull–bear axis so the space between them reads as contested volume. Each
argument spawns a **claim** — a persistent shard that clusters on its own side
of the axis and orbits the midpoint. They accumulate across rounds, so the
weight of the argument is visible rather than implied.

Tension rises with each round: the connecting arcs heat from violet toward hot
pink, the claims orbit faster, and a low drone tightens underneath — the filter
opening is what actually communicates escalation.

The Research Manager's ruling collapses it. Claims from the side that carried
the argument accelerate into the judge and are absorbed; the other side's drift
apart and dissolve. `Hold` is a genuine draw, so nothing is absorbed.

Which side won comes from the `rating` field on the event, extracted server-side
where the output is still typed — not regex'd back out of rendered markdown in
the browser.

---

## Layout

```
quorum/
├── backend/quorum/
│   ├── events.py      the wire contract; every event type + JSONL recorder
│   ├── topology.py    graph shape as data, sent inside run_started
│   └── synth.py       synthetic run generator (no API calls)
├── sessions/          recorded runs, .jsonl
└── web/
    ├── replay.js      playback clock, seek, event dispatch
    ├── scene.js       Three.js — layout, nodes, edges, particles, claims, bloom
    ├── phases.js      act detection + camera director (composed shots)
    ├── audio.js       synthesized sound — oscillators only, no assets
    ├── hud.js         DOM overlay + a small markdown renderer
    └── main.js        wiring and derived run state
```

### Rules worth keeping

**No text in WebGL.** Labels are DOM elements positioned by `CSS2DRenderer`;
reports render as HTML. This is the single biggest reason it reads as designed
rather than as a tech demo — billboarded bitmap fonts are what make 3D UIs look
cheap.

**Topology is data, never hardcoded.** The scene lays out whatever
`run_started.topology` describes. Drop an analyst, add a debate seat, reorder
the pipeline — the visualization follows with no frontend change.

**Every consumer is rebuildable from a prefix.** If a consumer can't be reset
and replayed into the same state, scrubbing silently desyncs. `silent` means
"skip the animation", never "skip the state".

---

## Status

**Working**

- Event schema, JSONL recorder, seq-validated reader
- Topology spec driving layout
- Synthetic generator — 169 events, 13 agent nodes, 7 reports, realistic timings
- Playback: play/pause/seek/speed, deterministic scrubbing (verified: identical
  state on repeat visits to the same timestamp)
- 3D scene: bloom, DOM labels, state-driven materials, bowed debate arcs,
  particle flows, tool round-trips to vendor satellites
- Debate set piece: accumulating claim shards, tension-driven arc heat,
  rating-driven absorb/dissolve resolution
- Camera direction: composed shot per phase, eased moves, snap-on-scrub,
  yields to manual orbit
- Audio: synthesized activation tones, throttled tool ticks, tension drone,
  verdict chord keyed to the rating; mute toggle
- HUD: metadata, live stats, event feed, streaming report panel, phase title,
  verdict card

- **Live adapter** (`adapter.py`): LangGraph `["updates","messages"]` →
  events. Node map verified against the real compiled graph (12/12 agent nodes,
  no unmapped, no stale). 17 translation tests pass.
- **A real recording has run end to end**, against Anthropic (Haiku 4.5 /
  Sonnet 5), live NVDA market data: 1,541 events, all 12 agent nodes, 30 tool
  calls each correctly paired to a result, a full debate and risk committee, a
  parsed final decision, `synthetic: false`. 10 minutes, ~231k input / 40k
  output tokens.
- **Prompt caching** on the five debate agents (`utils/caching.py`): the four
  analyst reports (~13.5k tokens on that run) are cached across every
  researcher and risk-seat call via `cache_control`, instead of re-sent in full
  each time. Degrades to a plain string on non-Anthropic providers and below
  the bound model's cache-minimum (512–4096 tokens depending on model — not
  monotonic across generations). 14 tests.
- **Live view** (`?follow=1`): tails a session file a recorder is still
  writing, polling and pushing only unseen events into the same `Replay` the
  recorded path uses. Stops on its own at `run_finished`/`run_error`.
- **`quorum.server`**: a stdlib-only local dev server that replaces plain
  `http.server` — serves `web/` and `sessions/` (allowlisted, see the security
  note above) and exposes `POST /api/record`, so a ticker search box can
  trigger a real recording without a terminal. One run at a time; 422 on a
  missing key instead of an eight-minute failure. 15 tests, including the
  static-serving regression coverage.
- **Ticker search** in the HUD, wired to that endpoint.

**Not built yet**

- **The recording you'd actually ship.** Every real run so far has used cheap
  settings (Haiku 4.5, 1 debate/risk round) to validate the pipeline, not
  produce a recording worth watching. Re-record with a stronger deep model and
  2 rounds each once you're ready — see "Recording a real run" above.
- **A remote/public server.** `quorum.server` is deliberately single-user,
  localhost-only, no-auth — see the caution above. Turning this into something
  other people can reach needs auth, rate limiting, and a real answer to "who
  pays for the tokens."
- **React migration.** The scene is vanilla Three.js because Node isn't
  installed on this machine. Porting to Vite + react-three-fiber is mostly
  mechanical — same Three.js underneath, the scene math is unchanged.
- **Vendored Three.js.** Currently loaded from unpkg via import map. For a demo
  that must work offline, vendor it locally.
