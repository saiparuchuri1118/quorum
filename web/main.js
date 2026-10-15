/* Wiring: session -> replay -> (scene | hud).
 *
 * Every consumer is driven purely by events and can be rebuilt from a prefix of
 * the stream, which is what makes scrubbing correct rather than approximate.
 */

import { SceneView } from './scene.js';
import { Hud } from './hud.js';
import { Replay, loadSession, followSession } from './replay.js';
import { Sound } from './audio.js';
import { phaseForNode, PHASE_LABEL } from './phases.js';
import { wireSearch } from './search.js';

const sound = new Sound();

const PARAMS = new URLSearchParams(location.search);
const SESSION = PARAMS.get('session') ?? '../sessions/nvda-demo.jsonl';
// ?follow=1 tails a session file that a recorder is still writing.
const FOLLOW = PARAMS.get('follow') === '1';

const hud = new Hud();
let view, replay;

/** Everything derived from the stream. Cleared on reset, rebuilt on seek. */
const run = {
  reports: new Map(),   // report_key -> {title, body}
  buffers: new Map(),   // node id  -> streaming text
  nodeReport: new Map(),// node id  -> report_key
  calls: new Map(),     // call_id  -> originating node id
  labels: new Map(),    // node id  -> display label
  groups: new Map(),    // node id  -> visual group (drives audio pitch)
  active: null,
  phase: null,
  debateRounds: 1, riskRounds: 1,
  llm: 0, tools: 0, tin: 0, tout: 0,
};

function resetRun() {
  run.reports.clear();
  run.buffers.clear();
  run.calls.clear();
  run.active = null;
  run.phase = null;
  run.llm = run.tools = run.tin = run.tout = 0;
  view.reset();
  sound.reset();
  hud.phase(null);
  hud.clearFeed();
  hud.hideVerdict();
  hud.stats({ llm: 0, tools: 0, tin: 0, tout: 0, elapsed: 0 });
  if (!hud.pinned) hud.closePanel();
}

/** Panel follows the active node unless the user has pinned one. */
function refreshPanel() {
  const id = hud.pinned ?? run.active;
  if (!id) return;
  const label = run.labels.get(id) ?? id;
  const key = run.nodeReport.get(id);
  const finished = key && run.reports.get(key);
  if (finished) hud.showPanel(label, finished.body, false);
  else if (run.buffers.has(id)) hud.showPanel(label, run.buffers.get(id), true);
  else hud.showPanel(label, '_Waiting for output…_', false);
}

function handle(e, silent) {
  const p = e.payload;

  switch (e.type) {
    case 'run_started': {
      hud.runStarted(p);
      run.debateRounds = p.debate_rounds || 1;
      run.riskRounds = p.risk_rounds || 1;
      for (const n of p.topology.nodes) {
        run.labels.set(n.id, n.label);
        run.groups.set(n.id, n.group);
        if (n.report_key) run.nodeReport.set(n.id, n.report_key);
      }
      break;
    }

    case 'node_entered': {
      run.active = p.node;
      run.llm += 1;
      view.setNodeState(p.node, 'active', { silent });

      // Phase changes drive the camera. During a scrub we snap rather than
      // ease, since an eased move would trail the scrubber and read as lag.
      const phase = phaseForNode(p.node);
      if (phase !== run.phase) {
        run.phase = phase;
        if (silent) view.director.snap(phase, view.positions());
        else view.director.goTo(phase, view.positions());
        hud.phase(PHASE_LABEL[phase]);
      }

      if (!silent) {
        sound.activate(run.groups.get(p.node) ?? 'analysts');
        hud.feed('node', 'enter', run.labels.get(p.node) ?? p.node);
        refreshPanel();
      }
      break;
    }

    case 'node_exited':
      view.setNodeState(p.node, p.state, { silent });
      if (run.active === p.node) run.active = null;
      break;

    case 'edge_flow':
      view.flow(p.src, p.dst, p.kind, { silent });
      if (!silent && p.kind !== 'state') {
        sound.flow(p.kind);
        hud.feed(p.kind === 'verdict' ? 'verdict' : 'debate', p.kind,
          `${run.labels.get(p.src) ?? p.src} → ${run.labels.get(p.dst) ?? p.dst}`);
      }
      break;

    case 'tool_call':
      run.tools += 1;
      run.calls.set(p.call_id, p.node);
      view.toolPing(p.node, p.vendor, { silent });
      if (!silent) {
        sound.tool();
        hud.feed('tool', 'tool', `${p.tool}(${Object.values(p.args).slice(0, 2).join(', ')})`);
      }
      break;

    case 'tool_result':
      // Route the return leg by call_id rather than by whatever happens to be
      // active — with concurrent analysts those diverge.
      view.toolReturn(run.calls.get(p.call_id) ?? run.active, p.vendor, { silent });
      break;

    case 'token_delta': {
      run.buffers.set(p.node, (run.buffers.get(p.node) ?? '') + p.text);
      if (!silent && (hud.pinned ?? run.active) === p.node) refreshPanel();
      break;
    }

    case 'report_section':
      run.reports.set(p.key, { title: p.title, body: p.body });
      // The Research Manager's plan IS the ruling — collapse the contested
      // space the moment it lands.
      if (p.key === 'investment_plan' && p.rating) {
        view.resolveDebate(p.rating, { silent });
        sound.setTension(0);
      }
      if (!silent) refreshPanel();
      break;

    case 'debate_turn': {
      view.spawnClaim(p.side, { silent });
      const t = Math.min(1, p.round / run.debateRounds);
      view.setTension(t);
      if (!silent) { sound.setTension(t); hud.feed('debate', p.side, `round ${p.round}`); }
      break;
    }

    case 'risk_turn': {
      // The risk committee reuses the tension bed — same escalation, no shards,
      // since a three-way ring reads as a ring without extra objects in it.
      const t = Math.min(1, p.round / run.riskRounds) * 0.7;
      if (!silent) { sound.setTension(t); hud.feed('debate', p.side, `round ${p.round}`); }
      break;
    }

    case 'decision':
      view.setNodeState('verdict', 'complete', { silent });
      if (!silent) { sound.verdict(p.rating); hud.verdict(p); }
      break;

    case 'run_finished':
      run.llm = p.llm_calls; run.tools = p.tool_calls;
      run.tin = p.tokens_in; run.tout = p.tokens_out;
      if (!silent) hud.feed('verdict', 'done', `${p.duration.toFixed(0)}s`);
      break;

    case 'run_error':
      if (p.node) view.setNodeState(p.node, 'error', { silent });
      hud.feed('verdict', 'error', p.message);
      break;
  }

  // Token accounting is approximate mid-run; run_finished sets the true totals.
  if (e.type === 'tool_result') run.tin += Math.round(p.size / 12);
  if (e.type === 'token_delta') run.tout += Math.max(1, Math.round(p.text.length / 4));
  hud.stats({ llm: run.llm, tools: run.tools, tin: run.tin, tout: run.tout });
}

// -- transport ---------------------------------------------------------------

function wireTransport() {
  const scrub = document.getElementById('scrub');
  const playBtn = document.getElementById('btn-play');
  let dragging = false;

  playBtn.addEventListener('click', () => {
    if (replay.finished) replay.restart();
    else playBtn.textContent = replay.toggle() ? '❚❚' : '▶';
  });
  document.getElementById('btn-restart').addEventListener('click', () => {
    replay.restart();
    playBtn.textContent = '❚❚';
  });

  scrub.addEventListener('pointerdown', () => { dragging = true; replay.pause(); });
  addEventListener('pointerup', () => {
    if (dragging) { dragging = false; playBtn.textContent = '▶'; }
  });
  scrub.addEventListener('input', () => {
    replay.seek((scrub.value / 1000) * replay.duration);
    hud.clock(replay.t);
    hud.stats({ elapsed: replay.t });
    refreshPanel();
  });

  for (const b of document.querySelectorAll('.sbtn')) {
    b.addEventListener('click', () => {
      document.querySelectorAll('.sbtn').forEach((x) => x.classList.remove('is-on'));
      b.classList.add('is-on');
      replay.speed = Number(b.dataset.speed);
    });
  }

  const muteBtn = document.getElementById('btn-mute');
  muteBtn.addEventListener('click', () => {
    const on = muteBtn.getAttribute('aria-pressed') === 'true';
    muteBtn.setAttribute('aria-pressed', String(!on));
    muteBtn.textContent = on ? '♪' : '✕';
    sound.setEnabled(on);
  });

  addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); playBtn.click(); }
    if (e.code === 'KeyR') document.getElementById('btn-restart').click();
    if (e.code === 'KeyM') muteBtn.click();
  });

  // Browsers refuse to start an AudioContext without a user gesture, so the
  // first interaction of any kind unlocks it. Cheap to call repeatedly.
  const unlock = () => sound.resume();
  addEventListener('pointerdown', unlock);
  addEventListener('keydown', unlock);

  replay.onTick(() => {
    if (!dragging) scrub.value = String((replay.t / replay.duration) * 1000);
    hud.clock(replay.t);
    hud.stats({ elapsed: replay.t });
    if (replay.finished) playBtn.textContent = '▶';
  });
}

// -- boot --------------------------------------------------------------------

async function main() {
  view = new SceneView(document.getElementById('stage'));
  view.onNodeClick = (id) => {
    hud.pinned = id;
    refreshPanel();
    view.focusOn(id);
  };

  let events;
  try {
    events = await loadSession(SESSION, { lenient: FOLLOW });
  } catch (err) {
    hud.boot(
      `Could not load ${SESSION}\n\n${err.message}\n\n` +
      `Serve the project root over HTTP — opening index.html from the ` +
      `filesystem blocks fetch() and ES modules.`,
      true
    );
    return;
  }

  view.build(events[0].payload.topology);

  replay = new Replay(events);
  replay.onEvent(handle).onReset(resetRun);
  hud.scrubTicks(events, replay.duration);
  wireTransport();

  // Apply run_started immediately so metadata is on screen before playback.
  handle(events[0], true);

  hud.boot(null);

  if (FOLLOW) {
    // Live tail: apply what has already been recorded without animating it,
    // then let the recorder drive the cursor from here on.
    hud.live(true);
    replay.seek(replay.duration);
    followSession(SESSION, replay, {
      onEnd: (last) => {
        hud.live(false);
        hud.feed('verdict', 'live', last.type === 'run_error' ? 'run failed' : 'run complete');
      },
    });
  } else {
    replay.seek(0);
    replay.play();
    document.getElementById('btn-play').textContent = '❚❚';
  }

  // Single source of dt for both the replay clock and the scene, clamped so a
  // backgrounded tab doesn't resume by fast-forwarding through the whole run.
  let last = performance.now();
  (function frame(now = performance.now()) {
    requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 1 / 20);
    last = now;
    replay.tick(dt);
    view.update(dt);
  })();
}

// Debug handle: drive playback from the console or a test harness without a
// compositing surface (headless checks, CI screenshots).
globalThis.quorum = {
  step(seconds = 1 / 60, times = 1) {
    for (let i = 0; i < times; i++) { replay.tick(seconds); view.update(seconds); }
    return { t: replay.t, cursor: replay.cursor, of: replay.events.length };
  },
  get replay() { return replay; },
  get view() { return view; },
  get run() { return run; },
};

main();
wireSearch();
