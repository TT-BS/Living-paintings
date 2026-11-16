/* living-picture: mount, deterministic rendering and lifecycle. */
LP.loadImage = function (src, signal) {
  return new Promise(function (ok, fail) {
    var im = new Image();
    if (!/^data:/.test(src)) im.crossOrigin = 'anonymous';
    function clean() { im.onload = im.onerror = null; if (signal) signal.removeEventListener('abort', abort); }
    function abort() { clean(); im.src = ''; var e = new Error('living-picture: image load cancelled'); e.name = 'AbortError'; fail(e); }
    im.onload = function () { clean(); ok(im); };
    im.onerror = function () { clean(); fail(new Error('living-picture: could not load ' + String(src).slice(0, 80))); };
    if (signal && signal.aborted) { abort(); return; }
    if (signal) signal.addEventListener('abort', abort, { once: true });
    im.src = src;
  });
};
LP.instances = [];
/* Opt-in authored quiet windows. A declaration is not an inferred pose detector. */
LP.restContract = function (scene) {
  if (scene.rest == null) return [];
  if (!scene.rest || !Array.isArray(scene.rest.windows) || typeof scene.restCheck !== 'function')
    throw new Error('living-picture: rest needs windows and a deterministic restCheck hook');
  var end = -1;
  return scene.rest.windows.map(function (w, i) {
    if (!Array.isArray(w) || w.length !== 2 || !w.every(Number.isFinite) || w[0] < 0 || w[1] <= w[0] || w[0] < end || w[1] - w[0] > 10 + 1e-9 ||
        w.some(function (t) { return Math.abs(t * 60 - Math.round(t * 60)) > 1e-7; }))
      throw new Error('living-picture: rest windows must be ordered non-overlapping 1/60-second boundaries, each at most 10 seconds');
    end = w[1]; return { start: w[0], end: w[1], index: i };
  });
};
LP.mount = function (target, scene, opts) {
  opts = opts || {};
  var el = typeof target === 'string' ? document.querySelector(target) : target;
  if (!el) throw new Error('living-picture: no such element');
  var reg = root.LivingPictureHooks;
  if (scene.hooksId && reg && reg[scene.hooksId]) { var hk = reg[scene.hooksId]; for (var hn in hk) if (typeof hk[hn] === 'function') scene[hn] = hk[hn]; }
  var order = LP.layerOrder(scene.layers);
  LP.fixedContract(scene);
  var restWindows = LP.restContract(scene);
  if (el.__livingPicture) el.__livingPicture.destroy();
  var W = scene.size[0], H = scene.size[1];
  var canvas = el.tagName === 'CANVAS' ? el : document.createElement('canvas');
  var box = canvas === el ? el.parentNode : el;
  var fallback = box && box.querySelector ? box.querySelector('img') : null;
  if (!fallback && (opts.original || scene.original)) {
    fallback = document.createElement('img');
    var originalSrc = opts.original || scene.original;
    fallback.src = opts.assets && opts.assets[originalSrc] || (opts.original ? opts.original : (opts.base || '') + scene.original);
    fallback.alt = opts.alt || ''; fallback.style.cssText = 'display:block;width:100%;height:auto';
    if (canvas === el) box.insertBefore(fallback, canvas); else box.appendChild(fallback);
  }
  if (!fallback) throw new Error('living-picture: provide the original fallback img, or options.original');
  var fallbackVisibility = fallback.style.visibility || '';
  if (canvas !== el) {
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    if (!el.style.aspectRatio && !(el.clientHeight > 4)) el.style.aspectRatio = W + ' / ' + H;
    canvas.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;display:block;opacity:0';
    canvas.setAttribute('aria-hidden', 'true'); el.appendChild(canvas);
  }
  var mq = opts.motion !== 'force' && typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
  var world = {
    size: [W, H], scene: scene, rng: LP.rng(scene.seed || 1), layers: [], byId: {}, order: [], time: 0,
    pointer: { x: W / 2, y: H / 2, nx: 0, ny: 0, active: 0, want: 0, last: -9, vx: 0, vy: 0 },
    ripples: [], scroll: 0, events: []
  };
  var inst = { world: world, canvas: canvas, fallback: fallback, playing: false, ready: null, reduced: !!(mq && mq.matches), scripted: false, disposed: false, error: null, renders: 0, resting: false, restUntil: null, restChecks: 0, restDeclines: 0 };
  LP.instances.push(inst); el.__livingPicture = inst;
  var G = null, raf = 0, lastNow = 0, acc = 0, visible = !document.hidden, onscreen = true, userPaused = opts.autoplay === false, btn = null;
  var generation = 0, pending = null, controller = null, removers = [], lost = false, io = null;
  var STEP = 1 / 60;
  var sleep = null, restTimer = 0, restTicket = 0, restBlockedUntil = -1;
  function listen(target, name, fn, options) { target.addEventListener(name, fn, options); removers.push(function () { target.removeEventListener(name, fn, options); }); }
  function stop() { inst.playing = false; if (raf) cancelAnimationFrame(raf); raf = 0; }
  function still() { canvas.style.transition = 'none'; canvas.style.opacity = '0'; fallback.style.visibility = fallbackVisibility; }
  function showMotion() { canvas.style.opacity = '1'; fallback.style.visibility = 'hidden'; }
  function restAt(t) {
    for (var i = 0; i < restWindows.length; i++) if (t + 1e-9 >= restWindows[i].start && t < restWindows[i].end - 1e-9) return restWindows[i];
    return null;
  }
  function clearRest() {
    restTicket++; if (restTimer) clearTimeout(restTimer); restTimer = 0; sleep = null; inst.resting = false; inst.restUntil = null;
  }
  function checkRest(w, strict) {
    inst.restChecks++;
    var ok = scene.restCheck(world, LP);
    if (ok !== true && ok !== false) throw new Error('living-picture: restCheck must return a boolean');
    if (!ok) {
      if (strict) throw new Error('living-picture: declared rest window ' + w.index + ' failed at t=' + world.time.toFixed(6));
      inst.restDeclines++;
    }
    return ok;
  }
  function eligibleRest() { return world.time + 1e-9 >= restBlockedUntil ? restAt(world.time) : null; }
  // Replay only simulation steps, never render a hidden gap. Each sleep has <=600
  // steps. This preserves the existing RNG/events/state; it is not analytic skipping.
  function settleRest(toEnd) {
    if (!sleep) return;
    var s = sleep, elapsed = Math.max(0, (performance.now() - s.wall) / 1000);
    var n = toEnd ? Math.round((s.window.end - world.time) / STEP) : Math.min(Math.floor((elapsed + 1e-9) / STEP), Math.round((s.window.end - world.time) / STEP));
    clearRest();
    if (n > 600) throw new Error('living-picture: rest catch-up exceeded 600 steps');
    for (var i = 0; i < n; i++) { step(); if (world.time < s.window.end - 1e-9) checkRest(s.window, true); }
  }
  function tryRest() {
    var w = eligibleRest();
    if (!w || inst.disposed || inst.reduced || lost || inst.error || userPaused || !visible || !onscreen || !G || inst.scripted) return false;
    if (sleep) return true;
    if (!checkRest(w, false)) return false;
    stop(); still(); inst.resting = true; inst.restUntil = w.end; acc = 0;
    sleep = { window: w, wall: performance.now() };
    var ticket = ++restTicket;
    restTimer = setTimeout(function () {
      if (ticket !== restTicket || !sleep || inst.disposed || inst.reduced || lost || userPaused || inst.error) return;
      restTimer = 0;
      try {
        settleRest(true);
        if (!tryRest() && render()) showMotion();
        wake();
      } catch (error) { fail(error); }
    }, Math.max(1, Math.ceil((w.end - world.time) * 1000)));
    return true;
  }
  function interruptRest() {
    if (inst.disposed || inst.reduced || lost || inst.error) return false;
    try {
      settleRest(false);
      var w = restAt(world.time); if (w) restBlockedUntil = Math.max(restBlockedUntil, w.end);
      clearRest(); wake(); return !inst.error;
    } catch (error) { fail(error); return false; }
  }
  function cancelLoad() { generation++; if (controller) controller.abort(); controller = null; pending = null; }
  function releaseGraphics() {
    if (G) {
      var gl = G.gl;
      world.layers.forEach(function (L) { if (L.tex) gl.deleteTexture(L.tex); if (L.blinkTex) gl.deleteTexture(L.blinkTex); if (L.blinkMask) gl.deleteTexture(L.blinkMask); if (L.blinkPaint) gl.deleteTexture(L.blinkPaint); [L.bPos, L.bUv, L.bDepth, L.bIdx, L.bFade].forEach(function (b) { if (b) gl.deleteBuffer(b); }); });
      if (G.flowTex) gl.deleteTexture(G.flowTex);
      if (G.lightTex) gl.deleteTexture(G.lightTex);
      if (G.prog) gl.deleteProgram(G.prog);
    }
    G = null; world.G = null; world.layers = []; world.byId = {}; world.order = [];
  }
  function fail(error) { inst.error = error; clearRest(); stop(); still(); if (opts.onError) opts.onError(error); else if (root.console) root.console.warn(error); }
  function reset() {
    clearRest(); restBlockedUntil = -1;
    world.rng = LP.rng(scene.seed || 1); world.time = 0; world.ripples = []; world.events = []; acc = 0;
    var p = world.pointer; p.nx = p.ny = p.active = p.want = p.vx = p.vy = 0; p.x = W / 2; p.y = H / 2; p.last = -9;
    world.lastPoke = null;
    world.wind = new LP.Wind(scene.wind, world.size, world.rng);
    world.wind.listeners.push(function (g) { world.events.push({ type: 'gust', t: Math.round(world.time * 100) / 100, strength: Math.round(g.strength * 100) / 100 }); });
    world.camera = new LP.Camera(scene.camera); world.light = new LP.Light(scene.light, world.size, world.rng); world.ch = new LP.Channels(scene.channels, world);
    world.ripple = function (x, y, s) { world.ripples.push({ x: x, y: y, t0: world.time, s: s == null ? 1 : s }); if (world.ripples.length > 4) world.ripples.shift(); };
    world.ripNext = {};
  }
  function rewind() { reset(); if (G) { world.order.forEach(function (L) { L.build(); }); if (typeof scene.setup === 'function') scene.setup(world, LP); } }
  function build(images) {
    releaseGraphics(); G = LP.GL(canvas);
    if (!G) throw new Error('living-picture: WebGL unavailable');
    world.G = G;
    scene.layers.forEach(function (d, i) { var L = new LP.Layer(d, world, images[i]); world.layers.push(L); world.byId[L.id] = L; });
    world.order = order.map(function (id) { return world.byId[id]; });
    world.order.forEach(function (L) { L.build(); });
    if (scene.flowMap && images.flow) G.flowTex = LP.texture(G, images.flow);
    if (scene.lightMap && images.light) G.lightTex = LP.texture(G, images.light);
    scene.layers.forEach(function (d) {
      if (d.relight && !G.lightTex) throw new Error('living-picture: layer ' + d.id + ' relights but the scene has no lightMap');
    });
    if (typeof scene.setup === 'function') scene.setup(world, LP);
  }
  function step() {
    var t = world.time + STEP, P = world.pointer; world.time = t;
    P.active += (P.want - P.active) * Math.min(1, STEP * 3);
    if (!inst.scripted && t - P.last > 2.8) P.want = 0;
    world.wind.step(t); world.ch.step(t, STEP); world.camera.step(t, STEP, P, world.scroll); world.light.step(t, STEP, world.ch);
    world.order.forEach(function (L) {
      var rp = L.def.ripples;
      if (rp && rp.every) {
        if (world.ripNext[L.id] == null) world.ripNext[L.id] = t + world.rng.pick(rp.first || rp.every);
        if (t >= world.ripNext[L.id]) {
          var at = rp.at ? rp.at[Math.floor(world.rng() * rp.at.length)] : [world.rng.range(L.rect[0], L.rect[0] + L.rect[2]), world.rng.range(L.rect[1], L.rect[1] + L.rect[3])];
          world.ripple(at[0], at[1], world.rng.range(0.6, 1)); world.ripNext[L.id] = t + world.rng.pick(rp.every);
        }
      }
      var fw = L.def.flow;
      if (fw) {
        var fk = 1;
        if (fw.gust) { var fc = fw.at || [L.rect[0] + L.rect[2] / 2, L.rect[1] + L.rect[3] / 2], wv = world.wind.at(fc[0], fc[1]); fk = Math.max(0.2, 1 + fw.gust * (Math.hypot(wv[0], wv[1]) - 0.3)); }
        L.flowPhase = (L.flowPhase || 0) + STEP * (fw.speed || 8) / (fw.reach || 18) * fk;
      }
      if (!L.def.fixed) L.step(t, STEP);
    });
    if (typeof scene.tick === 'function') scene.tick(world, t, STEP, LP);
  }
  var I3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];
  function mul(a, b) { return [a[0]*b[0]+a[3]*b[1], a[1]*b[0]+a[4]*b[1], 0, a[0]*b[3]+a[3]*b[4], a[1]*b[3]+a[4]*b[4], 0, a[0]*b[6]+a[3]*b[7]+a[6], a[1]*b[6]+a[4]*b[7]+a[7], 1]; }
  var f4 = function (n) { return new Float32Array(n * 4); };
  var WA = f4(6), WB = f4(6), WC = f4(6), WD = f4(6), BA = f4(6), BB = f4(6), RIP = f4(4), GA = f4(4), GB = f4(4);
  function render() {
    if (inst.disposed || !G || inst.reduced || lost) return false;
    var gl = G.gl, U = G.U, t = world.time, cam = world.camera, ch = world.ch, i, k;
    var dpr = Math.min(opts.maxDpr || 2, window.devicePixelRatio || 1);
    var cw = Math.max(2, Math.round((canvas.clientWidth || W) * dpr)), chh = Math.max(2, Math.round((canvas.clientHeight || H) * dpr));
    var cap = opts.maxPixels || 3.2e6;
    if (cw * chh > cap) { var sc = Math.sqrt(cap / (cw * chh)); cw = Math.round(cw * sc); chh = Math.round(chh * sc); }
    if (inst.fixedSize) { cw = inst.fixedSize[0]; chh = inst.fixedSize[1]; }
    if (canvas.width !== cw || canvas.height !== chh) { canvas.width = cw; canvas.height = chh; }
    gl.viewport(0, 0, cw, chh);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(G.prog);
    var shift = cam.travel * W;
    gl.uniform2f(U.u_size, W, H);
    gl.uniform2f(U.u_cam, -cam.x * shift, -cam.y * shift);
    gl.uniform1f(U.u_focus, cam.focus);
    var z = scene.camera && scene.camera.zoom ? scene.camera.zoom : 1;
    gl.uniform3f(U.u_view, W * (1 - 1 / z) / 2, H * (1 - 1 / z) / 2, z);
    gl.uniform1f(U.u_hold, cam.edge === 'mirror' ? 0 : cam.hold * Math.min(W, H));
    gl.uniform1f(U.u_time, t);
    gl.uniform1i(U.u_tex, 0); gl.uniform1i(U.u_flowTex, 1); gl.uniform1i(U.u_blinkTex, 2); gl.uniform1i(U.u_blinkMask,3); gl.uniform1i(U.u_blinkPaint,4);
    if (G.flowTex) { gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, G.flowTex); gl.activeTexture(gl.TEXTURE0); }
    gl.uniform1i(U.u_lightTex, 5);
    if (G.lightTex) { gl.activeTexture(gl.TEXTURE5); gl.bindTexture(gl.TEXTURE_2D, G.lightTex); gl.activeTexture(gl.TEXTURE0); }
    for (i = 0; i < world.order.length; i++) {
      var L = world.order[i], d = L.def;
      if (!d.fixed) L.update(t);
      // family: a child rides its parent, so parts of one body never slide apart
      var m = L.xf;
      if (L.parent && ((L.parent.warps || []).length || L.parent.def.flow || L.parent.isSprites || L.parent.def.bulge))
        throw new Error('living-picture: cannot carry ' + L.id + ' through parent texture/sprite deformation');
      if (L.parent && L.parent.motion.length && !d.attach)
        throw new Error('living-picture: deforming parent needs explicit attach for ' + L.id);
      if (L.parent) {
        var P = L.parent;
        if (d.attach) {
          var a0 = d.attach, c0 = P.carry(a0[0], a0[1]), c1 = P.carry(a0[0], a0[1] - 24);
          var ang = Math.atan2(c1[0] - c0[0], -(c1[1] - c0[1]));
          var cs = Math.cos(ang), sn = Math.sin(ang);
          var ride = [cs, sn, 0, -sn, cs, 0, c0[0] - cs * a0[0] + sn * a0[1], c0[1] - sn * a0[0] - cs * a0[1], 1];
          m = mul(ride, L.xf);
        } else m = mul(P.fullXf || P.xf, L.xf);
      }
      L.fullXf = m;
    }
    // light eases in from the painted state, so the first frame is the picture itself and nothing pops
    var Lg = world.light, sh = Lg.shade, wake = LP.smooth(0, 1.8, t);
    if (sh) {
      gl.uniform4f(U.u_shade, sh.amount * wake, sh.scale, sh.ox + sh.seed, sh.oy + sh.seed * 0.7);
      gl.uniform4f(U.u_shade2, sh.horizon, sh.near, sh.coverNow, sh.soft);
      gl.uniform3f(U.u_shadeTint, sh.tint[0], sh.tint[1], sh.tint[2]);
    } else gl.uniform4f(U.u_shade, 0, 1, 0, 0);
    // lights inside the picture ride with whatever they belong to
    if (Lg.glows.length > 4) throw new Error("living-picture: more than four simultaneous glow definitions");
    var ng = 0;
    for (i = 0; i < Lg.glows.length; i++) {
      var gd = Lg.glows[i], gp = gd.at, own = gd.on ? world.byId[gd.on] : null, dep = gd.depth == null ? (own ? own.depthAt(gp[0], gp[1]) : cam.focus) : gd.depth;
      var gx = gp[0], gy = gp[1], passK = 1, ps = Lg.pass[i];
      if (ps) {                               // travelling from one side to the other, swelling and fading as it goes
        if (ps.u < 0) passK = 0;
        else { var uu = LP.ease(ps.u), sf = gd.sweep.from || gp, st2 = gd.sweep.to || gp; gx = sf[0] + (st2[0] - sf[0]) * uu; gy = sf[1] + (st2[1] - sf[1]) * uu; passK = Math.pow(Math.sin(Math.PI * ps.u), 0.7); }
      }
      if (own) { var cp = own.carry(gx, gy); gx = cp[0]; gy = cp[1]; }
      gx += -cam.x * shift * (cam.focus - dep); gy += -cam.y * shift * (cam.focus - dep);
      var col = gd.color || [1, 0.85, 0.6];
      GA[ng * 4] = gx; GA[ng * 4 + 1] = gy; GA[ng * 4 + 2] = gd.radius || 60; GA[ng * 4 + 3] = Math.max(0, ch.get(gd.power, 1)) * wake * passK;
      GB[ng * 4] = col[0]; GB[ng * 4 + 1] = col[1]; GB[ng * 4 + 2] = col[2]; GB[ng * 4 + 3] = gd.mode === 'multiply' ? 1 : 0;
      ng++;
    }
    gl.uniform1i(U.u_nglow, ng);
    if (ng) { gl.uniform4fv(U.u_ga, GA); gl.uniform4fv(U.u_gb, GB); }
    var nr = 0;
    for (i = 0; i < world.ripples.length; i++) {
      var rp = world.ripples[i];
      RIP[nr * 4] = rp.x; RIP[nr * 4 + 1] = rp.y; RIP[nr * 4 + 2] = t - rp.t0; RIP[nr * 4 + 3] = rp.s; nr++;
    }
    var draws = world.layers.filter(function (L) { return !L.def.fixed; }).concat(world.layers.filter(function (L) { return L.def.fixed; }));
    for (i = 0; i < draws.length; i++) {
      var L = draws[i], d = L.def, m = d.fixed ? I3 : L.fullXf;
      if (L.isSprites) L.clipSprites();
      var op = d.fixed ? 1 : L.opacity * (d.fade != null ? LP.clamp(ch.get(d.fade, 1), 0, 1) : 1);
      if (op <= 0.002) continue;
      gl.uniform2f(U.u_cam, d.fixed ? 0 : -cam.x * shift, d.fixed ? 0 : -cam.y * shift);
      gl.uniform3f(U.u_view, d.fixed ? 0 : W * (1 - 1 / z) / 2, d.fixed ? 0 : H * (1 - 1 / z) / 2, d.fixed ? 1 : z);
      gl.uniformMatrix3fv(U.u_xf, false, m);
      gl.uniform4f(U.u_rect, L.rect[0], L.rect[1], L.rect[2], L.rect[3]);
      gl.uniform1f(U.u_opacity, op);
      gl.uniform1f(U.u_mirror, !d.fixed && L.mirror ? 1 : 0);
      var nw = 0, nb = 0, ws = d.fixed ? [] : LP.activeWarps(L.warps, ch, L.id);
      for (k = 0; k < ws.length; k++) {
        var w = ws[k], o = nw * 4;
        if (w.type === 'blink') {
          var closed = LP.clamp(ch.get(w.closed == null ? 'blink' : w.closed), 0, 1);
          if (closed <= 0) continue;
          var bo=nb*4;
          BA[bo]=w.at[0];BA[bo+1]=w.at[1];BA[bo+2]=w.r[0];BA[bo+3]=w.r[1];
          BB[bo]=(w.rot||0)*LP.D2R;BB[bo+1]=closed;BB[bo+2]=w.line==null?0.2:w.line;
          BB[bo+3]=Math.min(0.12,0.65*L.blinkTexel/w.r[1]);
          nb++; continue;
        } else {
          var ang2 = ch.get(w.angle) * LP.D2R, dx = ch.get(w.x), dy = ch.get(w.y), s2 = w.scale == null ? 1 : 1 + ch.get(w.scale);
          if (Math.abs(ang2) < 1e-5 && Math.abs(dx) < 1e-3 && Math.abs(dy) < 1e-3 && Math.abs(s2 - 1) < 1e-5) continue;
          var rg = w.region || {}, c = rg.c || w.pivot, rr = rg.r || w.r || [40, 40];
          if (typeof rr === 'number') rr = [rr, rr];
          WA[o] = w.pivot[0]; WA[o + 1] = w.pivot[1]; WA[o + 2] = c[0]; WA[o + 3] = c[1];
          WB[o] = rr[0]; WB[o + 1] = rr[1]; WB[o + 2] = (rg.rot || 0) * LP.D2R; WB[o + 3] = rg.core == null ? 0.55 : rg.core;
          WC[o] = ang2; WC[o + 1] = dx; WC[o + 2] = dy; WC[o + 3] = 0; WD[o] = s2;
        }
        nw++;
      }
      gl.uniform1i(U.u_nwarp, nw);
      if (nw) { gl.uniform4fv(U.u_wa, WA); gl.uniform4fv(U.u_wb, WB); gl.uniform4fv(U.u_wc, WC); gl.uniform4fv(U.u_wd, WD); }
      gl.uniform1i(U.u_nblink,nb);
      if(nb){
        gl.uniform4fv(U.u_ba,BA);gl.uniform4fv(U.u_bb,BB);
        gl.uniform3f(U.u_blinkPixel,W/cw/z,H/chh/z,L.blinkTexel);
        gl.activeTexture(gl.TEXTURE2);gl.bindTexture(gl.TEXTURE_2D,L.blinkTex);
        gl.activeTexture(gl.TEXTURE3);gl.bindTexture(gl.TEXTURE_2D,L.blinkMask);
        gl.activeTexture(gl.TEXTURE4);gl.bindTexture(gl.TEXTURE_2D,L.blinkPaint);
      } else { gl.activeTexture(gl.TEXTURE2);gl.bindTexture(gl.TEXTURE_2D,L.tex);gl.activeTexture(gl.TEXTURE3);gl.bindTexture(gl.TEXTURE_2D,L.tex);gl.activeTexture(gl.TEXTURE4);gl.bindTexture(gl.TEXTURE_2D,L.tex); }
      var fl = d.fixed ? null : d.flow;
      if (fl) {
        var fd = fl.dir || [1, 0], fn = Math.hypot(fd[0], fd[1]) || 1, bx = fl.box;
        gl.uniform4f(U.u_flow, fd[0] / fn, fd[1] / fn, L.flowPhase || 0, (fl.reach || 18) * wake);
        gl.uniform4f(U.u_flow2, fl.map && G.flowTex ? 1 : 0, 1, 0, 0);
        if (bx) gl.uniform4f(U.u_flowBox, (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2, (bx[2] - bx[0]) / 2, (bx[3] - bx[1]) / 2);
        else gl.uniform4f(U.u_flowBox, 0, 0, 0, 0);
      } else { gl.uniform4f(U.u_flow, 0, 0, 0, 0); gl.uniform4f(U.u_flow2, 0, 0, 0, 0); }
      var rpd = d.fixed ? null : d.ripples;
      if (rpd && nr) {
        gl.uniform1i(U.u_nrip, nr); gl.uniform4fv(U.u_rip, RIP);
        gl.uniform4f(U.u_ripP, rpd.speed || 46, rpd.width || 9, rpd.life || 4.5, rpd.shine == null ? 0.18 : rpd.shine);
      } else gl.uniform1i(U.u_nrip, 0);
      // a glaze is lit through what lies under it, so it takes no light of its own
      var mult = d.blend === 'multiply';
      var li = (d.fixed || mult) ? { shade: 0, gain: 0, flash: 0, glow: 0 } : (d.light || {});
      gl.uniform3f(U.u_recv, li.shade == null ? 1 : li.shade, li.upright ? 1 : 0, li.foot == null ? L.rect[1] + L.rect[3] : li.foot);
      var gr = li.gain == null ? 1 : li.gain;
      gr *= wake;
      gl.uniform3f(U.u_gain, 1 + (Lg.gain[0] - 1) * gr, 1 + (Lg.gain[1] - 1) * gr, 1 + (Lg.gain[2] - 1) * gr);
      var fr = li.flash == null ? 1 : li.flash;
      gl.uniform3f(U.u_lift, Lg.lift[0] * fr, Lg.lift[1] * fr, Lg.lift[2] * fr);
      gl.uniform1f(U.u_glowRecv, li.glow == null ? 1 : li.glow);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, L.tex);
      gl.bindBuffer(gl.ARRAY_BUFFER, L.bPos); if (d.fixed) gl.bufferSubData(gl.ARRAY_BUFFER, 0, L.rest); gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, L.bUv); gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, L.bDepth); gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 0, 0);
      if (L.isSprites) { gl.bindBuffer(gl.ARRAY_BUFFER, L.bFade); gl.enableVertexAttribArray(3); gl.vertexAttribPointer(3, 1, gl.FLOAT, false, 0, 0); }
      else { gl.disableVertexAttribArray(3); gl.vertexAttrib1f(3, 1.0); }
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, L.bIdx);
      gl.uniform1f(U.u_blend, mult ? 1 : 0);
      gl.uniform1f(U.u_relight, (!d.fixed && !mult && G.lightTex && d.relight) ? (d.relight === true ? 1 : LP.clamp(+d.relight, 0, 1)) : 0);
      if (mult) gl.blendFuncSeparate(gl.ZERO, gl.SRC_COLOR, gl.ZERO, gl.ONE);
      else gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawElements(gl.TRIANGLES, L.count, gl.UNSIGNED_SHORT, 0);
    }
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    inst.renders++;
    return true;
  }
  function loop(now) {
    raf = 0;
    if (!inst.playing || inst.disposed || !G || inst.reduced || lost) return;
    var dt = Math.min(0.1, (now - lastNow) / 1000 || 0); lastNow = now; acc += dt;
    var n = 0;
    try {
      while (acc >= STEP && n < 6) { step(); acc -= STEP; n++; if (tryRest()) return; }
      if (n === 6) acc = 0;
      if (n && render()) showMotion();
    } catch (error) { fail(error); return; }
    if (inst.playing && !inst.disposed) raf = requestAnimationFrame(loop);
  }
  function wake() {
    var should = !inst.disposed && !lost && !inst.error && !userPaused && visible && onscreen && !inst.reduced && !!G && !inst.scripted;
    try {
      if (!should) {
        if (sleep && !inst.disposed && !inst.reduced && !lost && !userPaused && !inst.scripted && !inst.error) {
          settleRest(false);
          var quiet = eligibleRest();
          if (quiet) { inst.resting = true; inst.restUntil = quiet.end; still(); }
        }
        else if (sleep) clearRest();
        stop();
      } else if (!tryRest() && !inst.playing) { inst.playing = true; lastNow = performance.now(); if (!raf) raf = requestAnimationFrame(loop); }
    } catch (error) { fail(error); }
    if (userPaused || inst.reduced || inst.disposed || lost) still();
    if (btn) {
      btn.disabled = inst.reduced; btn.textContent = userPaused ? '▶' : '❚❚';
      btn.setAttribute('aria-label', userPaused ? (opts.playLabel || 'Play motion') : (opts.pauseLabel || 'Pause motion'));
      btn.setAttribute('aria-pressed', userPaused ? 'true' : 'false');
    }
  }
  inst.play = function () {
    if (inst.disposed) return;
    userPaused = false; inst.scripted = false;
    if (!G && !inst.reduced) { inst.ready = initialize(); inst.ready.catch(function () {}); }
    wake();
  };
  inst.pause = function () {
    if (inst.disposed) return;
    userPaused = true; inst.scripted = false; clearRest(); stop(); still();
    if (G) rewind();
    wake();
  };
  inst.reset = inst.pause;
  inst.wake = interruptRest;
  inst.gust = function (o) { if (!inst.disposed && G && !inst.reduced && interruptRest()) return world.wind.gust(o); };
  inst.ripple = function (x, y, s) { if (!inst.disposed && G && !inst.reduced && interruptRest()) world.ripple(x, y, s); };
  inst.seek = function (t, script) {
    if (inst.disposed || !G || inst.reduced || lost) { still(); return world.time; }
    if (!Number.isFinite(t) || t < 0) throw new Error('living-picture: seek time must be finite and nonnegative');
    clearRest(); inst.scripted = true; wake(); rewind(); inst.script = script || null;
    return inst.advance(t);
  };
  inst.advance = function (dt) {
    if (inst.disposed || !G || inst.reduced || lost) { still(); return world.time; }
    if (!Number.isFinite(dt) || dt < 0) throw new Error('living-picture: advance must be finite and nonnegative');
    clearRest(); inst.scripted = true; stop();
    var n = Math.round(dt / STEP);
    try {
      var w = eligibleRest(); if (w) checkRest(w, true);
      for (var i = 0; i < n; i++) {
        if (inst.script) inst.script(world, world.time); step();
        w = eligibleRest(); if (w) checkRest(w, true);
      }
      w = eligibleRest();
      if (w) { still(); inst.resting = true; inst.restUntil = w.end; }
      else if (render()) showMotion();
    } catch (error) { fail(error); throw error; }
    return world.time;
  };
  inst.setPointer = function (nx, ny, active) {
    if (inst.disposed) return;
    if (!interruptRest()) return;
    var P = world.pointer; P.nx = nx; P.ny = ny; P.x = (nx * 0.5 + 0.5) * W; P.y = (ny * 0.5 + 0.5) * H; P.want = active ? 1 : 0; P.last = world.time;
  };
  inst.snapshot = function (type, q) {
    if (inst.disposed) throw new Error('living-picture: instance destroyed');
    if (inst.error || inst.reduced || inst.resting || userPaused && !inst.scripted || !G || lost) {
      var original = document.createElement('canvas'); original.width = fallback.naturalWidth || W; original.height = fallback.naturalHeight || H;
      original.getContext('2d').drawImage(fallback, 0, 0, original.width, original.height);
      return original.toDataURL(type || 'image/png', q);
    }
    try { render(); return canvas.toDataURL(type || 'image/png', q); } catch (error) { fail(error); throw error; }
  };
  inst.destroy = function () {
    if (inst.disposed) return;
    inst.disposed = true; userPaused = true; clearRest(); stop(); still(); cancelLoad();
    removers.splice(0).forEach(function (remove) { remove(); });
    if (io) { io.disconnect(); io = null; }
    releaseGraphics(); inst.images = null;
    var ii = LP.instances.indexOf(inst); if (ii >= 0) LP.instances.splice(ii, 1);
    if (el.__livingPicture === inst) { delete el.__livingPicture; delete el.__lp; }
    if (canvas !== el && canvas.parentNode) canvas.parentNode.removeChild(canvas);
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
  };

  var lastPuff = 0, lastMoveT = 0, lastX = 0, lastY = 0;
  function onMove(e) {
    if (inst.disposed || inst.scripted || inst.reduced || userPaused || !G) return;
    var r = canvas.getBoundingClientRect(); if (!r.width) return;
    var nx = ((e.clientX - r.left) / r.width) * 2 - 1, ny = ((e.clientY - r.top) / r.height) * 2 - 1, P = world.pointer;
    if (Math.abs(nx) >= 1.25 || Math.abs(ny) >= 1.25) { P.want = 0; return; }
    if (!interruptRest()) return;
    var now = Number.isFinite(e.timeStamp) ? e.timeStamp : performance.now(), dtm = lastMoveT ? Math.max(1, now - lastMoveT) / 1000 : 1;
    var px = (nx * 0.5 + 0.5) * W, py = (ny * 0.5 + 0.5) * H;
    P.vx = (px - lastX) / dtm; P.vy = (py - lastY) / dtm; lastX = px; lastY = py; lastMoveT = now;
    P.nx = LP.clamp(nx, -1, 1); P.ny = LP.clamp(ny, -1, 1); P.x = px; P.y = py; P.want = e.pointerType === 'touch' ? 0.6 : 1; P.last = world.time;
    var sp = Math.hypot(P.vx, P.vy) / W;
    if (world.wind.pointerGain > 0 && sp > 1.1 && dtm < 0.2 && world.time - lastPuff > 0.7) {
      world.wind.puff(px, py, P.vx, P.vy, Math.min(1, sp * 0.35) * world.wind.pointerGain); lastPuff = world.time;
      for (var gi = 0; gi < world.light.glows.length; gi++) if (world.light.glows[gi].sweep && world.light.glows[gi].sweep.visitor) world.light.passNow(gi);
    }
  }
  function poke(x, y) {
    if (inst.disposed || inst.reduced || userPaused || !G) return;
    if (!interruptRest()) return;
    world.events.push({ type: 'tap', x: Math.round(x), y: Math.round(y), t: world.time }); world.lastPoke = world.time;
    if (typeof scene.poke === 'function') scene.poke(world, x, y, LP);
    else if (scene.tap === 'ripple') world.ripple(x, y, 1);
    else if (scene.tap !== 'none') world.wind.puff(x, y, world.wind.dir[0], world.wind.dir[1] - 0.2, 0.9);
  }
  function onDown(e) {
    if (inst.disposed || inst.scripted || inst.reduced || userPaused || !G) return;
    var r = canvas.getBoundingClientRect(); if (r.width && r.height) poke((e.clientX - r.left) / r.width * W, (e.clientY - r.top) / r.height * H);
  }
  inst.probe = function (id, x, y) {
    var L = world.byId[id]; if (!L) return null;
    var c = L.carry(x, y), cam = world.camera, sh = cam.travel * W, dep = L.depthAt(x, y);
    return [Math.round((c[0]-x)*100)/100, Math.round((c[1]-y)*100)/100, Math.round(-cam.x*sh*(cam.focus-dep)*100)/100, Math.round(-cam.y*sh*(cam.focus-dep)*100)/100];
  };
  inst.tap = poke;
  inst.puff = function (x, y, vx, vy, strength) { if (!inst.disposed && G && !inst.reduced && interruptRest()) world.wind.puff(x, y, vx, vy, strength == null ? 0.8 : strength); };
  function onScroll() {
    if (inst.disposed) return;
    var r = canvas.getBoundingClientRect(), vh = window.innerHeight || 1, next = LP.clamp(((r.top + r.height / 2) - vh / 2) / vh, -1, 1);
    if (next !== world.scroll && G && !interruptRest()) return;
    world.scroll = next;
  }
  function onVis() { visible = !document.hidden; wake(); }
  function controls() {
    if (opts.controls === false || canvas === el) return;
    btn = document.createElement('button'); btn.type = 'button';
    btn.style.cssText = 'position:absolute;right:8px;bottom:8px;width:30px;height:30px;border-radius:50%;border:0;background:rgba(20,20,20,.42);color:#fff;font:11px/30px system-ui,sans-serif;padding:0;cursor:pointer;opacity:.8;transition:opacity .25s';
    var show = function () { btn.style.opacity = '1'; }, hide = function () { if (document.activeElement !== btn) btn.style.opacity = '.8'; };
    listen(el, 'pointerenter', show); listen(el, 'pointerleave', hide); listen(btn, 'focus', show); listen(btn, 'blur', hide);
    listen(btn, 'click', function (e) { e.stopPropagation(); if (userPaused) inst.play(); else inst.pause(); show(); }); el.appendChild(btn);
  }
  function initialize() {
    if (inst.disposed || inst.reduced || lost) { still(); return Promise.resolve(inst); }
    if (G) return Promise.resolve(inst);
    if (pending) return pending;
    var ticket = ++generation;
    controller = typeof AbortController === 'function' ? new AbortController() : null;
    var signal = controller && controller.signal;
    var srcs = scene.layers.map(function (l) { return (opts.assets && opts.assets[l.src]) || ((opts.base || '') + l.src); });
    var loads = inst.images ? Promise.resolve(inst.images) : Promise.all(srcs.map(function (src) { return LP.loadImage(src, signal); })).then(function (images) {
      if (scene.flowMap) return LP.loadImage((opts.assets && opts.assets[scene.flowMap]) || ((opts.base || '') + scene.flowMap), signal).then(function (f) { images.flow = f; return images; });
      return images;
    }).then(function (images) {
      if (scene.lightMap && !images.light) return LP.loadImage((opts.assets && opts.assets[scene.lightMap]) || ((opts.base || '') + scene.lightMap), signal).then(function (l) { images.light = l; return images; });
      return images;
    });
    pending = loads.then(function (images) {
      if (inst.disposed || inst.reduced || lost || ticket !== generation) return inst;
      inst.images = images; inst.error = null; reset(); build(images); render(); still(); wake(); return inst;
    }).catch(function (error) {
      if (inst.disposed || inst.reduced || ticket !== generation || error.name === 'AbortError') return inst;
      releaseGraphics(); fail(error); throw error;
    }).finally(function () { if (ticket === generation) { pending = null; controller = null; } });
    return pending;
  }
  function onLost(e) { e.preventDefault(); if (inst.disposed) return; lost = true; clearRest(); stop(); still(); cancelLoad(); releaseGraphics(); }
  function onRestored() { if (inst.disposed) return; lost = false; inst.ready = initialize(); inst.ready.catch(function () {}); }
  listen(canvas, 'webglcontextlost', onLost); listen(canvas, 'webglcontextrestored', onRestored);
  listen(window, 'pointermove', onMove, { passive: true }); listen(canvas, 'pointerdown', onDown, { passive: true });
  listen(window, 'scroll', onScroll, { passive: true }); onScroll(); listen(document, 'visibilitychange', onVis);
  if (typeof IntersectionObserver === 'function') { io = new IntersectionObserver(function (en) { if (inst.disposed) return; onscreen = !!(en[0] && en[0].isIntersecting); wake(); }, { rootMargin: '80px' }); io.observe(canvas); }
  if (mq) {
    var onMq = function () {
      if (inst.disposed) return;
      inst.reduced = mq.matches;
      if (inst.reduced) { clearRest(); stop(); still(); cancelLoad(); releaseGraphics(); inst.images = null; reset(); }
      else { inst.ready = initialize(); inst.ready.catch(function () {}); }
      wake();
    };
    if (mq.addEventListener) listen(mq, 'change', onMq);
    else if (mq.addListener) { mq.addListener(onMq); removers.push(function () { mq.removeListener(onMq); }); }
  }
  reset(); controls(); still(); wake(); inst.ready = initialize();
  return inst;
};

LP.unmount = function (el) {
  el = typeof el === 'string' ? document.querySelector(el) : el;
  if (!el) return;
  if (el.__lpPending) { el.__lpPending.cancelled = true; if (el.__lpPending.controller) el.__lpPending.controller.abort(); delete el.__lpPending; }
  if (el.__livingPicture) el.__livingPicture.destroy();
  delete el.__lp;
};
LP.autoMount = function (parent) {
  var nodes = (parent || document).querySelectorAll('[data-living-picture]'), out = [];
  for (var i = 0; i < nodes.length; i++) (function (el) {
    if (el.__lp || el.__livingPicture) return;
    el.__lp = true;
    var token = { cancelled: false, controller: typeof AbortController === 'function' ? new AbortController() : null }; el.__lpPending = token;
    var url = el.getAttribute('data-living-picture'), base = url.replace(/[^\/]*$/, '');
    out.push(fetch(url, token.controller ? { signal: token.controller.signal } : undefined).then(function (r) { if (!r.ok) throw new Error('living-picture: scene request failed'); return r.json(); }).then(function (scene) {
      if (token.cancelled || el.__lpPending !== token || el.isConnected === false) return null;
      delete el.__lpPending; return LP.mount(el, scene, { base: base }).ready;
    }).catch(function (e) { if (el.__lpPending === token) { delete el.__lpPending; delete el.__lp; } if (e.name !== 'AbortError' && root.console) root.console.warn(e); }));
  })(nodes[i]);
  return Promise.all(out);
};
