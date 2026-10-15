/* DOM overlay: run metadata, stats, event feed, report panel, transport.
 *
 * All text rendering happens here. The scene never draws a glyph.
 */

const $ = (id) => document.getElementById(id);

const fmtClock = (s) => {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};
const fmtNum = (n) =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);

/* Minimal markdown for the report bodies: headings, bold, tables, paragraphs.
   Deliberately not a full parser — the corpus is our own agents' output, and a
   dependency-free 40 lines beats pulling in a library for four constructs. */
export function renderMarkdown(md) {
  const esc = (s) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (s) =>
    esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
          .replace(/`(.+?)`/g, '<code>$1</code>');

  const out = [];
  const lines = md.split('\n');
  let i = 0;
  let para = [];

  const flush = () => {
    if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; }
  };

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) { flush(); i++; continue; }

    if (/^#{1,6}\s/.test(line)) {
      flush();
      out.push(`<h2>${inline(line.replace(/^#{1,6}\s/, ''))}</h2>`);
      i++; continue;
    }

    // Table: header row, alignment row, then body until a non-pipe line.
    if (line.trim().startsWith('|') && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] ?? '')) {
      flush();
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(line);
      const align = cells(lines[i + 1]).map((a) => (a.endsWith(':') ? ' class="num"' : ''));
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) body.push(cells(lines[i++]));
      out.push(
        '<table><thead><tr>' +
        head.map((h, k) => `<th${align[k] ?? ''}>${inline(h)}</th>`).join('') +
        '</tr></thead><tbody>' +
        body.map((r) =>
          '<tr>' + r.map((c, k) => `<td${align[k] ?? ''}>${inline(c)}</td>`).join('') + '</tr>'
        ).join('') +
        '</tbody></table>'
      );
      continue;
    }

    para.push(line.trim());
    i++;
  }
  flush();
  return out.join('');
}

export class Hud {
  constructor() {
    this.feedRows = 0;
    this.pinned = null;        // node id the user clicked, or null to auto-follow
    this.panelEl = $('panel');
    this.bodyEl = $('panel-body');
    $('panel-close').addEventListener('click', () => this.closePanel());
  }

  boot(msg, isError = false) {
    const el = $('boot');
    if (msg === null) { el.classList.add('is-gone'); return; }
    el.textContent = msg;
    el.classList.toggle('is-error', isError);
  }

  runStarted(p) {
    $('meta-ticker').textContent = p.ticker;
    $('meta-date').textContent = p.trade_date;
    $('meta-models').textContent = `${p.provider} · ${p.deep_model} / ${p.quick_model}`;
    $('synthetic-badge').hidden = !p.synthetic;
  }

  stats({ elapsed, llm, tools, tin, tout }) {
    if (elapsed !== undefined) $('stat-elapsed').textContent = fmtClock(elapsed);
    if (llm !== undefined)  $('stat-llm').textContent = String(llm);
    if (tools !== undefined) $('stat-tools').textContent = String(tools);
    if (tin !== undefined)  $('stat-tin').textContent = fmtNum(tin);
    if (tout !== undefined) $('stat-tout').textContent = fmtNum(tout);
  }

  feed(kind, key, value) {
    const el = document.createElement('div');
    el.className = `feed-row is-${kind}`;
    el.innerHTML = `<span class="k">${key}</span><span class="v"></span>`;
    el.querySelector('.v').textContent = value;
    const feed = $('feed');
    feed.prepend(el);
    while (feed.children.length > 14) feed.lastChild.remove();
  }

  clearFeed() { $('feed').innerHTML = ''; }

  /** Show a report. `streaming` appends a caret and keeps the panel scrolled. */
  showPanel(title, markdown, streaming = false) {
    $('panel-title').textContent = title;
    this.bodyEl.innerHTML = renderMarkdown(markdown) + (streaming ? '<span class="caret">▊</span>' : '');
    this.panelEl.hidden = false;
    if (streaming) this.bodyEl.scrollTop = this.bodyEl.scrollHeight;
  }

  closePanel() { this.panelEl.hidden = true; this.pinned = null; }

  verdict(p) {
    $('verdict-rating').textContent = p.rating;
    $('verdict-sub').textContent =
      p.price_target ? `target ${p.price_target} · ${p.time_horizon ?? ''}`.trim() : 'final decision';
    $('verdict-card').hidden = false;
    requestAnimationFrame(() => $('verdict-card').classList.add('is-shown'));
  }

  hideVerdict() {
    $('verdict-card').classList.remove('is-shown');
    $('verdict-card').hidden = true;
  }

  clock(t) { $('clock').textContent = fmtClock(t); }

  /** Toggle the LIVE indicator shown while tailing an in-progress run. */
  live(on) {
    const el = $('live-badge');
    el.hidden = !on;
    // The transport is meaningless while the recorder owns the cursor.
    for (const id of ['btn-play', 'btn-restart', 'scrub']) {
      const c = $(id);
      if (c) c.disabled = on;
    }
  }

  /** Act title. Passing null clears it (used on reset). */
  phase(label) {
    const el = $('phase');
    el.textContent = label ?? '';
    el.classList.toggle('is-shown', Boolean(label));
  }

  scrubTicks(events, duration) {
    const wrap = $('scrub-ticks');
    wrap.innerHTML = '';
    const marks = { report_section: 'var(--analysts)', decision: 'var(--portfolio)' };
    for (const e of events) {
      const color = marks[e.type];
      if (!color) continue;
      const i = document.createElement('i');
      i.style.left = `${(e.t / duration) * 100}%`;
      i.style.background = color;
      wrap.appendChild(i);
    }
  }
}
