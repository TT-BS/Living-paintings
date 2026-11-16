#!/usr/bin/env node
/* Render frames (and optionally a short video) of a bundled living picture.
 *
 * Drives a locally installed Chrome/Chromium/Edge over the DevTools protocol.
 * No npm packages needed; requires Node 22+ (built-in WebSocket).
 * Playback is deterministic: the same page and options give the same frames.
 *
 *   node capture.mjs <index.html> --out <dir> --times 0,1,2.5,4
 *   node capture.mjs <index.html> --out <dir> --every 0.5 --duration 12
 *   node capture.mjs <index.html> --out <dir> --video 16 [--fps 30] [--keep-frames] [--encoder auto|ffmpeg|avfoundation] [--hevc]
 *
 * Options
 *   --width N          frame width in px (default 720); height follows the picture
 *   --pointer SPEC     none (default) | orbit:START-END | sweep:START-END | corners:START-END | hold:START-END:NX,NY
 *                      moves a virtual pointer so the change of viewpoint can be judged (seconds). corners
 *                      visits the four extremes of the view (left, right, top, bottom); hold keeps the
 *                      pointer at NX,NY (each -1..1, 0,0 is the centre)
 *   --scroll V         pretend the page is scrolled so the picture sits V (-1..1) from the middle of the screen
 *   --gusts LIST       force a gust at these seconds, e.g. 1.5,8 (default: the scene's own weather)
 *   --puff LIST        a local puff of air, as if a pointer swept past: "t,x,y,dx,dy" (picture px; several joined by ;)
 *   --tap LIST         a touch on the picture: "t,x,y" (several joined by ;)
 *   --eval EXPR        a JavaScript expression evaluated in the page at each captured time, with `lp` (the player)
 *                      and `world` in scope; results are returned under "evals". For measuring, e.g.
 *                      --eval "lp.probe('tree', 410, 220)"  (how far that point of that layer has moved, in px)
 *   --channels LIST    channel names (e.g. blink,tilt,earR): sampled at each captured time and summarised under
 *                      "channels": range and the times of the strongest peaks (for timing a close look)
 *   --track LIST       "layer:x,y" points (several joined by ;): how far each moved, summarised under "tracks"
 *                      with the largest displacement and when (sample densely enough to catch the peak)
 *   --name PREFIX      file name prefix (default "f")
 *   --png              lossless frames (default JPEG)
 *   --encoder NAME     how --video frames become an MP4: auto (default) uses ffmpeg, and on macOS AVFoundation
 *                      when ffmpeg is missing, does not run or fails (why goes under "ffmpeg_error"); ffmpeg
 *                      runs libx264 -crf 19; avfoundation runs macos/lpencode.swift (hardware encoder, about
 *                      as close to the frames as x264 -crf 19 at 2.5 to 3.6 times the size, tagged so that
 *                      QuickTime, Safari and Chrome show the frames' colours; built once with the Xcode
 *                      command line tools). Frames over 4096 px a side come out as HEVC there, and "note"
 *                      says so. The result names the encoder and codec used under "encoder" and "codec"
 *   --hevc             HEVC (H.265, tagged hvc1 for Safari and QuickTime) instead of H.264; ffmpeg uses libx265
 *   --tmp DIR          where the throwaway browser profile goes (default: system temp); removed afterwards
 *   --chrome PATH      browser binary (default: auto-detect, or env CHROME_PATH)
 *   --probe SECONDS    do not capture: let the page run in real time for SECONDS and report whether it is
 *                      playing (use with --reduced-motion to confirm that it stays still)
 *   --reduced-motion   emulate the "prefers-reduced-motion: reduce" setting
 *   --bench            do not capture: measure the cost of a frame (simulation step plus draw) on this machine
 * The first argument may also be an http(s) URL of a page that mounts a living picture.
 * Prints one JSON object describing what was written.
 */
import { spawn, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

function args(argv) {
  const o = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      if (['png', 'keep-frames', 'help', 'reduced-motion', 'bench', 'hevc'].includes(k)) o[k] = true; else o[k] = argv[++i];
    } else o._.push(a);
  }
  return o;
}
const A = args(process.argv.slice(2));
if (!A.out && (A.probe || A.bench)) A.out = os.tmpdir();      // these two write nothing
if (A.help || !A._[0] || !A.out) {
  console.error(fs.readFileSync(new URL(import.meta.url), 'utf8').split('*/')[0].replace(/^#!.*\n\/\*/, ''));
  process.exit(A.help ? 0 : 2);
}
if (typeof WebSocket === 'undefined') { console.error('capture.mjs needs Node 22 or newer (built-in WebSocket).'); process.exit(2); }
if (A.encoder && !['auto', 'ffmpeg', 'avfoundation'].includes(A.encoder)) { console.error('--encoder must be auto, ffmpeg or avfoundation'); process.exit(2); }

// A helper from macos/<stem>.swift, compiled once per source version (same convention as lp/imgops.py):
// ~/Library/Caches/living-picture/<stem>-<sha256[:12]>, built under a temporary name and renamed into place.
function swiftHelper(stem) {
  if (process.platform !== 'darwin') return { problem: stem + ' needs macOS' };
  const src = fileURLToPath(new URL('./macos/' + stem + '.swift', import.meta.url));
  const digest = crypto.createHash('sha256').update(fs.readFileSync(src)).digest('hex').slice(0, 12);
  const cache = path.join(os.homedir(), 'Library', 'Caches', 'living-picture');
  const exe = path.join(cache, stem + '-' + digest);
  try { fs.accessSync(exe, fs.constants.X_OK); return { exe }; } catch (e) { /* not built yet */ }
  // without a developer directory, xcrun would pop up an install dialog instead of failing
  if (spawnSync('xcode-select', ['-p']).status !== 0) return { problem: 'building ' + stem + ' needs the Xcode command line tools (run: xcode-select --install)' };
  const tmp = exe + '.' + process.pid + '.tmp';
  try {
    fs.mkdirSync(cache, { recursive: true });
    console.error('building ' + stem + ' once (Swift) ...');
    const r = spawnSync('xcrun', ['swiftc', '-O', src, '-o', tmp], { encoding: 'utf8' });
    if (r.error || r.status !== 0) return { problem: 'xcrun swiftc could not build ' + src + ': ' + String(r.error || r.stderr).trim().slice(-600) };
    fs.renameSync(tmp, exe);
    return { exe };
  } catch (e) {
    return { problem: 'could not build ' + src + ': ' + e.message };
  } finally {
    fs.rmSync(tmp, { force: true });
  }
}

function findChrome() {
  const c = [A.chrome, process.env.CHROME_PATH,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'].filter(Boolean);
  for (const p of c) if (fs.existsSync(p)) return p;
  console.error('No Chrome/Chromium/Edge found. Pass --chrome PATH or set CHROME_PATH.');
  process.exit(2);
}

const isUrl = /^https?:\/\//.test(A._[0]);
const page = isUrl ? A._[0] : path.resolve(A._[0]);
const out = path.resolve(A.out);
if (!(A.probe || A.bench)) fs.mkdirSync(out, { recursive: true });
const width = parseInt(A.width || '720', 10);
const fps = parseInt(A.fps || '30', 10);
const prefix = A.name || 'f';
const ext = A.png ? 'png' : 'jpg';

let times = null, video = A.video ? parseFloat(A.video) : 0;
if (A.times) times = A.times.split(',').map(Number);
else if (A.every) { const e = parseFloat(A.every), d = parseFloat(A.duration || '8'); times = []; for (let t = 0; t <= d + 1e-6; t += e) times.push(+t.toFixed(3)); }
else if (!video) times = [0, 1, 2, 3, 4, 6, 8, 10];

const tmpBase = path.resolve(A.tmp || os.tmpdir());
const profile = fs.mkdtempSync(path.join(tmpBase, 'lp-chrome-'));
const chrome = findChrome();
// --use-mock-keychain: a throwaway profile must never touch the person's macOS Keychain (otherwise every launch
// can raise a 'Keychain Not Found' / Chrome Safe Storage prompt). MacAppCodeSignClone off: no copy of the app
// bundle per launch. --password-store=basic is the same idea on Linux.
const child = spawn(chrome, ['--headless=new', '--remote-debugging-port=0', '--user-data-dir=' + profile, '--no-first-run',
  '--use-mock-keychain', '--password-store=basic', '--disable-features=MacAppCodeSignClone',
  '--no-default-browser-check', '--disable-extensions', '--hide-scrollbars', '--mute-audio', '--disable-background-networking',
  '--disk-cache-size=1', '--ignore-gpu-blocklist', '--enable-webgl', '--allow-file-access-from-files',
  '--window-size=' + (width + 40) + ',' + (width * 2 + 40), 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });

let cleaned = false;
function cleanup() {
  if (cleaned) return; cleaned = true;
  try { child.kill('SIGKILL'); } catch (e) { /* already gone */ }
  // the profile is ours and throwaway: remove it, but only if it is the folder we made
  if (path.basename(profile).startsWith('lp-chrome-') && profile.startsWith(tmpBase)) {
    for (let i = 0; i < 5; i++) { try { fs.rmSync(profile, { recursive: true, force: true }); break; } catch (e) { spawnSync('sleep', ['0.2']); } }
  }
}
process.on('exit', cleanup);
process.on('SIGINT', () => { cleanup(); process.exit(130); });

const wsUrl = await new Promise((ok, fail) => {
  let buf = '';
  const timer = setTimeout(() => fail(new Error('Chrome did not start within 20 s')), 20000);
  child.stderr.on('data', (d) => {
    buf += d.toString();
    const m = buf.match(/DevTools listening on (ws:\/\/[^\s]+)/);
    if (m) { clearTimeout(timer); ok(m[1]); }
  });
  child.on('exit', (c) => fail(new Error('Chrome exited early (code ' + c + ')')));
}).catch((e) => { console.error(String(e.message || e)); cleanup(); process.exit(1); });

const port = new URL(wsUrl).port;
async function targets() { return (await fetch('http://127.0.0.1:' + port + '/json/list')).json(); }
let tgt = (await targets()).find((t) => t.type === 'page');
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error('cannot connect to Chrome')); });
let seq = 0; const waiting = new Map(); const events = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { const w = waiting.get(m.id); waiting.delete(m.id); m.error ? w.fail(new Error(m.error.message)) : w.ok(m.result); }
  else if (m.method) events.push(m);
};
function dropAll(why) { for (const w of waiting.values()) w.fail(new Error(why)); waiting.clear(); }
ws.onclose = () => dropAll('the browser connection closed unexpectedly');
ws.onerror = () => dropAll('the browser connection failed');
function send(method, params) {
  return new Promise((ok, fail) => {
    const id = ++seq;
    const timer = setTimeout(() => { if (waiting.has(id)) { waiting.delete(id); fail(new Error('the browser did not answer within 90 s (' + method + ')')); } }, 90000);
    waiting.set(id, { ok: (v) => { clearTimeout(timer); ok(v); }, fail: (e) => { clearTimeout(timer); fail(e); } });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('page error: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}

try {
  await send('Page.enable'); await send('Runtime.enable');
  if (A['reduced-motion']) await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  const LPX = '(window.__lp||(window.LivingPicture&&window.LivingPicture.instances[0]))';
  await send('Page.navigate', { url: isUrl ? page : pathToFileURL(page).href + (A.probe ? '' : '?force=1') });
  const t0 = Date.now();
  let size = null;
  let reducedIdle = false;
  while (Date.now() - t0 < 30000) {
    size = await evaluate('(function(){var l=' + LPX + ';if(l&&!window.__lp)window.__lp=l;return l&&l.images&&l.world&&l.world.layers.length?l.world.size:null})()').catch(() => null);
    if (size) break;
    // Under reduced motion the player never loads layers or WebGL: the original image is all there is.
    if (A.probe && A['reduced-motion'] && await evaluate('(function(){var l=' + LPX + ';return !!(l&&l.reduced)})()').catch(() => false)) { reducedIdle = true; break; }
    await new Promise((r) => setTimeout(r, 150));
  }
  if (reducedIdle) {
    await new Promise((r) => setTimeout(r, parseFloat(A.probe) * 1000));
    const st = await evaluate('({playing: window.__lp.playing, reduced: window.__lp.reduced, renders: window.__lp.renders, layers: window.__lp.world ? window.__lp.world.layers.length : 0, canvasShown: !!(window.__lp.canvas && window.__lp.canvas.offsetParent && getComputedStyle(window.__lp.canvas).visibility !== "hidden" && getComputedStyle(window.__lp.canvas).opacity !== "0")})');
    console.log(JSON.stringify({ page, probe_seconds: parseFloat(A.probe), reduced_motion_emulated: true, playing: st.playing, player_reduced: st.reduced, renders: st.renders, layers_loaded: st.layers, canvas_shown: st.canvasShown, meaning: 'reduced motion: the player stayed idle and loaded no layers; the page shows its original image' }));
    throw { quiet: true };
  }
  if (!size) {
    const errs = events.filter((e) => e.method === 'Runtime.exceptionThrown').map((e) => e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text);
    throw new Error('the page never became ready' + (errs.length ? ': ' + errs.join(' | ') : ' (is it a bundled living-picture page? does WebGL work here?)'));
  }
  if (A.probe) {
    const t1 = await evaluate('window.__lp.world.time');
    await new Promise((r) => setTimeout(r, parseFloat(A.probe) * 1000));
    const st = await evaluate('({playing: window.__lp.playing, reduced: window.__lp.reduced, time: window.__lp.world.time})');
    console.log(JSON.stringify({ page, probe_seconds: parseFloat(A.probe), reduced_motion_emulated: !!A['reduced-motion'], playing: st.playing, player_reduced: st.reduced, time_before: t1, time_after: st.time }));
    throw { quiet: true };
  }
  if (A.bench) {
    const r = await evaluate(`(function(){ var lp = window.__lp; lp.fixedSize = [${Math.round(width * 2)}, Math.round(${width * 2} * lp.world.size[1] / lp.world.size[0])];
      lp.seek(0); var gl = lp.canvas.getContext('webgl'); var n = 240, t0 = performance.now();
      for (var i = 0; i < n; i++) { lp.advance(1 / 60); } gl.finish(); var px = new Uint8Array(4); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
      var ms = (performance.now() - t0) / n; var v = 0; lp.world.layers.forEach(function (L) { v += L.n || 0; });
      return { ms_per_frame: Math.round(ms * 100) / 100, canvas: [lp.canvas.width, lp.canvas.height], layers: lp.world.layers.length, vertices: v }; })()`);
    console.log(JSON.stringify(Object.assign({ page, note: 'a 60 fps display leaves about 16 ms per frame; phones are several times slower than a desktop' }, r)));
    throw { quiet: true };
  }
  // stills keep the exact width asked for (so frame pixels can equal picture pixels); video needs even sizes
  const w2 = video ? Math.round(width / 2) * 2 : width;
  const height = video ? Math.round(width * size[1] / size[0] / 2) * 2 : Math.round(width * size[1] / size[0]);
  const ptr = (A.pointer || 'none').split(':'), span = (ptr[1] || '2-10').split('-').map(Number);
  const holdAt = (ptr[2] || '0,0').split(',').map(Number), scrollV = A.scroll == null ? null : parseFloat(A.scroll);
  const gusts = A.gusts ? A.gusts.split(',').map(Number) : null;
  const puffs = A.puff ? A.puff.split(';').map((q) => q.split(',').map(Number)) : [];
  const taps = A.tap ? A.tap.split(';').map((q) => q.split(',').map(Number)) : [];
  await evaluate(`(function(){
    var lp = window.__lp; lp.fixedSize = [${w2}, ${height}];
    var kind = ${JSON.stringify(ptr[0])}, a = ${span[0]}, b = ${span[1]}, gusts = ${JSON.stringify(gusts)}, fired = {}, lastT = 0;
    if (gusts) { lp.world.scene.wind = lp.world.scene.wind || {}; lp.world.scene.wind.gust = false; lp.world.scene.wind.opening = null; lp.world.scene.wind.__forced = true; }
    lp.seek(0, function (world, t) {
      if (t < lastT) fired = {};
      lastT = t;
      var sv = ${scrollV === null ? 'null' : scrollV}; if (sv !== null) world.scroll = sv;
      if (kind !== 'none' && t >= a && t <= b) {
        var u = (t - a) / (b - a), k = Math.min(1, Math.min(u, 1 - u) * 8);
        if (kind === 'orbit') lp.setPointer(0.85 * Math.sin(u * 6.283) * k, 0.6 * Math.sin(u * 12.566 + 0.6) * k, true);
        else if (kind === 'corners') { var c4 = [[-1, 0], [1, 0], [0, -1], [0, 1]][Math.min(3, Math.floor(u * 4))]; lp.setPointer(c4[0], c4[1], true); }
        else if (kind === 'hold') lp.setPointer(${holdAt[0] || 0}, ${holdAt[1] || 0}, true);
        else lp.setPointer(0.9 * Math.sin(u * 6.283 * 1.5) * k, 0.1, true);
      } else if (kind !== 'none') lp.setPointer(0, 0, false);
      if (gusts) for (var i = 0; i < gusts.length; i++) if (t >= gusts[i] && !fired[i]) { fired[i] = 1; world.wind.gust({ strength: 1 }); }
      var puffs = ${JSON.stringify(puffs)}, taps = ${JSON.stringify(taps)}, q;
      for (i = 0; i < puffs.length; i++) { q = puffs[i]; if (t >= q[0] && !fired['p' + i]) { fired['p' + i] = 1; world.wind.puff(q[1], q[2], q[3], q[4], q[5] == null ? 0.9 : q[5]); } }
      for (i = 0; i < taps.length; i++) { q = taps[i]; if (t >= q[0] && !fired['t' + i]) { fired['t' + i] = 1; if (lp.tap) lp.tap(q[1], q[2]); } }
    });
    return true; })()`);

  const mime = A.png ? 'image/png' : 'image/jpeg';
  const written = [];
  async function grab(file) {
    // render and read back in one task, then fetch the image in pieces: one very large message can stall the socket
    const len = await evaluate(`(window.__capbuf = window.__lp.snapshot('${mime}', 0.92)).length`);
    let url = '';
    for (let a = 0; a < len; a += 400000) url += await evaluate(`window.__capbuf.slice(${a}, ${a + 400000})`);
    await evaluate('window.__capbuf = null');
    fs.writeFileSync(file, Buffer.from(url.slice(url.indexOf(',') + 1), 'base64'));
    written.push(file);
  }
  const result = { page, size: [w2, height] };
  const samples = [];
  const chNames = A.channels ? String(A.channels).split(',').map((s) => s.trim()).filter(Boolean) : [];
  const tracks = A.track ? String(A.track).split(';').map((s) => { const [id, xy] = s.split(':'); const [x, y] = xy.split(',').map(Number); return [id.trim(), x, y]; }) : [];
  if (times) {
    let now = 0;
    for (const t of times) {
      if (t > now) { await evaluate(`window.__lp.advance(${(t - now).toFixed(4)})`); now = t; } else await evaluate('window.__lp.advance(0)');
      await grab(path.join(out, `${prefix}_t${t.toFixed(2).padStart(5, '0')}.${ext}`));
      if (A.eval) (result.evals = result.evals || []).push({ t, value: await evaluate(`(function(lp, world){ return (${A.eval}); })(window.__lp, window.__lp.world)`) });
      if (A.channels || A.track) samples.push({ t, v: await evaluate(`(function(lp, world){ return {
        ch: ${JSON.stringify(chNames)}.map(function (n) { return world.ch.v[n] == null ? null : world.ch.v[n]; }),
        tr: ${JSON.stringify(tracks)}.map(function (k) { var p = lp.probe(k[0], k[1], k[2]); return Math.hypot(p[0], p[1]); }) }; })(window.__lp, window.__lp.world)`) });
    }
    if (A.channels) {
      result.channels = {};
      chNames.forEach((n, j) => {
        const vals = samples.map((s) => s.v.ch[j]);
        if (vals.some((v) => v == null)) { result.channels[n] = 'no such channel'; return; }
        const mag = vals.map(Math.abs), top = Math.max(...mag);
        const peaks = [];
        for (let i = 0; i < vals.length; i++) {
          if (mag[i] >= 0.5 * top && top > 0 && (i === 0 || mag[i] >= mag[i - 1]) && (i === vals.length - 1 || mag[i] > mag[i + 1]))
            peaks.push([samples[i].t, Math.round(vals[i] * 1000) / 1000]);
        }
        peaks.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
        result.channels[n] = { min: Math.min(...vals), max: Math.max(...vals), peaks: peaks.slice(0, 10).sort((a, b) => a[0] - b[0]) };
      });
    }
    if (A.track) {
      result.tracks = tracks.map((k, j) => {
        const d = samples.map((s) => s.v.tr[j]); const i = d.indexOf(Math.max(...d));
        return { layer: k[0], at: [k[1], k[2]], max_px: Math.round(d[i] * 100) / 100, when: samples[i].t,
                 median_px: Math.round([...d].sort((a, b) => a - b)[Math.floor(d.length / 2)] * 100) / 100 };
      });
    }
    result.frames = written.slice();
    result.events = await evaluate('window.__lp.world.events || []');     // gusts and taps so far, with their times
  }
  if (video) {
    const dir = path.join(out, prefix + '_frames'); fs.mkdirSync(dir, { recursive: true });
    // frames left by an earlier, longer run would be encoded after this run's own
    for (const f of fs.readdirSync(dir)) if (/^v_\d{5}\.(jpg|png)$/.test(f)) fs.rmSync(path.join(dir, f));
    await evaluate('window.__lp.seek(0, window.__lp.script)');
    const n = Math.round(video * fps);
    for (let i = 0; i < n; i++) {
      if (i) await evaluate(`window.__lp.advance(${(1 / fps).toFixed(5)})`);
      await grab(path.join(dir, `v_${String(i).padStart(5, '0')}.${ext}`));
    }
    const mp4 = path.join(out, prefix + '.mp4');
    const mac = process.platform === 'darwin';
    const why = (r) => (r.error ? r.error.code === 'ENOENT' ? 'not found' : r.error.message : r.signal ? 'was killed by ' + r.signal : 'exited with ' + r.status)
      + (String(r.stderr || '').trim() ? ': ' + String(r.stderr).trim() : '');
    let encoder = A.encoder || 'auto';
    const auto = encoder === 'auto';
    if (auto) {
      // an ffmpeg on PATH that cannot run (a library lost in an upgrade, say) counts as none
      const probe = spawnSync('ffmpeg', ['-version']);
      encoder = (!probe.error && probe.status === 0) || !mac ? 'ffmpeg' : 'avfoundation';
      if (encoder === 'avfoundation' && probe.error?.code !== 'ENOENT') result.ffmpeg_error = 'ffmpeg ' + why(probe);
    }
    result.encoder = encoder;
    result.codec = A.hevc ? 'hevc' : 'h264';
    let failed = null;
    if (encoder === 'ffmpeg') {
      const codec = A.hevc ? ['-c:v', 'libx265', '-crf', '19', '-preset', 'medium', '-tag:v', 'hvc1', '-x265-params', 'log-level=error']
        : ['-c:v', 'libx264', '-crf', '19', '-preset', 'medium'];
      const ff = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(fps), '-i', path.join(dir, `v_%05d.${ext}`),
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', ...codec, '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4]);
      if (ff.error || ff.status !== 0) failed = 'ffmpeg ' + why(ff);
      // auto means whichever works: on macOS a failed ffmpeg hands the frames to AVFoundation, and says so
      if (failed && auto && mac) { result.ffmpeg_error = failed; result.encoder = encoder = 'avfoundation'; failed = null; }
      else if (failed && mac) failed += ' (--encoder avfoundation needs no ffmpeg)';
    }
    if (encoder === 'avfoundation') {
      const tool = swiftHelper('lpencode');
      const r = tool.exe && spawnSync(tool.exe, [dir, mp4, '--fps', String(fps), ...(A.hevc ? ['--hevc'] : [])], { encoding: 'utf8' });
      if (!tool.exe) failed = tool.problem;
      else if (r.error || r.status !== 0) failed = String(r.error || r.stderr).trim();
      else try {
        // lpencode may switch to HEVC (H.264 there takes at most 4096 px a side); its JSON says what it wrote
        const lp = JSON.parse(r.stdout);
        result.codec = lp.codec; result.rate_control = lp.rate_control;
        if (lp.note) result.note = lp.note;
      } catch (e) { /* the video exists either way */ }
    }
    if (failed) result.video_error = failed + '; frames kept in ' + dir;
    else { result.video = mp4; if (!A['keep-frames']) fs.rmSync(dir, { recursive: true, force: true }); }
  }
  const errs = events.filter((e) => e.method === 'Runtime.exceptionThrown').map((e) => e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text);
  if (errs.length) result.page_errors = errs.slice(0, 5);
  console.log(JSON.stringify(result));
} catch (e) {
  if (!(e && e.quiet)) { console.error(String(e.message || e)); process.exitCode = 1; }
} finally {
  try { ws.close(); } catch (e) { /* ignore */ }
  cleanup();
}
