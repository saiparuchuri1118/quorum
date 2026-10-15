"""Tests for quorum.server -- primarily the static-file allowlist.

This module used to serve the entire quorum/ project tree, which meant
backend/.env (the Anthropic key) was fetchable over HTTP at /backend/.env --
found live, via curl, while smoke-testing the ticker-search feature. These
tests exist so that specific regression can never come back silently. See the
_STATIC_ROOTS comment in server.py for the full story.

Runs a real HTTP server on an ephemeral port against a throwaway directory
tree (not the real quorum/ root), so nothing here touches the actual .env.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quorum.server as server_mod  # noqa: E402


@pytest.fixture()
def running_server(tmp_path, monkeypatch):
    """Point the module's globals at a throwaway tree, then serve it for real.

    Patching module-level ROOT/SESSIONS/etc. rather than instantiating the
    handler class differently, because the handler methods close over those
    module globals directly (matches how the real process runs).
    """
    root = tmp_path / "quorum"
    web = root / "web"
    sessions = root / "sessions"
    backend = root / "backend"
    web.mkdir(parents=True)
    sessions.mkdir(parents=True)
    backend.mkdir(parents=True)

    (web / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (web / "main.js").write_text("console.log('hi')", encoding="utf-8")
    (sessions / "demo.jsonl").write_text('{"seq":0}\n', encoding="utf-8")
    (backend / ".env").write_text("ANTHROPIC_API_KEY=super-secret-value\n", encoding="utf-8")
    (root / "README.md").write_text("# quorum", encoding="utf-8")

    monkeypatch.setattr(server_mod, "ROOT", root)
    monkeypatch.setattr(server_mod, "BACKEND", backend)
    monkeypatch.setattr(server_mod, "SESSIONS", sessions)
    monkeypatch.setattr(server_mod, "LOGS", sessions / ".logs")
    monkeypatch.setattr(server_mod, "_active", None)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", root
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def _get(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(base, path, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# -- the regression itself ---------------------------------------------------


@pytest.mark.parametrize("path", [
    "/backend/.env",
    "/../backend/.env",
    "/web/../backend/.env",
    "/backend/quorum/server.py",
])
def test_dot_env_and_backend_source_are_never_servable(running_server, path):
    base, _ = running_server
    status, body = _get(base, path)
    assert status == 403
    assert b"super-secret-value" not in body


def test_dotfile_segments_are_rejected_anywhere_in_the_path(running_server):
    """Defense in depth beyond the top-level allowlist: sessions/.logs holds
    subprocess output and should not be web-servable either."""
    base, root = running_server
    (root / "sessions" / ".logs").mkdir()
    (root / "sessions" / ".logs" / "run.log").write_text("log", encoding="utf-8")
    status, _ = _get(base, "/sessions/.logs/run.log")
    assert status == 403


def test_readme_and_other_top_level_files_are_not_servable(running_server):
    """Only web/ and sessions/ are reachable -- not the whole project root,
    even for an innocuous file."""
    base, _ = running_server
    status, _ = _get(base, "/README.md")
    assert status == 403


# -- static serving still works for what it should ---------------------------


def test_web_assets_are_servable(running_server):
    base, _ = running_server
    status, body = _get(base, "/web/index.html")
    assert status == 200
    assert body == b"<html>app</html>"


def test_sessions_are_servable(running_server):
    base, _ = running_server
    status, body = _get(base, "/sessions/demo.jsonl")
    assert status == 200
    assert b'"seq":0' in body


def test_root_serves_the_app_without_a_redirect(running_server):
    """A redirect the client has to follow is an extra round trip for no
    reason -- root should just be the app."""
    base, _ = running_server
    status, body = _get(base, "/")
    assert status == 200
    assert body == b"<html>app</html>"


def test_missing_file_is_404_not_403(running_server):
    base, _ = running_server
    status, _ = _get(base, "/web/does-not-exist.js")
    assert status == 404


def test_head_request_does_not_500(running_server):
    base, _ = running_server
    status, body = _get(base, "/web/index.html", method="HEAD")
    assert status == 200


# -- the recording API --------------------------------------------------------


def test_health_never_echoes_the_key_value(running_server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-never-appear-in-response")
    base, _ = running_server
    status, body = _get(base, "/api/health")
    assert status == 200
    assert b"should-never-appear-in-response" not in body
    assert json.loads(body)["key_configured"] is True


def test_invalid_ticker_is_rejected_before_any_process_spawns(running_server):
    base, _ = running_server
    status, body = _post(base, "/api/record", {"ticker": "not a ticker!"})
    assert status == 400
    assert "invalid ticker" in body["error"]


def test_missing_key_is_reported_clearly(running_server, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    base, root = running_server
    # The fixture's own backend/.env (used by the exploit tests above) sets a
    # fake key via setdefault -- overwrite it so this test's key really is
    # absent, matching what "missing" means for a real user's backend/.env.
    (root / "backend" / ".env").write_text("# no key here\n", encoding="utf-8")

    status, body = _post(base, "/api/record", {"ticker": "AAPL", "provider": "anthropic"})
    assert status == 422
    assert "ANTHROPIC_API_KEY" in body["error"]


def test_concurrency_guard_rejects_a_second_run(running_server, monkeypatch):
    """Spawns a real (instant, harmless) subprocess in place of quorum.record
    so this test exercises the actual guard without making an API call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    base, _ = running_server

    import subprocess
    real_popen = subprocess.Popen

    def slow_noop(cmd, **kwargs):
        # A process that stays alive briefly, standing in for a real run.
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=kwargs.get("cwd"),
        )

    monkeypatch.setattr(server_mod.subprocess, "Popen", slow_noop)

    status1, body1 = _post(base, "/api/record", {"ticker": "AAPL"})
    assert status1 == 202
    status2, body2 = _post(base, "/api/record", {"ticker": "MSFT"})
    assert status2 == 409
    assert body2["ticker"] == "AAPL"

    # Let the stand-in process exit so the fixture's teardown is clean.
    time.sleep(2.5)
