/* living-picture runtime, part 2: the world the picture lives in.
 * One wind, one light, one camera, and named channels that carry "what a
 * living thing is doing right now". Everything that moves reads from these,
 * which is why things move together and for a reason. */

/* ---------- wind: a light breeze plus gusts that travel across the picture ---------- */
LP.Wind = function (cfg, size, rng) {
  cfg = cfg || {};
  this.size = size; this.rng = rng;
  var d = cfg.dir || [1, 0], n = Math.hypot(d[0], d[1]) || 1;
  this.dir = [d[0] / n, d[1] / n];
  this.breeze = cfg.breeze == null ? 0.12 : cfg.breeze;
  var g = cfg.gust === false ? null : (cfg.gust || {});
  this.g = g && {
    every: g.every || [5, 11], strength: g.strength || [0.55, 1], speed: g.speed || 0.55,
    length: g.length || [1.2, 2.6], first: g.first == null ? [1.0, 2.2] : g.first, spread: g.spread == null ? 0.2 : g.spread
  };
  this.pointerGain = cfg.pointer == null ? 0.8 : cfg.pointer;
  // a picture painted in the middle of a gust begins with that gust already blowing everywhere, then dying away
  var op = cfg.opening;
  this.opening = op ? { strength: op.strength == null ? 0.8 : op.strength, length: op.length == null ? 2.2 : op.length, fade: op.fade || 2.2 } : null;
  this.gusts = []; this.puffs = []; this.t = 0;
  this.next = this.g ? rng.pick(this.g.first) : Infinity;
  this.listeners = [];
};
LP.Wind.prototype.gust = function (o) {
  o = o || {};
  var W = this.size[0], H = this.size[1], rng = this.rng, g = this.g || { strength: [0.6, 1], speed: 0.55, length: [1.2, 2.6], spread: 0.2 };
  var ang = Math.atan2(this.dir[1], this.dir[0]) + (o.turn != null ? o.turn : (rng() - 0.5) * 2 * g.spread);
  var dir = o.dir ? o.dir : [Math.cos(ang), Math.sin(ang)];
  // start just upwind of the frame so the front is seen crossing it
  var s0 = Math.min(0, dir[0] * W, dir[1] * H, dir[0] * W + dir[1] * H) - W * 0.08;
  var gu = { t0: this.t, dir: dir, s0: s0, speed: (o.speed || g.speed) * W, strength: o.strength || rng.pick(g.strength),
             length: o.length || rng.pick(g.length), seed: rng() * 100 };
  this.gusts.push(gu);
  for (var i = 0; i < this.listeners.length; i++) this.listeners[i](gu);
  return gu;
};
/* a local puff, e.g. the pointer sweeping past like someone walking by */
LP.Wind.prototype.puff = function (x, y, vx, vy, strength) {
  var n = Math.hypot(vx, vy) || 1;
  this.puffs.push({ t0: this.t, x: x, y: y, dir: [vx / n, vy / n], strength: strength, r: this.size[0] * 0.22, seed: this.rng() * 100 });
  if (this.puffs.length > 6) this.puffs.shift();
};
LP.Wind.prototype.step = function (t) {
  this.t = t;
  if (t >= this.next) { this.gust(); this.next = t + this.rng.gap(this.g.every, this); }
  var W = this.size[0];
  this.gusts = this.gusts.filter(function (g) { return t - g.t0 < g.length + 4 + 2.2 * W / g.speed; });
  this.puffs = this.puffs.filter(function (p) { return t - p.t0 < 3; });
};
/* wind vector at a point: [x, y], length is strength (about 0..1.3) */
LP.Wind.prototype.at = function (x, y) {
  var t = this.t, W = this.size[0];
  var b = this.breeze * (0.55 + 0.9 * LP.noise2(t * 0.23 + x / W * 0.8, y / W * 0.8 + 3.1));
  var wx = this.dir[0] * b, wy = this.dir[1] * b;
  if (this.opening && t < this.opening.length + this.opening.fade) {
    var oe = this.opening.strength * (1 - LP.smooth(this.opening.length, this.opening.length + this.opening.fade, t));
    oe *= 1 - 0.22 * LP.smooth(0, 0.8, t) * LP.noise2(t * 1.9 + 5.5, x / W * 2.3);
    wx += this.dir[0] * oe; wy += this.dir[1] * oe;
  }
  for (var i = 0; i < this.gusts.length; i++) {
    var g = this.gusts[i];
    var tau = (t - g.t0) - (x * g.dir[0] + y * g.dir[1] - g.s0) / g.speed;   // seconds since the front reached this point
    if (tau <= 0) continue;
    var env = LP.smooth(0, 0.32, tau) * (1 - LP.smooth(g.length, g.length + 1.4, tau));
    if (env <= 0) continue;
    var across = (-x * g.dir[1] + y * g.dir[0]) / W;
    var turb = 0.72 + 0.56 * LP.noise2(tau * 1.9 + g.seed, across * 2.3 + g.seed);
    var s = g.strength * env * turb;
    wx += g.dir[0] * s; wy += g.dir[1] * s;
  }
  for (var j = 0; j < this.puffs.length; j++) {
    var p = this.puffs[j], age = t - p.t0;
    var dx = x - (p.x + p.dir[0] * age * W * 0.25), dy = y - (p.y + p.dir[1] * age * W * 0.25);
    var k = Math.exp(-(dx * dx + dy * dy) / (2 * p.r * p.r)) * LP.smooth(0, 0.15, age) * (1 - LP.smooth(0.5, 2.6, age)) * p.strength;
    wx += p.dir[0] * k; wy += p.dir[1] * k;
  }
  return [wx, wy];
};

/* ---------- channels: named values that describe what something is doing ---------- */
LP.Channels = function (defs, world) {
  this.world = world; this.v = {}; this.list = [];
  defs = defs || {};
  for (var name in defs) if (Object.prototype.hasOwnProperty.call(defs, name)) this.add(name, defs[name]);
};
LP.Channels.prototype.add = function (name, def) {
  var rng = this.world.rng, c = { name: name, def: def, type: def.type || 'wander', value: def.start || 0, state: {} };
  var st = c.state;
  if (c.type === 'wander') {
    st.target = def.start || 0; st.next = rng.pick(def.first || def.every || [2, 6]);
    st.sp = new LP.Spring(def.freq || 0.7, def.damp == null ? 0.85 : def.damp); st.sp.x = st.target; st.moved = -9;
  } else if (c.type === 'pulse' || c.type === 'blink') {
    st.next = rng.pick(def.first || def.every || [2, 6]); st.t0 = -99; st.amp = 0; st.seen = 0; st.above = false;
  } else if (c.type === 'follow') {
    st.sp = new LP.Spring(def.freq || 2, def.damp == null ? 0.3 : def.damp); st.buf = []; st.extra = null;
    if (def.plus && typeof def.plus === 'object' && !Array.isArray(def.plus)) { this.add(name + '+', def.plus); st.extra = name + '+'; }
  } else if (c.type === 'osc') {
    st.phase = def.phase || 0; st.period = rng.pick(def.period || [3.2, 4.4]);
  } else if (c.type === 'wind') {
    st.sp = new LP.Spring(def.freq || 1.2, def.damp == null ? 0.3 : def.damp);
  } else if (c.type === 'pointer') {
    st.sp = new LP.Spring(def.freq || 1.4, 0.9);
  }
  this.v[name] = c.value; this.list.push(c);
};
LP.Channels.prototype.get = function (ref, fallback) {
  if (ref == null) return fallback || 0;
  if (typeof ref === 'number') return ref;
  if (typeof ref === 'string') return this.v[ref] || 0;
  if (typeof ref === 'function') return ref(this.world);
  var v = this.get(ref.ch, 0) * (ref.gain == null ? 1 : ref.gain) + (ref.add || 0);
  if (ref.plus) v += this.get(ref.plus);
  return v;
};
LP.Channels.prototype.step = function (t, dt) {
  var rng = this.world.rng, self = this, world = this.world;
  for (var i = 0; i < this.list.length; i++) {
    var c = this.list[i], d = c.def, st = c.state, v = 0;
    if (d.on && (c.type === 'pulse' || c.type === 'blink')) {       // answer the visitor: a touch, or coming near
      var fireNow = false;
      if (d.on === 'tap') { var lt = world.lastPoke == null ? -1 : world.lastPoke; if (lt > (st.evSeen == null ? -1 : st.evSeen)) { st.evSeen = lt; fireNow = true; } }
      else if (d.on === 'approach' && d.at) {
        var P0 = world.pointer, near = P0.active > 0.5 && Math.hypot(P0.x - d.at[0], P0.y - d.at[1]) < (d.radius || world.size[0] * 0.25);
        if (near && !st.near) fireNow = true;
        st.near = near;
      }
      if (fireNow && t - st.t0 > (d.refractory || 1.5)) this.fire(c.name);
    }
    switch (c.type) {
      case 'wander':                     // go somewhere, settle, stay a while, go again (or come home)
        if (t >= st.next) {
          var home = d.home == null ? 0.35 : d.home, r = d.range || [-1, 1];
          st.target = rng() < home ? (d.rest || 0) : rng.range(r[0], r[1]);
          st.next = t + rng.gap(d.every || [3, 8], st); st.moved = t;
        }
        v = st.sp.step(st.target, dt);
        break;
      case 'pulse':                      // a brief action: twitch, flick, start
        if (d.when) {
          var w = this.world.wind.at(d.when.at[0], d.when.at[1]), ws = Math.hypot(w[0], w[1]);
          var above = ws > (d.when.wind || 0.45);
          if (above && !st.above && t - st.t0 > (d.refractory || 1.5)) { st.t0 = t + (d.delay || 0); st.amp = rng.pick(d.amp || 1) * (d.sign === 'random' && rng() < 0.5 ? -1 : 1); st.left = d.repeat ? Math.floor(rng.pick(d.repeat)) : 0; }
          st.above = above;
        } else if (t >= st.next && (d.every || !d.on)) {
          st.t0 = t; st.amp = rng.pick(d.amp || 1) * (d.sign === 'random' && rng() < 0.5 ? -1 : 1);
          st.next = t + rng.gap(d.every || [4, 10], st);
          st.left = d.repeat ? Math.floor(rng.pick(d.repeat)) : 0;
        }
        var a = d.attack || 0.08, h = d.hold || 0.05, rl = d.release || 0.3, e = t - st.t0;
        if (e >= 0 && e < a + h + rl) {
          v = st.amp * (e < a ? LP.ease(e / a) : (e < a + h ? 1 : 1 - LP.ease((e - a - h) / rl)));
        } else if (st.left > 0 && e >= a + h + rl + (d.gap || 0.08)) { st.left--; st.t0 = t; }
        break;
      case 'blink': {                    // 0 open .. 1 closed; quick down, slower up
        var src = d['with'] ? this._find(d['with']) : null;
        // a blink goes with a shift of gaze, but never on top of one that has just happened
        if (src && src.state.moved > st.seen) { st.seen = src.state.moved; if (t - st.t0 > 1.2 && rng() < (d.chance == null ? 0.7 : d.chance)) st.next = Math.min(st.next, t + 0.05); }
        if (t >= st.next) {
          st.t0 = t; var mean = d.every || [2.5, 7];
          st.next = t + rng.gap(mean, st);
          st.dbl = rng() < (d['double'] == null ? 0.12 : d['double']);
        }
        var cl = d.close || 0.085, ho = d.hold || 0.045, op = d.open || 0.17, eb = t - st.t0;
        if (st.dbl && eb > cl + ho + op + 0.09) { st.t0 = t; st.dbl = false; eb = 0; }
        v = eb < 0 ? 0 : (eb < cl ? LP.ease(eb / cl) : (eb < cl + ho ? 1 : (eb < cl + ho + op ? 1 - LP.ease((eb - cl - ho) / op) : 0)));
        break;
      }
      case 'follow': {                   // trails another channel with weight: overshoots and settles
        var s = this.get(d.of) * (d.gain == null ? 1 : d.gain);
        if (d.delay) { st.buf.push(s); if (st.buf.length > Math.round(d.delay * 60)) s = st.buf.shift(); else s = st.buf[0]; }
        v = st.sp.step(s, dt) + (st.extra ? this.v[st.extra] : 0);
        break;
      }
      case 'osc': {                      // breathing: in a little faster than out, never an exact period
        st.phase += dt / st.period;
        if (st.phase >= 1) { st.phase -= 1; st.period = rng.pick(d.period || [3.2, 4.4]); }
        var ph = st.phase, inh = d.inhale || 0.42;
        v = (ph < inh ? LP.ease(ph / inh) : 1 - LP.ease((ph - inh) / (1 - inh))) * (d.amp == null ? 1 : d.amp);
        break;
      }
      case 'wind': {                     // how a thing at a place answers the wind
        var wv = this.world.wind.at(d.at[0], d.at[1]);
        var drive = d.axis === 'y' ? wv[1] : (d.axis === 'abs' ? Math.hypot(wv[0], wv[1]) : wv[0]);
        v = st.sp.step(drive * (d.gain == null ? 1 : d.gain), dt);
        break;
      }
      case 'noise':
        v = LP.wander(t * (d.rate || 0.3), d.seed || i) * (d.amp == null ? 1 : d.amp);
        break;
      case 'pointer': {                  // notice the visitor: -1..1 toward where the pointer is
        var P = this.world.pointer, tg = 0;
        if (P.active > 0.01) {
          var dx = P.x - d.at[0], dy = P.y - d.at[1], R = d.radius || this.world.size[0] * 0.6;
          var val = d.axis === 'y' ? dy : dx;
          tg = LP.clamp(val / R, -1, 1) * P.active * (d.amp == null ? 1 : d.amp);
        }
        v = st.sp.step(tg, dt);
        break;
      }
      case 'const': v = d.value || 0; break;
    }
    if (Array.isArray(d.plus)) for (var k = 0; k < d.plus.length; k++) v += this.get(d.plus[k]);
    else if (typeof d.plus === 'string') v += this.get(d.plus);
    if (d.times != null) v *= this.get(d.times);
    if (t < 1.1) v *= LP.smooth(0, 1.1, t);       // everything starts from the painted state
    c.value = v; this.v[c.name] = v;
  }
};
/* make a pulse or a blink happen now (from a hook, or from the `on` triggers) */
LP.Channels.prototype.fire = function (name, amp) {
  var c = this._find(name); if (!c) return;
  var d = c.def, st = c.state, rng = this.world.rng, t = this.world.time;
  if (c.type === 'blink') { st.next = t; }
  else if (c.type === 'pulse') { st.t0 = t + (d.delay || 0); st.amp = amp != null ? amp : rng.pick(d.amp || 1); st.left = d.repeat ? Math.floor(rng.pick(d.repeat)) : 0; if (d.every) st.next = t + rng.pick(d.every); }
  else if (c.type === 'wander') { st.next = t; }
};
LP.Channels.prototype._find = function (name) {
  for (var i = 0; i < this.list.length; i++) if (this.list[i].name === name) return this.list[i];
  return null;
};

/* ---------- camera: a small change of viewpoint, so depth can be felt ---------- */
LP.Camera = function (cfg) {
  cfg = cfg || {};
  this.travel = cfg.travel == null ? 0.022 : cfg.travel;     // near-to-far slide, as a fraction of picture width
  this.focus = cfg.focus == null ? 0.5 : cfg.focus;          // the depth that stays put
  this.drift = cfg.drift == null ? 0.55 : cfg.drift;         // idle wandering, so space shows without a pointer
  this.pointerGain = cfg.pointer == null ? 1 : cfg.pointer;
  this.scrollGain = cfg.scroll == null ? 0.5 : cfg.scroll;
  this.rate = cfg.rate || 0.055;
  // "hold": the picture's own border stays where it is and the shift grows inward from it, so nothing
  // from beyond the edge is ever shown. "mirror": the whole plane shifts and mirrored paint fills the gap.
  this.edge = cfg.edge || 'hold'; this.hold = cfg.hold == null ? 0.07 : cfg.hold;
  this.x = 0; this.y = 0; this.sx = new LP.Spring(cfg.freq || 0.9, 1); this.sy = new LP.Spring(cfg.freq || 0.9, 1);
};
LP.Camera.prototype.step = function (t, dt, pointer, scroll) {
  var px = 0, py = 0;
  if (this.drift) {
    var ease = LP.smooth(0, 5, t);            // the view starts where the picture was painted and only then begins to wander
    px = LP.wander(t * this.rate, 1.7) * this.drift * ease;
    py = LP.wander(t * this.rate * 0.8, 8.3) * this.drift * 0.45 * ease;
  }
  var a = pointer.active * this.pointerGain;
  px = px * (1 - Math.min(1, a) * 0.8) + pointer.nx * a;
  py = py * (1 - Math.min(1, a) * 0.8) + pointer.ny * a * 0.7;
  py += scroll * this.scrollGain;
  this.x = this.sx.step(LP.clamp(px, -1.2, 1.2), dt);
  this.y = this.sy.step(LP.clamp(py, -1.2, 1.2), dt);
};

/* ---------- light: passing shade, exposure, flashes, lights inside the picture ---------- */
LP.Light = function (cfg, size, rng) {
  cfg = cfg || {};
  this.size = size; this.cfg = cfg;
  var s = cfg.shade;
  this.shade = s ? {
    amount: s.amount == null ? 0.32 : s.amount, scale: s.scale || size[0] * 0.9,
    dir: s.dir || [1, 0.15], speed: s.speed == null ? 0.06 : s.speed,
    cover: s.cover || [0.4, 0.62], rate: s.rate || 0.05, soft: s.soft == null ? 0.16 : s.soft,
    horizon: s.horizon == null ? 0 : s.horizon, near: s.near == null ? 0 : s.near,
    tint: s.tint || [0.7, 0.74, 0.86], seed: rng() * 50, ox: 0, oy: 0, coverNow: 0.5
  } : null;
  this.glows = (cfg.glows || []).slice(0, 4);
  this.pass = this.glows.map(function (g) {      // a light or shadow that crosses the picture now and then
    return g.sweep ? { next: rng.pick(g.sweep.first || g.sweep.every || [6, 14]), t0: -99, dur: 1, u: -1 } : null;
  });
  this.rng = rng;
  this.gain = [1, 1, 1]; this.lift = [0, 0, 0];
};
/* start a passing light or shadow now (e.g. when the visitor sweeps across) */
LP.Light.prototype.passNow = function (i) {
  var ps = this.pass[i || 0]; if (ps && ps.u < 0) ps.next = 0;
};
LP.Light.prototype.step = function (t, dt, ch) {
  var s = this.shade, c = this.cfg;
  if (s) {
    s.ox += s.dir[0] * s.speed * dt; s.oy += s.dir[1] * s.speed * dt;
    // coverage wanders: sometimes clear sun, sometimes a cloud takes a while to pass
    s.coverNow = LP.lerp(s.cover[1], s.cover[0], LP.noise1(t * s.rate + s.seed));
  }
  for (var i = 0; i < this.glows.length; i++) {
    var ps = this.pass[i]; if (!ps) continue;
    var sw = this.glows[i].sweep;
    if (t >= ps.next && ps.u < 0) { ps.t0 = t; ps.dur = this.rng.pick(sw.duration || [5, 8]); ps.next = t + ps.dur + this.rng.gap(sw.every || [20, 45], ps); }
    ps.u = (t - ps.t0) / ps.dur; if (ps.u > 1 || ps.u < 0) ps.u = -1;
  }
  var e = c.exposure == null ? 1 : ch.get(c.exposure, 1);
  var tint = c.tint || [1, 1, 1], warm = c.warmth == null ? 0 : ch.get(c.warmth);
  this.gain[0] = e * tint[0] * (1 + warm * 0.06); this.gain[1] = e * tint[1]; this.gain[2] = e * tint[2] * (1 - warm * 0.08);
  var f = c.flash == null ? 0 : ch.get(c.flash), fc = c.flashColor || [0.85, 0.9, 1];
  this.lift[0] = f * fc[0]; this.lift[1] = f * fc[1]; this.lift[2] = f * fc[2];
};
