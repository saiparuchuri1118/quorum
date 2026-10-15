"""Local dev server: static files (web/, sessions/) + one API endpoint that
triggers a real recording.

Why this exists
----------------
Everything up to now was either a static frontend (serve web/ and read a
.jsonl) or a CLI you ran by hand (record.py). Selecting a ticker from the page
itself needs something that can spawn a process -- a plain file server can't do
that, so this replaces `python -m http.server` as the dev entry point.

Deliberately stdlib-only, matching the rest of this project's backend. FastAPI
is the obvious real answer once this needs more than one endpoint (see the
README's "not built yet" list) -- this is the minimum that unblocks a search
box on your own machine, not the shape it should take once other people are
involved.

Concurrency
-----------
Exactly one recording at a time. This is a "just you, on your machine"
tool: your API key pays for every run, and a second concurrent run is far
more likely to be an accidental double-click than an intentional need for
parallelism. A second request while one is in flight gets 409.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]        # quorum/
BACKEND = ROOT / "backend"                          # quorum/backend/  (.env lives here)
SESSIONS = ROOT / "sessions"
LOGS = ROOT / "sessions" / ".logs"

# Static serving is an allowlist, not "everything under ROOT" -- ROOT also
# contains backend/.env (the Anthropic key) and backend/quorum/*.py. Naively
# serving the whole tree (as `python -m http.server --directory quorum` did
# before this file existed) makes the key fetchable by anyone who can reach
# the port: GET /backend/.env. Only these two subtrees are ever web-reachable.
_STATIC_ROOTS = {"web", "sessions"}

# Same charset TradingAgents' own CLI validator accepts (equities, futures,
# forex, indices) -- see cli/utils.py::is_valid_ticker_input upstream.
_TICKER_RE = re.compile(r"^[A-Za-z0-9._\-^=]{1,20}$")

# Kept intentionally small and cheap: this is the "try a ticker" path, not the
# "produce the recording you'll ship" path. See record.py's docstring for the
# two-tier recommendation (cheap validation run, then a real one).
_DEFAULTS = {
    "provider": "anthropic",
    "quick": "claude-haiku-4-5",
    "deep": "claude-sonnet-5",
    "debate_rounds": 1,
    "risk_rounds": 1,
    "asset_type": "stock",
}

_state_lock = threading.Lock()
_active: dict | None = None  # {"run_id","ticker","proc","session","started"} or None


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _key_env_for(provider: str) -> str | None:
    """Defers to TradingAgents' own provider->env-var table when importable,
    same rationale as record.py: guessing the var name is wrong for several
    providers (Kimi, the dual-region ones, Bedrock's credential chain)."""
    try:
        sys.path.insert(0, str(BACKEND))
        from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

        return PROVIDER_API_KEY_ENV.get(provider)
    except ImportError:
        return {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "google": "GOOGLE_API_KEY"}.get(provider)


def _load_dotenv_into_environ() -> None:
    """The subprocess we spawn loads its own .env (tradingagents does this on
    import), but the key-presence check below runs in *this* process first --
    so it needs the same file read before it can answer honestly."""
    env_path = BACKEND / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


class Handler(BaseHTTPRequestHandler):
    server_version = "QuorumDev/0.1"

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        # Quiet the default per-request access log; API activity is logged
        # explicitly where it matters (run start/stop) instead.
        pass

    # -- routing --------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._health()
        if path.startswith("/api/record/") and path.endswith("/status"):
            return self._status(path.split("/")[3])
        return self._static()

    def do_HEAD(self):
        # curl -I and some browser preflight probes use HEAD; treat it as GET
        # with the body discarded rather than 501ing.
        self.do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/record":
            return self._start_record()
        _json(self, 404, {"error": f"no such endpoint: {path}"})

    # -- static files (web/, sessions/) ---------------------------------

    def _static(self):
        from http.server import SimpleHTTPRequestHandler

        rel = urlparse(self.path).path.lstrip("/")
        if rel in ("", "index.html"):
            # Serve the app directly rather than 302-redirecting to it -- one
            # response instead of a redirect the client has to follow.
            rel = "web/index.html"

        parts = rel.split("/")
        # Allowlist by top-level directory (web/, sessions/) rather than a
        # bare "resolves inside ROOT" check -- ROOT also contains backend/,
        # which holds .env and the server's own source. Any path segment
        # starting with "." is rejected outright as defense in depth (blocks
        # sessions/.logs/*, and any future dotfile under an allowed root).
        if parts[0] not in _STATIC_ROOTS or any(p.startswith(".") for p in parts):
            return _json(self, 403, {"error": "forbidden"})

        fs_path = (ROOT / rel).resolve()
        allowed_root = (ROOT / parts[0]).resolve()
        if allowed_root not in fs_path.parents and fs_path != allowed_root:
            return _json(self, 403, {"error": "forbidden"})  # symlink/.. escape
        if not fs_path.is_file():
            return _json(self, 404, {"error": "not found"})

        handler = SimpleHTTPRequestHandler
        ctype = handler.extensions_map.get(fs_path.suffix, "application/octet-stream")
        data = fs_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # sessions/*.jsonl must never serve stale
        self.end_headers()
        self.wfile.write(data)

    # -- api --------------------------------------------------------

    def _health(self):
        _load_dotenv_into_environ()
        provider = _DEFAULTS["provider"]
        env = _key_env_for(provider)
        with _state_lock:
            running = _active is not None and _active["proc"].poll() is None
        _json(self, 200, {
            "ok": True,
            "provider": provider,
            "key_configured": bool(env and os.environ.get(env)),
            "run_in_progress": running,
        })

    def _status(self, run_id: str):
        with _state_lock:
            if not _active or _active["run_id"] != run_id:
                return _json(self, 404, {"error": "unknown run_id"})
            proc = _active["proc"]
            code = proc.poll()
            payload = {
                "run_id": run_id,
                "ticker": _active["ticker"],
                "running": code is None,
                "returncode": code,
                "elapsed": round(time.monotonic() - _active["started"], 1),
                "session": f"sessions/{_active['session'].name}",
            }
        _json(self, 200, payload)

    def _start_record(self):
        global _active
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return _json(self, 400, {"error": "malformed JSON body"})

        ticker = str(body.get("ticker", "")).strip().upper()
        if not _TICKER_RE.match(ticker):
            return _json(self, 400, {
                "error": f"invalid ticker {ticker!r}; expected letters/digits/._-^=, max 20 chars",
            })

        provider = str(body.get("provider") or _DEFAULTS["provider"])
        _load_dotenv_into_environ()
        env = _key_env_for(provider)
        if env and not os.environ.get(env):
            return _json(self, 422, {
                "error": f"{env} is not set",
                "fix": f"add {env}=... to backend/.env",
            })

        with _state_lock:
            if _active is not None and _active["proc"].poll() is None:
                return _json(self, 409, {
                    "error": "a run is already in progress",
                    "run_id": _active["run_id"],
                    "ticker": _active["ticker"],
                })

            run_id = f"{ticker.lower()}-{int(time.time())}"
            session = SESSIONS / f"{run_id}.jsonl"
            LOGS.mkdir(parents=True, exist_ok=True)
            log_path = LOGS / f"{run_id}.log"

            cmd = [
                sys.executable, "-m", "quorum.record",
                "--ticker", ticker,
                "--provider", provider,
                "--quick", str(body.get("quick") or _DEFAULTS["quick"]),
                "--deep", str(body.get("deep") or _DEFAULTS["deep"]),
                "--debate-rounds", str(body.get("debate_rounds") or _DEFAULTS["debate_rounds"]),
                "--risk-rounds", str(body.get("risk_rounds") or _DEFAULTS["risk_rounds"]),
                "--asset-type", str(body.get("asset_type") or _DEFAULTS["asset_type"]),
                "--out", str(session),
            ]
            if body.get("date"):
                cmd += ["--date", str(body["date"])]

            print(f"[record] {run_id}: {' '.join(cmd)}")
            log_fh = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd, cwd=str(BACKEND), stdout=log_fh, stderr=subprocess.STDOUT,
            )
            _active = {
                "run_id": run_id, "ticker": ticker, "proc": proc,
                "session": session, "started": time.monotonic(),
            }

        _json(self, 202, {
            "run_id": run_id,
            "ticker": ticker,
            "session": f"sessions/{session.name}",
            "status_url": f"/api/record/{run_id}/status",
            "view_url": f"/web/index.html?session=../sessions/{session.name}&follow=1",
        })


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Quorum dev server: static files + recording API.")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    SESSIONS.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"quorum dev server: http://{args.host}:{args.port}/web/index.html")
    print(f"  static root : {ROOT}")
    print(f"  api         : POST /api/record   GET /api/health   GET /api/record/<id>/status")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
