/* Playback engine.
 *
 * A session is an ordered event list with monotonic `seq` and float `t`.
 * Playing = advance a virtual clock and dispatch everything whose t has passed.
 * Seeking = reset every consumer, then re-dispatch the prefix in `silent` mode
 * so state lands instantly without firing animations. That single mechanism
 * gives scrubbing, restart, and live-tailing for free.
 *
 * When the real backend lands, a WebSocket feeds `push()` instead and nothing
 * downstream changes.
 */

export class Replay {
  constructor(events) {
    this.events = events;
    this.duration = events.length ? events[events.length - 1].t : 0;
    this.t = 0;
    this.cursor = 0;
    this.speed = 4;
    this.playing = false;
    this.following = false;   // true while tailing a run that is still being written
    this._onEvent = [];
    this._onReset = [];
    this._onTick = [];
  }

  onEvent(fn) { this._onEvent.push(fn); return this; }
  onReset(fn) { this._onReset.push(fn); return this; }
  onTick(fn)  { this._onTick.push(fn);  return this; }

  play()   { this.playing = true; }
  pause()  { this.playing = false; }
  toggle() { this.playing = !this.playing; return this.playing; }

  get finished() { return this.cursor >= this.events.length; }

  /** Advance the clock by `dt` real seconds and dispatch anything due.
   *  In follow mode the recorder drives the cursor via push(), so the local
   *  clock must not also advance it or the two race each other. */
  tick(dt) {
    if (this.playing && !this.following && !this.finished) {
      this.t += dt * this.speed;
      while (this.cursor < this.events.length && this.events[this.cursor].t <= this.t) {
        this._dispatch(this.events[this.cursor++], false);
      }
      if (this.finished) this.playing = false;
    }
    for (const fn of this._onTick) fn(dt, this.t);
  }

  /** Jump to `t`. Rebuilds state from scratch — correct by construction. */
  seek(t) {
    this.t = Math.max(0, Math.min(t, this.duration));
    this.cursor = 0;
    for (const fn of this._onReset) fn();
    while (this.cursor < this.events.length && this.events[this.cursor].t <= this.t) {
      this._dispatch(this.events[this.cursor++], true);
    }
  }

  restart() {
    this.seek(0);
    this.play();
  }

  /** Live path: append and dispatch immediately. */
  push(event) {
    this.events.push(event);
    this.duration = event.t;
    this.t = event.t;
    this.cursor = this.events.length;
    this._dispatch(event, false);
  }

  _dispatch(event, silent) {
    for (const fn of this._onEvent) fn(event, silent);
  }
}

/** Parse a .jsonl session, validating seq continuity. A gap means a consumer
 *  would silently miss a state transition, so fail loudly instead.
 *
 *  `lenient` tolerates a truncated FINAL line only — while tailing a run that
 *  is still being written, a poll can land mid-write. A malformed line anywhere
 *  else is still an error, because that is corruption rather than a race. */
export function parseSession(text, { lenient = false } = {}) {
  const events = [];
  const lines = text.split('\n');
  let expected = 0;
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (!s) continue;
    let e;
    try {
      e = JSON.parse(s);
    } catch (err) {
      const isLastContentLine = lines.slice(i + 1).every((l) => !l.trim());
      if (lenient && isLastContentLine) break;   // partial write; next poll gets it
      throw new Error(`session line ${i + 1} is not valid JSON: ${err.message}`);
    }
    if (e.seq !== expected) {
      throw new Error(`session seq gap: expected ${expected}, got ${e.seq}`);
    }
    expected++;
    events.push(e);
  }
  if (!events.length) throw new Error('session is empty');
  if (events[0].type !== 'run_started') {
    throw new Error(`session must open with run_started, got ${events[0].type}`);
  }
  return events;
}

export async function loadSession(url, opts) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} fetching ${url}`);
  return parseSession(await res.text(), opts);
}

/**
 * Tail a session file that a recorder is still appending to.
 *
 * Polls, re-parses, and pushes only the events we have not seen. Re-reading the
 * whole file each tick is deliberately dumb: sessions are a few hundred KB, it
 * needs no server support beyond static file serving, and it cannot desync the
 * way an incremental byte-offset reader can if a write straddles a poll.
 *
 * Stops on its own when the run reaches a terminal event.
 */
export function followSession(url, replay, { intervalMs = 1200, onEnd = () => {} } = {}) {
  replay.following = true;
  replay.pause();
  let known = replay.events.length;

  const tick = async () => {
    let all;
    try {
      all = await loadSession(url, { lenient: true });
    } catch {
      return;   // mid-write or momentarily unreadable; try again next interval
    }
    for (let i = known; i < all.length; i++) replay.push(all[i]);
    known = all.length;

    const last = all[all.length - 1];
    if (last && (last.type === 'run_finished' || last.type === 'run_error')) {
      stop();
      onEnd(last);
    }
  };

  const timer = setInterval(tick, intervalMs);
  const stop = () => {
    clearInterval(timer);
    replay.following = false;
  };
  tick();
  return stop;
}
