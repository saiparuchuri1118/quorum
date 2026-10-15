/* Ticker search: POST /api/record, then navigate to the live view once the
 * server has accepted the job.
 *
 * Kept independent of replay/scene wiring in main.js -- this only exists when
 * the page is served by quorum.server (which owns /api/*), not when it's
 * served by a plain static file server. Failing open (a clear error message)
 * rather than throwing is deliberate: someone may still load index.html via
 * `python -m http.server`, which has no API to call.
 */

const TICKER_RE = /^[A-Za-z0-9._\-^=]{1,20}$/;

function setStatus(el, text, kind) {
  el.textContent = text;
  el.hidden = !text;
  el.className = 'search-status' + (kind ? ` is-${kind}` : '');
}

export function wireSearch() {
  const form = document.getElementById('search');
  const input = document.getElementById('search-input');
  const button = document.getElementById('search-submit');
  const status = document.getElementById('search-status');
  if (!form) return;

  const setBusy = (busy) => {
    input.disabled = busy;
    button.disabled = busy;
  };

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const ticker = input.value.trim().toUpperCase();

    if (!TICKER_RE.test(ticker)) {
      setStatus(status, 'enter a valid ticker, e.g. AAPL, 0700.HK, BTC-USD', 'error');
      return;
    }

    setBusy(true);
    setStatus(status, `starting analysis for ${ticker}…`, 'pending');

    let res, body;
    try {
      res = await fetch('/api/record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker }),
      });
      body = await res.json();
    } catch {
      setBusy(false);
      setStatus(
        status,
        'no recording API here — this page needs quorum.server, not a plain file server',
        'error'
      );
      return;
    }

    if (res.status === 202) {
      setStatus(status, `${ticker} started — this run makes real API calls and takes a few minutes…`, 'pending');
      // Small delay so the status line is readable before the page changes.
      setTimeout(() => { location.href = body.view_url; }, 900);
      return;
    }

    setBusy(false);
    if (res.status === 409) {
      setStatus(status, `already analyzing ${body.ticker} — wait for it to finish`, 'error');
    } else if (res.status === 422) {
      setStatus(status, body.error + ` (${body.fix ?? ''})`, 'error');
    } else {
      setStatus(status, body.error ?? `request failed (${res.status})`, 'error');
    }
  });
}
