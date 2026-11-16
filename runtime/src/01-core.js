/* living-picture runtime, part 1: maths, seeded randomness, WebGL plumbing.
 * No dependencies. Everything hangs off one global: LivingPicture. */
var LP = {};

/* ---------- seeded randomness: the same seed always plays the same film ---------- */
LP.rng = function (seed) {
  var s = (Math.imul((seed >>> 0) ^ 0x9E3779B9, 0x85EBCA6B) >>> 0) || 1;      // scrambled, so small seeds are as good as any
  var f = function () {
    s |= 0; s = (s + 0x6D2B79F5) | 0;
    var t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  f.range = function (a, b) { return a + (b - a) * f(); };
  f.pick = function (v) { return Array.isArray(v) ? f.range(v[0], v[1]) : v; };
  // a waiting time from a range that is never close to the one before it: chance alone can deal
  // several near-equal gaps in a row, and that reads as a timetable
  f.gap = function (v, mem) {
    if (!Array.isArray(v)) return v;
    var u = f(), n = 0;
    while (mem && mem.gapU != null && Math.abs(u - mem.gapU) < 0.22 && n++ < 4) u = f();
    if (mem) mem.gapU = u;
    return v[0] + (v[1] - v[0]) * u;
  };
  return f;
};
LP.hash = function (n) { var x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };
LP.hash2 = function (x, y) { var v = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return v - Math.floor(v); };
/* smooth value noise, 0..1 */
LP.noise1 = function (x) {
  var i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
  return LP.hash(i) * (1 - u) + LP.hash(i + 1) * u;
};
LP.noise2 = function (x, y) {
  var ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
  var ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
  var a = LP.hash2(ix, iy), b = LP.hash2(ix + 1, iy), c = LP.hash2(ix, iy + 1), d = LP.hash2(ix + 1, iy + 1);
  return (a * (1 - ux) + b * ux) * (1 - uy) + (c * (1 - ux) + d * ux) * uy;
};
/* two octaves, centred on 0, roughly -1..1: never visibly periodic */
LP.wander = function (x, seed) {
  return (LP.noise1(x + seed * 17.3) - 0.5) * 1.4 + (LP.noise1(x * 2.37 + seed * 5.1 + 40) - 0.5) * 0.6;
};
LP.clamp = function (x, a, b) { return x < a ? a : (x > b ? b : x); };
LP.lerp = function (a, b, t) { return a + (b - a) * t; };
LP.smooth = function (a, b, x) { var t = LP.clamp((x - a) / (b - a || 1e-9), 0, 1); return t * t * (3 - 2 * t); };
LP.ease = function (t) { t = LP.clamp(t, 0, 1); return t * t * (3 - 2 * t); };
LP.D2R = Math.PI / 180;

/* ---------- a damped spring: how a thing with weight answers a push ---------- */
LP.Spring = function (freq, damp) {
  this.w = 2 * Math.PI * (freq || 1); this.z = damp == null ? 0.35 : damp; this.x = 0; this.v = 0;
};
LP.Spring.prototype.step = function (target, dt) {
  var a = this.w * this.w * (target - this.x) - 2 * this.z * this.w * this.v;
  this.v += a * dt; this.x += this.v * dt; return this.x;
};

/* ---------- WebGL ---------- */
LP.VS = [
  'attribute vec2 a_pos; attribute vec2 a_uv; attribute float a_depth; attribute float a_fade;',
  'uniform vec2 u_size; uniform vec2 u_cam; uniform float u_focus; uniform mat3 u_xf; uniform vec3 u_view; uniform float u_hold;',
  'varying vec2 v_uv; varying vec2 v_pic; varying float v_depth; varying float v_fade; varying vec2 v_world;',
  'void main(){',
  '  vec3 p = u_xf * vec3(a_pos, 1.0);',
  '  v_world = p.xy;',
  '  float ek = 1.0;',
  '  if (u_hold > 0.0) { vec2 e = min(a_pos, u_size - a_pos); ek = smoothstep(0.0, u_hold, min(e.x, e.y)); }',
  '  p.xy += u_cam * (u_focus - a_depth) * ek;',
  '  v_pic = p.xy; v_uv = a_uv; v_depth = a_depth; v_fade = a_fade;',
  '  vec2 c = (p.xy - u_view.xy) * u_view.z / u_size * 2.0 - 1.0;',
  '  gl_Position = vec4(c.x, -c.y, 0.0, 1.0);',
  '}'].join('\n');

LP.FS = [
  '#ifdef GL_FRAGMENT_PRECISION_HIGH', 'precision highp float;', '#else', 'precision mediump float;', '#endif',
  'varying vec2 v_uv; varying vec2 v_pic; varying float v_depth; varying float v_fade; varying vec2 v_world;',
  'uniform sampler2D u_lightTex; uniform float u_relight;',
  'uniform sampler2D u_tex; uniform sampler2D u_flowTex; uniform sampler2D u_blinkTex; uniform sampler2D u_blinkMask; uniform sampler2D u_blinkPaint;',
  'uniform int u_nblink; uniform vec4 u_ba[6]; uniform vec4 u_bb[6]; uniform vec3 u_blinkPixel;',
  'uniform vec4 u_rect; uniform vec2 u_size; uniform float u_opacity; uniform float u_time; uniform float u_mirror; uniform float u_blend;',
  // local warps (small features: eyes, ears, a head): 4 vec4 each
  'uniform int u_nwarp; uniform vec4 u_wa[6]; uniform vec4 u_wb[6]; uniform vec4 u_wc[6]; uniform vec4 u_wd[6];',
  // flow along the paint: dir.xy, phase (cycles), reach px | useMap, on, 0, 0 | soft box centre.xy half.xy
  'uniform vec4 u_flow; uniform vec4 u_flow2; uniform vec4 u_flowBox;',
  // ripples: x, y, age s, strength | speed px/s, width px, life s, shine
  'uniform int u_nrip; uniform vec4 u_rip[4]; uniform vec4 u_ripP;',
  // passing shade: amount, scale px, offset.xy | horizonY, nearY, cover, soft | tint | receive, upright, footY
  'uniform vec4 u_shade; uniform vec4 u_shade2; uniform vec3 u_shadeTint; uniform vec3 u_recv;',
  'uniform vec3 u_gain; uniform vec3 u_lift;',
  // lights in the picture: x, y, radius, power | r, g, b, mode(0 screen, 1 multiply)
  'uniform int u_nglow; uniform vec4 u_ga[4]; uniform vec4 u_gb[4]; uniform float u_glowRecv;',
  'float h2(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }',
  'float vn(vec2 p){ vec2 i = floor(p), f = fract(p); vec2 u = f*f*(3.0-2.0*f);',
  '  return mix(mix(h2(i), h2(i+vec2(1.0,0.0)), u.x), mix(h2(i+vec2(0.0,1.0)), h2(i+vec2(1.0,1.0)), u.x), u.y); }',
  'float fbm(vec2 p){ return vn(p)*0.6 + vn(p*2.13+7.1)*0.28 + vn(p*4.3+3.7)*0.12; }',
  'vec2 rot(vec2 v, float a){ float c = cos(a), s = sin(a); return vec2(c*v.x - s*v.y, s*v.x + c*v.y); }',
  'vec2 warp(vec2 p){',
  '  for (int i = 0; i < 6; i++) {',
  '    if (i >= u_nwarp) break;',
  '    vec4 a = u_wa[i], b = u_wb[i], c = u_wc[i], d = u_wd[i];',
  '    if (c.w < 0.5) {',
  // pose: a = pivot.xy, centre.xy | b = rx, ry, regionRot, core | c = angle, dx, dy, 0 | d = scale
  '      vec2 q = rot(p - a.zw, -b.z) / b.xy;',
  '      float f = 1.0 - smoothstep(b.w, 1.0, length(q));',
  '      if (f > 0.0) {',
  '        vec2 v = p - a.xy - c.yz * f;',
  '        p = a.xy + rot(v, -c.x * f) / (1.0 + (d.x - 1.0) * f);',
  '      }',
  '    }',
  '  }',
  '  return p;',
  '}',
  'vec4 samp(vec2 p){',
  '  vec2 uv = (p - u_rect.xy) / u_rect.zw;',
  '  if (u_mirror > 0.5) uv = 1.0 - abs(1.0 - abs(uv));',
  '  return texture2D(u_tex, uv);',
  '}',
  // Only source paint inside the eye is moved. Its evacuated pixels use a local
  // clean plate; face coordinates outside that finite domain never change.
  'vec4 cleanSamp(vec2 p){ return texture2D(u_blinkTex, (p-u_rect.xy)/u_rect.zw); }',
  'vec4 eyePaint(vec2 q, vec4 a, vec4 b, out float coverage){',
  '  coverage=0.0; if (abs(q.x)>=a.z || abs(q.y)>=a.w) return vec4(0.0);',
  '  vec2 src=a.xy+rot(q,b.x); coverage=texture2D(u_blinkMask,(src-u_rect.xy)/u_rect.zw).r;',
  '  return texture2D(u_blinkPaint,(src-u_rect.xy)/u_rect.zw);',
  '}',
  'vec4 blinkPaint(vec2 p, vec4 col){',
  '  for (int i=0; i<6; i++){',
  '    if (i>=u_nblink) break;',
  '    vec4 a=u_ba[i], b=u_bb[i];',
  '    vec2 q=rot(p-a.xy,-b.x);',
  '    float scale=mix(1.0,b.w,b.y), line=b.z*a.w;',
  '    vec2 from=vec2(q.x,line+(q.y-line)/scale);',
  '    float footprint=(abs(cos(b.x))*u_blinkPixel.y+abs(sin(b.x))*u_blinkPixel.x)/scale;',
  '    float footprintX=u_blinkPixel.x;',
  '    #ifdef LP_BLINK_DERIVATIVES',
  '    footprint=abs(dFdx(from.y))+abs(dFdy(from.y));',
  '    footprintX=abs(dFdx(from.x))+abs(dFdy(from.x));',
  '    #endif',
  '    if (abs(q.x)>=a.z || abs(q.y)>=a.w || b.y<=0.0) continue;',
  '    float texel=u_blinkPixel.z;',
  '    float spread=max(0.0,footprint-texel), spreadX=max(0.0,footprintX-texel);',
  '    vec4 moved=vec4(0.0); float movedMask=0.0;',
  '    if (spread<0.01 && spreadX<0.01) moved=eyePaint(from,a,b,movedMask);',
  '    else {',
  '      spread=max(spread,0.001); spreadX=max(spreadX,0.001);',
  '      float lo=max(-a.w,from.y-spread*0.5), hi=min(a.w,from.y+spread*0.5);',
  '      float left=max(-a.z,from.x-spreadX*0.5), right=min(a.z,from.x+spreadX*0.5);',
  '      if (hi>lo && right>left) {',
  // At least one tap per source pixel, including very tall/narrow or rotated eyes.
  // build validates that the fixed WebGL1 loop can cover the complete source span.
  '        float count=ceil((hi-lo)/texel), countX=ceil((right-left)/texel);',
  '        for (int j=0;j<256;j++){',
  '          if (float(j)>=count) break;',
  '          float sy=mix(lo,hi,(float(j)+0.5)/count);',
  '          for (int k=0;k<256;k++){',
  '            if(float(k)>=countX) break;',
  '            float sx=mix(left,right,(float(k)+0.5)/countX);',
  '            float coverage; moved+=eyePaint(vec2(sx,sy),a,b,coverage); movedMask+=coverage;',
  '          }',
  '        }',
  '        float weight=((hi-lo)/spread)*((right-left)/spreadX)/(count*countX); moved*=weight; movedMask*=weight;',
  '      }',
  '    }',
  '    col=cleanSamp(p)*(1.0-clamp(movedMask,0.0,1.0))+moved;',
  '  }',
  '  return col;',
  '}',
  'void main(){',
  '  vec2 p = u_rect.xy + v_uv * u_rect.zw;',
  '  if (u_nwarp > 0) p = warp(p);',
  '  float shine = 0.0;',
  '  for (int i = 0; i < 4; i++) {',
  '    if (i >= u_nrip) break;',
  '    vec2 d = p - u_rip[i].xy; float r = length(d) + 0.001;',
  '    float age = u_rip[i].z;',
  '    float fade = (1.0 - smoothstep(u_ripP.z * 0.3, u_ripP.z, age)) * smoothstep(0.0, 0.15, age) * u_rip[i].w;',
  '    float x = (r - age * u_ripP.x) / u_ripP.y;',
  '    float ring = exp(-x * x * 0.5) * fade;',
  '    p += d / r * sin(x * 2.6) * ring * u_ripP.y * 0.5;',
  '    shine += max(0.0, cos(x * 2.6 - 0.8)) * ring * u_ripP.w;',
  '  }',
  '  vec4 col;',
  '  if (u_flow2.y > 0.5) {',
  '    float m = 1.0; vec2 dir = u_flow.xy;',
  '    if (u_flowBox.z > 0.0) { vec2 e = abs(p - u_flowBox.xy) / u_flowBox.zw; m = 1.0 - smoothstep(0.7, 1.0, max(e.x, e.y)); }',
  '    if (u_flow2.x > 0.5) {',
  '      vec4 f = texture2D(u_flowTex, p / u_size);',
  '      dir = (f.xy * 2.0 - 1.0); m *= f.z;',
  '    }',
  '    float ph = u_flow.z + vn(p / 90.0) * 0.4;',
  '    float a = fract(ph), b = fract(ph + 0.5);',
  '    vec4 ca = samp(p - dir * (a - 0.5) * u_flow.w * m);',
  '    vec4 cb = samp(p - dir * (b - 0.5) * u_flow.w * m);',
  '    col = mix(ca, cb, abs(a - 0.5) * 2.0);',
  '  } else col = samp(p);',
  '  if (u_nblink>0) col=blinkPaint(p,col);',
  '  vec3 rgb = col.rgb + shine * col.a;',
  // relight: paint keeps the light of where it was painted; moved, it takes the light where it is now.
  // Rest position of this texel vs where it is now; at rest the two samples are the same, so the factor is exactly 1.
  '  if (u_relight > 0.0) {',
  '    vec3 lh = texture2D(u_lightTex, v_world / u_size).rgb;',
  '    vec3 lr = texture2D(u_lightTex, (u_rect.xy + v_uv * u_rect.zw) / u_size).rgb;',
  '    rgb = min(rgb * mix(vec3(1.0), lh / max(lr, vec3(0.02)), u_relight), vec3(col.a));',
  '  }',
  '  if (u_shade.x > 0.0 && u_recv.x > 0.0) {',
  '    vec2 g = v_pic; vec2 q;',
  '    if (u_recv.y > 0.5) g.y = u_recv.z;',
  '    if (u_shade2.y - u_shade2.x > 1.0) {',
  '      float t = clamp((g.y - u_shade2.x) / (u_shade2.y - u_shade2.x), 0.05, 1.5);',
  '      float gz = 1.0 / t;',
  '      q = vec2((g.x - u_size.x * 0.5) * gz, gz * u_size.y * 0.9) / u_shade.y + u_shade.zw;',
  '    } else q = g / u_shade.y + u_shade.zw;',
  '    float cl = smoothstep(u_shade2.z - u_shade2.w, u_shade2.z + u_shade2.w, fbm(q));',
  '    rgb *= mix(vec3(1.0), u_shadeTint, cl * u_shade.x * u_recv.x);',
  '  }',
  '  rgb = rgb * u_gain + u_lift * (col.a - rgb);',
  '  for (int i = 0; i < 4; i++) {',
  '    if (i >= u_nglow) break;',
  '    float d = length(v_pic - u_ga[i].xy) / u_ga[i].z;',
  '    float k = exp(-d * d * 2.2) * u_ga[i].w * u_glowRecv;',
  '    if (u_gb[i].w < 0.5) rgb += (col.a - rgb) * u_gb[i].rgb * k;',
  '    else rgb *= mix(vec3(1.0), u_gb[i].rgb, clamp(k, 0.0, 1.0));',
  '  }',
  '  vec4 o = vec4(rgb, col.a) * (u_opacity * v_fade);',
  // multiply (a transparent glaze): the texture holds transmittance T, premultiplied;
  // with blendFunc(ZERO, SRC_COLOR) the ground becomes ground * (1 - a * (1 - T))
  '  if (u_blend > 0.5) o = vec4(vec3(1.0 - o.a) + o.rgb, 1.0);',
  '  gl_FragColor = o;',
  '}'].join('\n');

LP.GL = function (canvas) {
  var opts = { alpha: true, premultipliedAlpha: true, antialias: true, preserveDrawingBuffer: false, powerPreference: 'low-power' };
  var gl = canvas.getContext('webgl', opts) || canvas.getContext('experimental-webgl', opts);
  if (!gl) return null;
  function sh(type, src) {
    var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { var why = gl.getShaderInfoLog(s); gl.deleteShader(s); throw new Error('living-picture shader: ' + why); }
    return s;
  }
  var prog = gl.createProgram(), vs = null, fs = null;
  try {
    var derivative = gl.getExtension('OES_standard_derivatives');
    vs = sh(gl.VERTEX_SHADER, LP.VS);
    fs = sh(gl.FRAGMENT_SHADER, (derivative ? '#extension GL_OES_standard_derivatives : enable\n#define LP_BLINK_DERIVATIVES\n' : '') + LP.FS);
    gl.attachShader(prog, vs); gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, 'a_pos'); gl.bindAttribLocation(prog, 1, 'a_uv'); gl.bindAttribLocation(prog, 2, 'a_depth'); gl.bindAttribLocation(prog, 3, 'a_fade');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error('living-picture link: ' + gl.getProgramInfoLog(prog));
  } catch (error) { gl.deleteProgram(prog); throw error; }
  finally { if (vs) gl.deleteShader(vs); if (fs) gl.deleteShader(fs); }
  gl.useProgram(prog);
  var U = {}, n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
  for (var i = 0; i < n; i++) {
    var info = gl.getActiveUniform(prog, i), name = info.name.replace(/\[0\]$/, '');
    U[name] = gl.getUniformLocation(prog, info.name);
  }
  gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.DEPTH_TEST);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  return { gl: gl, prog: prog, U: U, derivatives: !!derivative, maxTex: Math.min(4096, gl.getParameter(gl.MAX_TEXTURE_SIZE)) };
};

LP.texture = function (G, img) {
  var gl = G.gl, src = img, w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
  if (Math.max(w, h) > G.maxTex) {
    var k = G.maxTex / Math.max(w, h), c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * k)); c.height = Math.max(1, Math.round(h * k));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); src = c;
  }
  var t = gl.createTexture();
  try {
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  } catch (error) { gl.deleteTexture(t); throw error; }
  return t;
};

/* read a layer's alpha at low resolution: used to find what is solid, how far
 * each bit is from its root, and to split loose marks into sprites */
LP.alphaGrid = function (img, cell) {
  var w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
  var gw = Math.max(2, Math.round(w / cell)), gh = Math.max(2, Math.round(h / cell));
  var c = document.createElement('canvas'); c.width = gw; c.height = gh;
  var x = c.getContext('2d', { willReadFrequently: true });
  x.drawImage(img, 0, 0, gw, gh);
  var d = x.getImageData(0, 0, gw, gh).data, a = new Float32Array(gw * gh);
  for (var i = 0; i < gw * gh; i++) a[i] = d[i * 4 + 3] / 255;
  return { w: gw, h: gh, a: a, sx: w / gw, sy: h / gh };
};
