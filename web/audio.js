/* Synthesized sound. No audio assets — everything here is oscillators and
 * envelopes, which keeps the whole app dependency-free and byte-cheap.
 *
 * Restraint is the design: a blip per agent activation, a quiet tick per tool
 * call, a drone that tightens through the debate, one chord at the verdict.
 * Nothing on token deltas — those fire dozens of times a second and would turn
 * into a buzzsaw.
 *
 * Pitches sit on a minor pentatonic so no two simultaneous events can collide
 * into something sour, whatever order they arrive in.
 */

// Minor pentatonic on A. Groups are pitched by depth in the pipeline: analysts
// high and bright, the portfolio manager low and final.
const GROUP_HZ = {
  vendors:   1318.5,
  analysts:   880.0,
  research:   587.3,
  trading:    659.3,
  risk:       440.0,
  portfolio:  329.6,
};

// Verdict chords, chosen so the resolution matches the call's sentiment.
const CHORDS = {
  Buy:         [329.6, 415.3, 493.9, 659.3],  // major-ish, open
  Overweight:  [329.6, 415.3, 493.9],         // major triad
  Hold:        [329.6, 440.0, 493.9],         // sus2, unresolved
  Underweight: [329.6, 392.0, 493.9],         // minor triad
  Sell:        [329.6, 392.0, 466.2],         // minor, flat top
};

export class Sound {
  constructor() {
    this.ctx = null;
    this.enabled = true;
    this.tension = 0;
    this._drone = null;
    this._lastTick = 0;
  }

  get ready() { return this.ctx !== null && this.ctx.state === 'running'; }

  /** Browsers require a user gesture before audio starts. Safe to call often. */
  resume() {
    if (!this.ctx) {
      const AC = window.AudioContext ?? window.webkitAudioContext;
      if (!AC) return;
      this.ctx = new AC();
      this.master = this.ctx.createGain();
      this.master.gain.value = this.enabled ? 0.3 : 0;
      // Keeps a burst of simultaneous events from clipping.
      const comp = this.ctx.createDynamicsCompressor();
      comp.threshold.value = -18;
      comp.ratio.value = 6;
      this.master.connect(comp).connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
  }

  setEnabled(on) {
    this.enabled = on;
    if (this.master) {
      this.master.gain.setTargetAtTime(on ? 0.3 : 0, this.ctx.currentTime, 0.05);
    }
  }

  /** One short pitched blip. The workhorse. */
  _blip(hz, { dur = 0.22, gain = 0.5, type = 'triangle', detune = 0 } = {}) {
    if (!this.ready || !this.enabled) return;
    const t = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const env = this.ctx.createGain();
    osc.type = type;
    osc.frequency.value = hz;
    osc.detune.value = detune;
    // Fast attack, exponential decay — reads as a struck object rather than a beep.
    env.gain.setValueAtTime(0.0001, t);
    env.gain.exponentialRampToValueAtTime(gain, t + 0.008);
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(env).connect(this.master);
    osc.start(t);
    osc.stop(t + dur + 0.02);
  }

  activate(group) {
    const hz = GROUP_HZ[group] ?? 440;
    this._blip(hz, { dur: 0.3, gain: 0.34 });
    this._blip(hz * 2, { dur: 0.12, gain: 0.1, type: 'sine' });
  }

  tool() {
    // Throttled: a burst of tool calls should read as texture, not machine-gun.
    const now = performance.now();
    if (now - this._lastTick < 70) return;
    this._lastTick = now;
    this._blip(GROUP_HZ.vendors, { dur: 0.06, gain: 0.055, type: 'sine' });
  }

  flow(kind) {
    if (kind === 'rebuttal') this._blip(587.3, { dur: 0.14, gain: 0.12, type: 'sawtooth', detune: -8 });
    if (kind === 'verdict')  this._blip(261.6, { dur: 0.5, gain: 0.2, type: 'triangle' });
  }

  /** Low bed that rises and sharpens as the debate escalates. */
  setTension(v) {
    this.tension = Math.max(0, Math.min(1, v));
    if (!this.ready || !this.enabled) return;
    const t = this.ctx.currentTime;

    if (!this._drone && this.tension > 0) {
      const a = this.ctx.createOscillator();
      const b = this.ctx.createOscillator();
      const filter = this.ctx.createBiquadFilter();
      const gain = this.ctx.createGain();
      a.type = b.type = 'sawtooth';
      a.frequency.value = 55;
      b.frequency.value = 55;
      b.detune.value = 11;           // slow beating — unease without dissonance
      filter.type = 'lowpass';
      filter.frequency.value = 200;
      filter.Q.value = 4;
      gain.gain.value = 0.0001;
      a.connect(filter); b.connect(filter);
      filter.connect(gain).connect(this.master);
      a.start(); b.start();
      this._drone = { a, b, filter, gain };
    }
    if (!this._drone) return;

    // Both loudness and brightness track tension; the filter opening is what
    // actually communicates "this is getting heated".
    this._drone.gain.gain.setTargetAtTime(this.tension * 0.11, t, 0.4);
    this._drone.filter.frequency.setTargetAtTime(200 + this.tension * 900, t, 0.4);
  }

  verdict(rating) {
    if (!this.ready || !this.enabled) return;
    this.setTension(0);
    const chord = CHORDS[rating] ?? CHORDS.Hold;
    chord.forEach((hz, i) => {
      setTimeout(() => this._blip(hz, { dur: 2.4, gain: 0.16, type: 'triangle' }), i * 90);
    });
  }

  /** Kill everything — used on reset/scrub so a seek doesn't leave a drone
   *  humming from a phase that is no longer on screen. */
  reset() {
    this.setTension(0);
    if (this._drone && this.ready) {
      const t = this.ctx.currentTime;
      this._drone.gain.gain.setTargetAtTime(0.0001, t, 0.08);
    }
  }
}
