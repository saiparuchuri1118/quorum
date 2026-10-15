/* The 3D scene.
 *
 * Layout is derived entirely from the topology the backend sends: nodes are
 * placed by `group`, edges are curves between them. Nothing about the pipeline
 * shape is hardcoded here, so changing the graph upstream reshapes the scene.
 *
 * Text is never drawn into WebGL. Labels are DOM elements positioned by
 * CSS2DRenderer, which keeps typography crisp at every zoom level and is the
 * single biggest difference between this looking designed and looking like a
 * tech demo.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { CameraDirector } from './phases.js';

const COLORS = {
  analysts:  0x37d6ff,
  research:  0x9b8cff,
  trading:   0x38f5a8,
  risk:      0xff7a4d,
  portfolio: 0xffd76a,
  vendors:   0x4a5470,
};
// A few nodes get their own identity rather than their group's.
const NODE_COLORS = {
  bull: 0xffc247,
  bear: 0xb57cff,
  aggressive:   0xff7a4d,
  conservative: 0x5aa9ff,
  neutral:      0x9aa5bd,
};
const FLOW_COLORS = { state: 0x37d6ff, rebuttal: 0xff5ce1, verdict: 0xffd76a };

// X position per lane. The pipeline reads left to right.
const LANE = {
  vendors: -19, analysts: -12, debate: -4, judge: 1,
  trading: 6, risk: 12, portfolio: 18, verdict: 22.5,
};

const EMISSIVE = { pending: 0.05, active: 1.5, complete: 0.5, error: 0.9 };

export class SceneView {
  constructor(container) {
    this.container = container;
    this.nodes = new Map();
    this.edges = [];
    this.particles = [];
    this._pool = [];
    this.shards = [];        // claim objects accumulating in the debate space
    this.debateEdges = [];   // the bull<->bear arcs, animated by tension
    this.tension = 0;
    this.time = 0;
    this.lastUserInput = -Infinity;
    this.focus = new THREE.Vector3(2, 0, 0);
    this.onNodeClick = () => {};

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x06070d, 0.0135);
    this.scene = scene;

    const camera = new THREE.PerspectiveCamera(46, 1, 0.1, 400);
    camera.position.set(-2, 19, 46);
    this.camera = camera;

    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    container.appendChild(renderer.domElement);
    this.renderer = renderer;

    const labelRenderer = new CSS2DRenderer();
    labelRenderer.domElement.style.cssText =
      'position:absolute;inset:0;pointer-events:none;z-index:2';
    container.appendChild(labelRenderer.domElement);
    this.labelRenderer = labelRenderer;

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.minDistance = 8;
    controls.maxDistance = 110;
    controls.target.copy(this.focus);
    controls.addEventListener('start', () => {
      this.lastUserInput = this.time;
      this.director.notifyUserInput();
    });
    this.controls = controls;
    this.director = new CameraDirector(camera, controls);

    // Bloom is what makes emissive geometry read as light rather than as
    // brightly-coloured plastic. Threshold is set so only emissive surfaces
    // bloom, not the whole scene.
    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.9, 0.55, 0.14);
    composer.addPass(bloom);
    composer.addPass(new OutputPass());
    this.composer = composer;
    this.bloom = bloom;

    scene.add(new THREE.AmbientLight(0x8fa4ff, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 0.7);
    key.position.set(-14, 22, 18);
    scene.add(key);

    const grid = new THREE.GridHelper(150, 60, 0x1b2030, 0x11151f);
    grid.position.y = -9;
    grid.material.transparent = true;
    grid.material.opacity = 0.3;
    scene.add(grid);

    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    renderer.domElement.addEventListener('pointerdown', (e) => this._pick(e));

    this.resize();
    addEventListener('resize', () => this.resize());
  }

  resize() {
    const w = this.container.clientWidth || innerWidth;
    const h = this.container.clientHeight || innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
    this.labelRenderer.setSize(w, h);
    this.composer.setSize(w, h);
    this.bloom.setSize(w, h);
  }

  // -- construction -------------------------------------------------------

  /** Position every node from its group. Pure function of the topology. */
  _layout(topology) {
    const pos = new Map();
    const byGroup = (g) => topology.nodes.filter((n) => n.group === g);

    const analysts = byGroup('analysts');
    analysts.forEach((n, i) => {
      const z = (i - (analysts.length - 1) / 2) * 3.6;
      pos.set(n.id, new THREE.Vector3(LANE.analysts, 0, z));
    });

    const vendors = byGroup('vendors');
    vendors.forEach((n, i) => {
      const z = (i - (vendors.length - 1) / 2) * 4.2;
      pos.set(n.id, new THREE.Vector3(LANE.vendors, Math.sin(i * 1.7) * 2.4, z));
    });

    // The debate axis is deliberately diagonal — bull high and near, bear low
    // and far — so the exchange between them reads as motion through space
    // rather than a flat left-right swap.
    pos.set('bull', new THREE.Vector3(LANE.debate, 2.4, -3.2));
    pos.set('bear', new THREE.Vector3(LANE.debate, -2.4, 3.2));
    pos.set('research_manager', new THREE.Vector3(LANE.judge, 0, 0));
    pos.set('trader', new THREE.Vector3(LANE.trading, 0, 0));

    // Risk seats form a ring in the YZ plane: you look down the barrel of the
    // triangle as the pipeline flows toward you.
    const risk = byGroup('risk');
    risk.forEach((n, i) => {
      const a = (i / risk.length) * Math.PI * 2 - Math.PI / 2;
      pos.set(n.id, new THREE.Vector3(LANE.risk, Math.sin(a) * 3.3, Math.cos(a) * 3.3));
    });

    pos.set('portfolio_manager', new THREE.Vector3(LANE.portfolio, 0, 0));
    pos.set('verdict', new THREE.Vector3(LANE.verdict, 0, 0));
    return pos;
  }

  _geometryFor(node) {
    if (node.role === 'terminal') return new THREE.IcosahedronGeometry(1.5, 1);
    if (node.role === 'judge')    return new THREE.OctahedronGeometry(1.15, 0);
    if (node.group === 'vendors') return new THREE.TetrahedronGeometry(0.5, 0);
    if (node.group === 'risk')    return new THREE.IcosahedronGeometry(0.72, 0);
    return new THREE.IcosahedronGeometry(0.85, 0);
  }

  build(topology) {
    const pos = this._layout(topology);

    for (const node of topology.nodes) {
      const p = pos.get(node.id);
      if (!p) continue;
      const color = NODE_COLORS[node.id] ?? COLORS[node.group] ?? 0xffffff;
      const geo = this._geometryFor(node);

      const material = new THREE.MeshStandardMaterial({
        color, emissive: color, emissiveIntensity: EMISSIVE.pending,
        roughness: 0.28, metalness: 0.65, flatShading: true,
      });
      const mesh = new THREE.Mesh(geo, material);
      mesh.position.copy(p);
      mesh.userData.id = node.id;
      this.scene.add(mesh);

      // A slightly larger wireframe shell gives the nodes structure at
      // distance, where the solid core is only a few pixels.
      const wire = new THREE.LineSegments(
        new THREE.WireframeGeometry(geo),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.16 })
      );
      wire.scale.setScalar(1.35);
      mesh.add(wire);

      // Activity ring — hidden until the node runs.
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(node.role === 'terminal' ? 2.5 : 1.7, 0.028, 8, 64),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0 })
      );
      mesh.add(ring);

      const el = document.createElement('div');
      el.className = 'node-label' + (node.group === 'vendors' ? ' is-vendor' : '');
      el.textContent = node.label;
      const label = new CSS2DObject(el);
      label.position.set(0, node.role === 'terminal' ? 2.6 : 1.7, 0);
      mesh.add(label);

      this.nodes.set(node.id, {
        id: node.id, spec: node, mesh, material, wire, ring, el,
        color: new THREE.Color(color), state: 'pending',
        spin: 0.1 + Math.random() * 0.2, phase: Math.random() * Math.PI * 2,
      });
    }

    for (const edge of topology.edges) {
      const a = this.nodes.get(edge.src), b = this.nodes.get(edge.dst);
      if (!a || !b) continue;
      const curve = this._curve(a.mesh.position, b.mesh.position, edge.kind);
      const mesh = new THREE.Mesh(
        new THREE.TubeGeometry(curve, 40, 0.018, 6, false),
        new THREE.MeshBasicMaterial({
          color: edge.kind === 'debate' ? 0x6b4a9e : 0x243046,
          transparent: true, opacity: edge.kind === 'debate' ? 0.5 : 0.35,
        })
      );
      this.scene.add(mesh);
      const rec = { ...edge, curve, mesh, baseOpacity: mesh.material.opacity };
      this.edges.push(rec);
      // The bull<->bear arcs carry the debate's tension; keep them to hand.
      if (edge.kind === 'debate' && ['bull', 'bear'].includes(edge.src)
                                 && ['bull', 'bear'].includes(edge.dst)) {
        this.debateEdges.push(rec);
      }
    }

    // Vendor links are drawn faintly — present, but clearly not pipeline.
    for (const node of topology.nodes) {
      for (const v of node.vendors ?? []) {
        const a = this.nodes.get(node.id), b = this.nodes.get(`vendor:${v}`);
        if (!a || !b) continue;
        const curve = this._curve(a.mesh.position, b.mesh.position, 'vendor');
        const mesh = new THREE.Mesh(
          new THREE.TubeGeometry(curve, 24, 0.008, 5, false),
          new THREE.MeshBasicMaterial({ color: 0x1e2740, transparent: true, opacity: 0.3 })
        );
        this.scene.add(mesh);
        this.edges.push({ src: node.id, dst: `vendor:${v}`, kind: 'vendor', curve, mesh });
      }
    }
  }

  /** Bowed curve between two points. Debate edges bow hard so the two
   *  directions of an exchange are visibly distinct arcs, not one line. */
  _curve(a, b, kind) {
    const mid = a.clone().lerp(b, 0.5);
    const dir = b.clone().sub(a);
    const bow = kind === 'debate' ? 0.34 : kind === 'vendor' ? 0.1 : 0.14;
    const perp = new THREE.Vector3(-dir.z, dir.length() * 0.28, dir.x)
      .normalize()
      .multiplyScalar(dir.length() * bow);
    if (kind === 'debate') perp.applyAxisAngle(dir.clone().normalize(), Math.PI * 0.35);
    return new THREE.QuadraticBezierCurve3(a.clone(), mid.add(perp), b.clone());
  }

  // -- state --------------------------------------------------------------

  setNodeState(id, state, { silent = false } = {}) {
    const n = this.nodes.get(id);
    if (!n) return;
    n.state = state;
    n.el.classList.toggle('is-active', state === 'active');
    n.el.classList.toggle('is-complete', state === 'complete');
    // Focus follows activation even on a silent (seek) apply: `silent` means
    // "skip the animation", but where the camera looks is state, not animation.
    // Without this, scrubbing leaves the camera pointed at whatever ran last
    // before the seek.
    if (state === 'active') this.focusOn(id);
  }

  focusOn(id) {
    const n = this.nodes.get(id);
    if (n) this.focus.copy(n.mesh.position);
  }

  /** Live node positions, for the camera director's shot composition. */
  positions() {
    const m = new Map();
    for (const [id, n] of this.nodes) m.set(id, n.mesh.position);
    return m;
  }

  // -- the debate ---------------------------------------------------------

  /** Orthonormal frame around the bull->bear axis, so claims can orbit it. */
  _debateFrame() {
    const bull = this.nodes.get('bull'), bear = this.nodes.get('bear');
    if (!bull || !bear) return null;
    const a = bull.mesh.position, b = bear.mesh.position;
    const axis = b.clone().sub(a).normalize();
    let u = new THREE.Vector3(0, 1, 0).cross(axis);
    if (u.lengthSq() < 0.01) u = new THREE.Vector3(1, 0, 0).cross(axis);
    u.normalize();
    const v = axis.clone().cross(u).normalize();
    return { mid: a.clone().lerp(b, 0.5), axis, u, v, span: a.distanceTo(b) };
  }

  /**
   * One argument becomes one persistent object in the contested space.
   *
   * Claims cluster on their own side of the axis and orbit the midpoint, so the
   * debate visibly accumulates weight instead of resetting each round — that
   * accumulation is the thing a progress bar can never show.
   */
  spawnClaim(side, { silent = false } = {}) {
    const f = this._debateFrame();
    if (!f) return;
    const color = side === 'bull' ? 0xffc247 : 0xb57cff;
    const mesh = new THREE.Mesh(
      new THREE.OctahedronGeometry(0.17 + Math.random() * 0.08, 0),
      new THREE.MeshStandardMaterial({
        color, emissive: color, emissiveIntensity: 1.1,
        transparent: true, opacity: 0, flatShading: true,
        roughness: 0.3, metalness: 0.5,
      })
    );
    this.scene.add(mesh);
    const shard = {
      mesh, side,
      angle: Math.random() * Math.PI * 2,
      radius: 1.0 + Math.random() * 1.1,
      // Bull claims sit on the bull half of the axis, bear on the bear half.
      along: (side === 'bull' ? -1 : 1) * (0.12 + Math.random() * 0.3),
      spin: 0.4 + Math.random() * 0.9,
      dir: Math.random() < 0.5 ? -1 : 1,
      born: this.time,
      fate: null, fateT: 0,
    };
    this._placeShard(shard, f);
    this.shards.push(shard);
    if (silent) mesh.material.opacity = 0.9;
  }

  _placeShard(s, f) {
    const r = s.radius;
    s.mesh.position.copy(f.mid)
      .addScaledVector(f.u, Math.cos(s.angle) * r)
      .addScaledVector(f.v, Math.sin(s.angle) * r)
      .addScaledVector(f.axis, s.along * f.span);
  }

  /** 0..1 — how far into the debate we are. Drives arc colour and orbit speed. */
  setTension(v) { this.tension = Math.max(0, Math.min(1, v)); }

  /**
   * The ruling. Claims from the side that carried the argument stream into the
   * Research Manager and are absorbed; the other side's dissolve.
   *
   * `Hold` is a genuine draw — the upstream prompt reserves it for evidence that
   * is actually balanced — so neither side is absorbed and everything disperses.
   */
  resolveDebate(rating, { silent = false } = {}) {
    const winner = { Buy: 'bull', Overweight: 'bull', Underweight: 'bear', Sell: 'bear' }[rating] ?? null;
    for (const s of this.shards) {
      s.fate = winner && s.side === winner ? 'absorb' : 'dissolve';
      s.fateT = 0;
    }
    this.setTension(0);
    if (silent) this._clearShards();
  }

  _clearShards() {
    for (const s of this.shards) {
      s.mesh.geometry.dispose();
      s.mesh.material.dispose();
      this.scene.remove(s.mesh);
    }
    this.shards.length = 0;
  }

  /** Send a packet along the src->dst edge. Falls back to a direct line if the
   *  topology has no such edge, so an unexpected transition is still visible
   *  rather than silently dropped. */
  flow(src, dst, kind = 'state', { silent = false } = {}) {
    if (silent) return;
    const edge = this.edges.find((e) => e.src === src && e.dst === dst)
              ?? this.edges.find((e) => e.src === dst && e.dst === src);
    const a = this.nodes.get(src), b = this.nodes.get(dst);
    if (!a || !b) return;
    const curve = edge?.curve ?? this._curve(a.mesh.position, b.mesh.position, 'sequence');
    const reversed = edge && edge.src !== src;
    this._spawn(curve, FLOW_COLORS[kind] ?? FLOW_COLORS.state, reversed, dst);
  }

  toolPing(nodeId, vendor, { silent = false } = {}) {
    if (silent) return;
    this.flow(nodeId, `vendor:${vendor}`, 'state');
  }

  toolReturn(nodeId, vendor, { silent = false } = {}) {
    if (silent) return;
    this.flow(`vendor:${vendor}`, nodeId, 'state');
  }

  _spawn(curve, color, reversed, targetId) {
    let mesh = this._pool.pop();
    if (!mesh) {
      mesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.13, 10, 10),
        new THREE.MeshBasicMaterial({ transparent: true })
      );
      this.scene.add(mesh);
    }
    mesh.visible = true;
    mesh.material.color.setHex(color);
    mesh.material.opacity = 1;
    this.particles.push({
      mesh, curve, u: 0, reversed, targetId,
      speed: 0.55 + Math.random() * 0.25,
    });
  }

  /** Brief flash on a node — used when a packet lands. */
  impact(id) {
    const n = this.nodes.get(id);
    if (n) n.impact = 1;
  }

  reset() {
    this.focus.set(2, 0, 0);
    this.setTension(0);
    this._clearShards();
    for (const e of this.debateEdges) {
      e.mesh.material.opacity = e.baseOpacity;
      e.mesh.material.color.setHex(0x6b4a9e);
    }
    for (const n of this.nodes.values()) {
      n.state = 'pending';
      n.impact = 0;
      n.el.classList.remove('is-active', 'is-complete');
      n.material.emissiveIntensity = EMISSIVE.pending;
      n.mesh.scale.setScalar(1);
      n.ring.material.opacity = 0;
    }
    for (const p of this.particles) { p.mesh.visible = false; this._pool.push(p.mesh); }
    this.particles.length = 0;
  }

  // -- frame --------------------------------------------------------------

  update(dt) {
    this.time += dt;

    for (const n of this.nodes.values()) {
      n.mesh.rotation.y += dt * n.spin;
      n.mesh.rotation.x += dt * n.spin * 0.4;

      let target = EMISSIVE[n.state] ?? EMISSIVE.pending;
      if (n.state === 'active') {
        target *= 0.75 + 0.35 * Math.sin(this.time * 4.5 + n.phase);
        n.ring.material.opacity += (0.55 - n.ring.material.opacity) * dt * 5;
        n.ring.rotation.z += dt * 1.4;
        n.ring.rotation.y += dt * 0.7;
      } else {
        n.ring.material.opacity += (0 - n.ring.material.opacity) * dt * 4;
      }
      if (n.impact > 0) {
        target += n.impact * 2.2;
        n.impact = Math.max(0, n.impact - dt * 2.6);
      }
      n.material.emissiveIntensity += (target - n.material.emissiveIntensity) * dt * 9;

      const scale = n.state === 'active' ? 1.22 : n.state === 'complete' ? 1.06 : 1;
      n.mesh.scale.lerp(new THREE.Vector3(scale, scale, scale), dt * 5);
    }

    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.u += dt * p.speed;
      if (p.u >= 1) {
        p.mesh.visible = false;
        this._pool.push(p.mesh);
        this.particles.splice(i, 1);
        if (p.targetId) this.impact(p.targetId);
        continue;
      }
      const u = p.reversed ? 1 - p.u : p.u;
      p.curve.getPointAt(Math.max(0, Math.min(1, u)), p.mesh.position);
      // Fade in and out so packets emerge from and dissolve into their nodes.
      p.mesh.material.opacity = Math.sin(p.u * Math.PI) * 0.95 + 0.05;
      p.mesh.scale.setScalar(0.7 + Math.sin(p.u * Math.PI) * 0.6);
    }

    this._updateDebate(dt);

    // The director owns the camera during a phase move. Between moves, the
    // target drifts gently toward whatever is active — but both yield to the
    // user for a few seconds after any manual orbit.
    const directed = this.director.update(dt);
    if (!directed && this.time - this.lastUserInput > 3.5) {
      this.controls.target.lerp(this.focus, dt * 0.85);
    }
    this.controls.update();
    this.composer.render();
    this.labelRenderer.render(this.scene, this.camera);
  }

  /** Animate the contested space: orbiting claims, tensioned arcs, resolution. */
  _updateDebate(dt) {
    const hot = new THREE.Color(0xff4d8f);
    const cool = new THREE.Color(0x6b4a9e);
    for (const e of this.debateEdges) {
      e.mesh.material.color.copy(cool).lerp(hot, this.tension);
      e.mesh.material.opacity = e.baseOpacity + this.tension * 0.4;
    }

    if (!this.shards.length) return;
    const f = this._debateFrame();
    if (!f) return;
    const rm = this.nodes.get('research_manager');

    for (let i = this.shards.length - 1; i >= 0; i--) {
      const s = this.shards[i];
      s.mesh.rotation.x += dt * s.spin;
      s.mesh.rotation.y += dt * s.spin * 0.7;

      if (!s.fate) {
        // Orbit faster as the argument heats up.
        s.angle += dt * s.dir * (0.25 + this.tension * 0.7);
        this._placeShard(s, f);
        s.mesh.material.opacity = Math.min(0.9, s.mesh.material.opacity + dt * 1.6);
        continue;
      }

      s.fateT = Math.min(1, s.fateT + dt * (s.fate === 'absorb' ? 0.85 : 0.6));
      if (s.fate === 'absorb' && rm) {
        // Accelerate into the judge rather than lerping linearly — the claims
        // should look pulled in, not politely escorted.
        const e = s.fateT * s.fateT;
        s.mesh.position.lerp(rm.mesh.position, e * 0.35);
        s.mesh.scale.setScalar(Math.max(0.01, 1 - e * 0.9));
        if (s.fateT >= 1) this.impact('research_manager');
      } else {
        s.angle += dt * s.dir * 0.15;
        this._placeShard(s, f);
        s.mesh.position.addScaledVector(f.axis, (s.side === 'bull' ? -1 : 1) * s.fateT * 2.2);
        s.mesh.material.opacity = 0.9 * (1 - s.fateT);
        s.mesh.scale.setScalar(Math.max(0.01, 1 - s.fateT));
      }

      if (s.fateT >= 1) {
        s.mesh.geometry.dispose();
        s.mesh.material.dispose();
        this.scene.remove(s.mesh);
        this.shards.splice(i, 1);
      }
    }
  }

  _pick(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1
    );
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const meshes = [...this.nodes.values()].map((n) => n.mesh);
    const hit = this.raycaster.intersectObjects(meshes, false)[0];
    if (hit) this.onNodeClick(hit.object.userData.id);
  }
}
