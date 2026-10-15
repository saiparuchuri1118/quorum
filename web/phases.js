/* Phases and camera direction.
 *
 * A run moves through five acts. Each gets a deliberate shot rather than a
 * generic orbit, because the interesting thing about each act is different:
 * the analyst phase is about breadth (four agents reaching outward to vendors),
 * the debate is about opposition (two agents facing off across contested
 * space), the risk phase is about a three-way ring you want to see as a ring.
 *
 * Shots are computed from live node positions, so they follow the topology
 * rather than assuming it.
 */

import * as THREE from 'three';

export const PHASE_OF_NODE = {
  bull: 'debate', bear: 'debate', research_manager: 'debate',
  trader: 'trading',
  aggressive: 'risk', conservative: 'risk', neutral: 'risk',
  portfolio_manager: 'verdict', verdict: 'verdict',
};

export const PHASE_LABEL = {
  analysis: 'Gathering evidence',
  debate:   'Bull vs Bear',
  trading:  'Sizing the trade',
  risk:     'Risk committee',
  verdict:  'Final decision',
};

export function phaseForNode(id) {
  return PHASE_OF_NODE[id] ?? 'analysis';
}

/** Perceptually smoother than smoothstep — zero first *and* second derivative
 *  at both ends, so a move has no visible kick as it starts or settles. */
const smootherstep = (t) => t * t * t * (t * (t * 6 - 15) + 10);

/** Compose a shot per phase from the actual node positions. */
export function shotFor(phase, positions) {
  const at = (id) => positions.get(id);
  const centroid = (ids) => {
    const v = new THREE.Vector3();
    let n = 0;
    for (const id of ids) { const p = at(id); if (p) { v.add(p); n++; } }
    return n ? v.divideScalar(n) : new THREE.Vector3();
  };

  switch (phase) {
    case 'debate': {
      const bull = at('bull'), bear = at('bear');
      if (!bull || !bear) break;
      const mid = bull.clone().lerp(bear, 0.5);
      // Stand off to the side of the bull-bear axis so both are in frame and
      // the contested space between them reads as volume, not a line.
      const axis = bear.clone().sub(bull).normalize();
      const side = new THREE.Vector3(1, 0, 0).cross(axis).normalize();
      if (side.lengthSq() < 0.01) side.set(1, 0, 0);
      const pos = mid.clone()
        .add(new THREE.Vector3(9.5, 3.2, 0))
        .add(side.multiplyScalar(3.5));
      return { pos, look: mid, dur: 1.6 };
    }
    case 'trading': {
      const t = at('trader');
      if (!t) break;
      return { pos: t.clone().add(new THREE.Vector3(7, 4.5, 13)), look: t.clone(), dur: 1.3 };
    }
    case 'risk': {
      const c = centroid(['aggressive', 'conservative', 'neutral']);
      // Down the barrel: the ring reads as a ring only when you look along its
      // axis, and the pipeline flows toward the viewer.
      return { pos: c.clone().add(new THREE.Vector3(-13, 2.5, 0.5)), look: c, dur: 1.5 };
    }
    case 'verdict': {
      const c = centroid(['portfolio_manager', 'verdict']);
      return { pos: c.clone().add(new THREE.Vector3(3, 6, 21)), look: c, dur: 1.8 };
    }
    case 'analysis':
    default: {
      const c = centroid(['market', 'social', 'news', 'fundamentals']);
      return { pos: c.clone().add(new THREE.Vector3(-4, 13, 25)), look: c, dur: 1.4 };
    }
  }
  return null;
}

/**
 * Drives camera position and orbit target along a timed move.
 *
 * OrbitControls derives its spherical offset from (camera.position - target) at
 * update() time, so externally lerping both and then calling update() is stable
 * — it round-trips when the user isn't dragging. As soon as they are, their
 * input wins and the move is abandoned.
 */
export class CameraDirector {
  constructor(camera, controls) {
    this.camera = camera;
    this.controls = controls;
    this.move = null;
    this.suspendedUntil = 0;
    this.time = 0;
    this.phase = null;
  }

  notifyUserInput() {
    this.move = null;                     // hand the camera back immediately
    this.suspendedUntil = this.time + 5;  // and stay out of the way for a beat
  }

  /** Returns true if the phase actually changed. */
  goTo(phase, positions, { force = false } = {}) {
    if (phase === this.phase && !force) return false;
    this.phase = phase;
    if (this.time < this.suspendedUntil) return true;
    const shot = shotFor(phase, positions);
    if (!shot) return true;
    this.move = {
      from: this.camera.position.clone(),
      fromLook: this.controls.target.clone(),
      to: shot.pos,
      toLook: shot.look,
      t: 0,
      dur: shot.dur,
    };
    return true;
  }

  /** Snap with no animation — used when scrubbing, where an eased move would
   *  lag behind the scrub and feel broken. */
  snap(phase, positions) {
    this.phase = phase;
    const shot = shotFor(phase, positions);
    if (!shot) return;
    this.move = null;
    this.camera.position.copy(shot.pos);
    this.controls.target.copy(shot.look);
  }

  update(dt) {
    this.time += dt;
    if (!this.move) return false;
    this.move.t = Math.min(1, this.move.t + dt / this.move.dur);
    const e = smootherstep(this.move.t);
    this.camera.position.lerpVectors(this.move.from, this.move.to, e);
    this.controls.target.lerpVectors(this.move.fromLook, this.move.toLook, e);
    if (this.move.t >= 1) this.move = null;
    return true;
  }

  get active() { return this.move !== null; }
}
