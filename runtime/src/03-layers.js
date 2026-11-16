/* living-picture runtime, part 3: layers and the ways they can be built to move. */

LP.PRESETS = {
  // bend: degrees at the tip under a full gust (tip travel = bend x length, so long stiff things need small numbers);
  // freq: natural sway (Hz); damp: how fast it settles (0 rings, 1 dead); lag: seconds the tip trails the root;
  // flutter: px of local leaf-tremble while the gust is on it; power: how much stiffer the base is than the tip
  tree:   { bend: 2.0, freq: 0.45, damp: 0.25, lag: 0.08, flutter: 3.4, power: 1.7 },
  bush:   { bend: 4.4, freq: 0.9,  damp: 0.3,  lag: 0.1,  flutter: 4.0, power: 1.3 },
  plant:  { bend: 8,   freq: 1.25, damp: 0.28, lag: 0.12, flutter: 3.0, power: 1.3 },
  grass:  { bend: 15,  freq: 1.8,  damp: 0.3,  lag: 0.1,  flutter: 1.6, power: 1.5 },
  branch: { bend: 3.2, freq: 0.75, damp: 0.22, lag: 0.15, flutter: 4.0, power: 1.4 },
  vine:   { bend: 10,  freq: 0.65, damp: 0.14, lag: 0.3,  flutter: 2.5, power: 1.15 },
  ivy:    { bend: 0,   freq: 1,    damp: 0.5,  lag: 0,    flutter: 3.6, power: 1, weight: 'none' },
  field:  { bend: 0,   freq: 1,    damp: 0.5,  lag: 0,    flutter: 2.0, power: 1, weight: 'none', lean: [10, 3] },
  cloth:  { bend: 10,  freq: 0.9,  damp: 0.35, lag: 0.3,  flutter: 0,   power: 1.2, ripple: 7, ripples: 1.4, rippleSpeed: 1.5 },
  flag:   { bend: 14,  freq: 1.2,  damp: 0.25, lag: 0.3,  flutter: 0,   power: 1.1, ripple: 10, ripples: 1.6, rippleSpeed: 2.2 },
  hair:   { bend: 7,   freq: 1.0,  damp: 0.32, lag: 0.3,  flutter: 0.8, power: 1.3, ripple: 3, ripples: 1.2, rippleSpeed: 1.2 },
  tail:   { bend: 12,  freq: 0.8,  damp: 0.4,  lag: 0.3,  flutter: 0,   power: 1.3 },
  hang:   { bend: 12,  freq: 0.8,  damp: 0.08, lag: 0.1,  flutter: 0,   power: 1.0 }
};

/* Evaluate attachment dependencies independently of painting order. */
LP.layerOrder = function (defs) {
  var by = Object.create(null), state = Object.create(null), order = [];
  function localDepth(d) {
    return d.depth && d.depth.perRoot || (d.motion || []).some(function (m) {
      return (m.roots || (m.root ? [m.root] : [])).some(function (r) { return r.depth != null; });
    });
  }
  defs.forEach(function (d) {
    if (!d.id || by[d.id]) throw new Error('living-picture: missing or duplicate layer id ' + d.id);
    by[d.id] = d;
  });
  function visit(id) {
    if (state[id] === 1) throw new Error('living-picture: cyclic layer/depth dependency at ' + id);
    if (state[id] === 2) return;
    var d = by[id]; if (!d) throw new Error('living-picture: missing layer reference ' + id);
    state[id] = 1;
    var region = d.sprites && d.sprites.region;
    if (region && (!Array.isArray(region) || region.length !== 4 || !region.every(Number.isFinite) || region[2] <= region[0] || region[3] <= region[1]))
      throw new Error('living-picture: sprite region for ' + id + ' must be [left, top, right, bottom]');
    if (d.parent) {
      visit(d.parent); var p = by[d.parent];
      if (p.fixed) throw new Error('living-picture: fixed screen overlay cannot be a motion parent');
      if (d.sprites && d.sprites.depth != null) throw new Error('living-picture: child sprites must inherit parent depth');
      if (localDepth(p) || localDepth(d)) throw new Error('living-picture: attachment cannot carry per-root depth; keep shared body depth on one layer');
      if ((p.warps || []).length || p.flow || p.sprites || p.bulge)
        throw new Error('living-picture: parent ' + p.id + ' uses texture/sprite deformation; keep attached parts on one layer');
      if ((p.motion || []).length && !d.attach)
        throw new Error('living-picture: deforming parent ' + p.id + ' needs an explicit attach point');
      if (d.bulge) throw new Error('living-picture: child ' + id + ' cannot override parent depth with bulge');
    }
    if (d.depth && d.depth.on) visit(d.depth.on);
    if (d.fixed && (d.parent || (d.motion || []).length || (d.warps || []).length || d.sprites || d.flow || d.rigid || d.bulge || d.ripples))
      throw new Error('living-picture: fixed layer ' + id + ' must contain original paint only');
    if (d.blend != null && d.blend !== 'normal' && d.blend !== 'multiply')
      throw new Error('living-picture: layer ' + id + ' blend must be "normal" or "multiply"');
    if (d.blend === 'multiply' && d.fixed)
      throw new Error('living-picture: fixed layer ' + id + ' cannot use multiply blending');
    if (d.relight != null && d.relight !== false && d.relight !== true && !(typeof d.relight === 'number' && d.relight >= 0 && d.relight <= 1))
      throw new Error('living-picture: layer ' + id + ' relight must be true, false or a number in [0, 1]');
    if (d.relight && (d.fixed || d.blend === 'multiply'))
      throw new Error('living-picture: layer ' + id + ' cannot relight: a fixed layer never moves, and a multiply glaze already takes the light of its ground');
    state[id] = 2; order.push(id);
  }
  defs.forEach(function (d) { visit(d.id); });
  return order;
};
/* Fixed overlays have one supported compositing contract: verified opaque original,
   binary source-pixel mask, native rect and lossless pixels. Missing evidence fails
   closed; soft/transparent preservation needs a separate compositing design. */
LP.fixedContract = function (scene) {
  var fixed = scene.layers.filter(function (d) { return d.fixed; });
  if (!fixed.length) return;
  var ac = scene.assetContract, records = ac && ac.fixedLayers;
  if (!ac || ac.originalOpaque !== true || !Array.isArray(records))
    throw new Error('living-picture: fixed overlays require verified opaque-source-hard-mask asset metadata');
  fixed.forEach(function (d) {
    var matches = records.filter(function (r) { return r.id === d.id; }), r = matches[0];
    var rect = d.rect || [0, 0, scene.size[0], scene.size[1]];
    if (matches.length !== 1 || r.mode !== 'opaque-source-hard-mask' || r.source_pixel_equal !== true ||
        r.binary_alpha !== true || r.lossless !== true || !Array.isArray(r.rect) || r.rect.length !== 4 ||
        !r.rect.every(function (v, i) { return Number.isInteger(v) && v === rect[i]; }))
      throw new Error('living-picture: fixed layer ' + d.id + ' lacks matching verified opaque source pixels and binary mask');
  });
};
LP.MAX_WARPS = 6;
LP.activeWarps = function (warps, channels, id) {
  var active = warps.filter(function (w) {
    if (w.type === 'blink') return LP.clamp(channels.get(w.closed == null ? 'blink' : w.closed), 0, 1) > 0;
    return Math.abs(channels.get(w.angle) * LP.D2R) >= 1e-5 || Math.abs(channels.get(w.x)) >= 1e-3 ||
      Math.abs(channels.get(w.y)) >= 1e-3 || (w.scale != null && Math.abs(channels.get(w.scale)) >= 1e-5);
  });
  if (active.length > LP.MAX_WARPS) throw new Error('living-picture: layer ' + id + ' has ' + active.length + ' active warps; capacity is ' + LP.MAX_WARPS);
  return active;
};

/* `mask` is a required picture-coordinate polygon around the EXISTING eye paint,
 * including its soft edges. Explicit mask:'ellipse' declares all of r as paint. Harmonic
 * boundary continuation supplies evacuated paint. Original RGBA and mask
 * coverage move together, preserving light, colored and transparent paint.
 * This pure function exposes preparation for independent image fixture tests. */
LP.blinkPlateData = function (pixels, width, height, rect, warps) {
  var out = new Uint8ClampedArray(pixels), coverage = new Uint8ClampedArray(pixels.length), sourcePaint = new Uint8ClampedArray(pixels.length), sx = rect[2]/width, sy = rect[3]/height;
  var texel = Math.min(sx,sy), eyes = warps.filter(function(w){return w.type==='blink';}), domains=[];
  function point(poly,x,y) {
    var inside=false;
    for (var i=0,j=poly.length-1;i<poly.length;j=i++) {
      var a=poly[i],b=poly[j];
      if ((a[1]>y)!==(b[1]>y) && x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]) inside=!inside;
    }
    return inside;
  }
  eyes.forEach(function(w){
    if(w.mask==null)throw new Error('living-picture: blink needs an explicit source-paint mask; r alone does not establish paint ownership');
    if (!Array.isArray(w.at)||!Array.isArray(w.r)||w.at.length!==2||w.r.length!==2||
        !w.at.concat(w.r).every(Number.isFinite)||w.r[0]<=0||w.r[1]<=0||
        !Number.isFinite(w.rot||0)||(w.line!=null&&(!Number.isFinite(w.line)||Math.abs(w.line)>1)))
      throw new Error('living-picture: blink needs finite at, positive r, rotation and line in [-1,1]');
    if (2*Math.max(w.r[0],w.r[1])/texel>256) throw new Error('living-picture: blink axis exceeds 256 source samples; reduce source resolution');
    var a=(w.rot||0)*LP.D2R,co=Math.cos(a),si=Math.sin(a),rx=w.r[0],ry=w.r[1];
    function local(x,y){x-=w.at[0];y-=w.at[1];return [co*x+si*y,-si*x+co*y];}
    var corners=[[-rx,-ry],[rx,-ry],[rx,ry],[-rx,ry]].map(function(p){return [w.at[0]+co*p[0]-si*p[1],w.at[1]+si*p[0]+co*p[1]];});
    if(corners.some(function(p){return p[0]<rect[0]+sx||p[1]<rect[1]+sy||p[0]>rect[0]+rect[2]-sx||p[1]>rect[1]+rect[3]-sy;}))
      throw new Error('living-picture: blink domain needs an observed one-pixel source halo inside its layer texture');
    var domain={corners:corners,axes:[[co,si],[-si,co]]};
    domains.forEach(function(other){
      var separate=domain.axes.concat(other.axes).some(function(axis){
        var p=domain.corners.map(function(v){return v[0]*axis[0]+v[1]*axis[1];});
        var q=other.corners.map(function(v){return v[0]*axis[0]+v[1]*axis[1];});
        var halo=2*(Math.abs(axis[0])*sx+Math.abs(axis[1])*sy);
        return Math.max.apply(null,p)+halo<=Math.min.apply(null,q)||Math.max.apply(null,q)+halo<=Math.min.apply(null,p);
      });
      if (!separate) throw new Error('living-picture: blink source-sampling halos overlap; separate the source-paint regions');
    });
    domains.push(domain);
    if (w.mask!=='ellipse'&&(!Array.isArray(w.mask)||w.mask.length<3||!w.mask.every(function(p){
      if(!Array.isArray(p)||p.length!==2||!p.every(Number.isFinite))return false;
      var q=local(p[0],p[1]);return Math.abs(q[0])<rx&&Math.abs(q[1])<ry;
    }))) throw new Error('living-picture: blink mask must be a finite source-paint polygon strictly inside r');
    var left=Math.max(1,Math.floor((Math.min.apply(null,corners.map(function(p){return p[0];}))-rect[0])/sx)-1);
    var top=Math.max(1,Math.floor((Math.min.apply(null,corners.map(function(p){return p[1];}))-rect[1])/sy)-1);
    var right=Math.min(width-2,Math.ceil((Math.max.apply(null,corners.map(function(p){return p[0];}))-rect[0])/sx)+1);
    var bottom=Math.min(height-2,Math.ceil((Math.max.apply(null,corners.map(function(p){return p[1];}))-rect[1])/sy)+1);
    var mask=new Uint8Array(width*height),indices=[];
    for(var y=top;y<=bottom;y++)for(var x=left;x<=right;x++){
      var px=rect[0]+(x+0.5)*sx,py=rect[1]+(y+0.5)*sy,q=local(px,py);
      if(w.mask!=='ellipse'?point(w.mask,px,py):(q[0]*q[0]/(rx*rx)+q[1]*q[1]/(ry*ry)<1)){var index=y*width+x;mask[index]=1;indices.push(index);}
    }
    if(!indices.length)throw new Error('living-picture: blink mask contains no source pixels');
    indices.forEach(function(index){coverage[index*4]=coverage[index*4+1]=coverage[index*4+2]=coverage[index*4+3]=255;});
    indices.forEach(function(index){for(var c=0;c<4;c++)sourcePaint[index*4+c]=pixels[index*4+c];});
    var values=new Float32Array(pixels.length);
    for(var v=0;v<pixels.length;v+=4){
      var alpha=pixels[v+3]/255;values[v]=pixels[v]*alpha;values[v+1]=pixels[v+1]*alpha;values[v+2]=pixels[v+2]*alpha;values[v+3]=pixels[v+3];
    }
    // A boundary-interpolated start already reproduces linear painted gradients;
    // it avoids spending thousands of sweeps diffusing a flat average into them.
    for(var begin=0;begin<indices.length;){
      var end=begin;
      while(end+1<indices.length&&indices[end+1]===indices[end]+1&&Math.floor(indices[end+1]/width)===Math.floor(indices[begin]/width))end++;
      var leftEdge=(indices[begin]-1)*4,rightEdge=(indices[end]+1)*4;
      for(var j=begin;j<=end;j++){var fraction=(j-begin+1)/(end-begin+2),o=indices[j]*4;
        for(var c=0;c<4;c++)values[o+c]=values[leftEdge+c]*(1-fraction)+values[rightEdge+c]*fraction;
      }
      begin=end+1;
    }
    for(var pass=0;pass<2000;pass++){
      var change=0;
      for(var n=0;n<indices.length;n++){
        var index=indices[n],o=index*4;
        for(var c=0;c<4;c++){
          var mean=(values[o-4+c]+values[o+4+c]+values[o-width*4+c]+values[o+width*4+c])*0.25;
          var next=values[o+c]+1.85*(mean-values[o+c]);
          change=Math.max(change,Math.abs(next-values[o+c]));values[o+c]=next;
        }
      }
      if(change<0.002)break;
    }
    if(pass===2000)throw new Error('living-picture: blink underpaint did not converge; use a tighter source-paint mask');
    indices.forEach(function(index){var o=index*4,alpha=values[o+3]/255;
      for(var c=0;c<3;c++)out[o+c]=alpha>1e-6?values[o+c]/alpha:0;out[o+3]=values[o+3];
    });
  });
  return {pixels:out,coverage:coverage,sourcePaint:sourcePaint,texel:texel,eyes:eyes.length};
};

LP.depthFn = function (d, world) {
  if (d == null) return function () { return 0.5; };
  if (typeof d === 'number') return function () { return d; };
  if (d.ramp) {
    var a = d.ramp[0], b = d.ramp[1], dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy || 1;
    return function (x, y) { var t = LP.clamp(((x - a[0]) * dx + (y - a[1]) * dy) / L2, 0, 1); return a[2] + (b[2] - a[2]) * t; };
  }
  if (d.on) {
    return function (x, y) { var L = world.byId[d.on]; var at = d.at || [x, y]; return L ? L.depthAt(at[0], at[1]) + (d.lift || 0) : 0.5; };
  }
  if (d.points) {                       // a depth sketch: a handful of [x, y, depth] anchors, blended smoothly
    var pts = d.points, pw = (d.power || 2.4) / 2;
    return function (x, y) {
      var sw = 0, sv = 0;
      for (var i = 0; i < pts.length; i++) {
        var q = pts[i], r2 = (x - q[0]) * (x - q[0]) + (y - q[1]) * (y - q[1]);
        if (r2 < 1) return q[2];
        var w = 1 / Math.pow(r2, pw); sw += w; sv += w * q[2];
      }
      return sv / sw;
    };
  }
  return function () { return 0.5; };
};

LP.Layer = function (def, world, img) {
  this.def = def; this.world = world; this.id = def.id; this.img = img;
  var W = world.size[0], H = world.size[1];
  this.rect = def.rect || [0, 0, W, H];
  this.full = this.rect[0] <= 0 && this.rect[1] <= 0 && this.rect[2] >= W && this.rect[3] >= H;
  this.mirror = false;
  this.opacity = def.opacity == null ? 1 : def.opacity;
  this.ownDepthAt = LP.depthFn(def.depth, world);
  this.depthAt = this.ownDepthAt;
  this.xf = [1, 0, 0, 0, 1, 0, 0, 0, 1];
  this.motion = []; this.warps = def.warps || []; this.dynamic = false;
  this.parent = null;
};

LP.Layer.prototype.build = function () {
  var def = this.def, world = this.world, r = this.rect, G = world.G, gl = G.gl;
  // building again (a rewind, a restored graphics context) must start clean: no doubled deformers, no stale state
  if (this.tex) gl.deleteTexture(this.tex);
  if (this.blinkTex) gl.deleteTexture(this.blinkTex);
  if (this.blinkMask) gl.deleteTexture(this.blinkMask);
  if (this.blinkPaint) gl.deleteTexture(this.blinkPaint);
  this.blinkTex = null;
  this.blinkMask = null;
  this.blinkPaint = null;
  var olds = [this.bPos, this.bUv, this.bDepth, this.bIdx, this.bFade];
  for (var ob = 0; ob < olds.length; ob++) if (olds[ob]) gl.deleteBuffer(olds[ob]);
  this.motion = []; this.rigid = null; this.swim = null; this.sprites = null; this.isSprites = false; this.dynamic = false;
  this.flowPhase = 0;
  this.xf = [1, 0, 0, 0, 1, 0, 0, 0, 1]; this.fullXf = null; this.bFade = null;
  if (def.fixed && ((this.img.naturalWidth || this.img.width) !== r[2] || (this.img.naturalHeight || this.img.height) !== r[3] || Math.max(r[2], r[3]) > G.maxTex))
    throw new Error('living-picture: fixed source must retain its verified native size within GPU texture capacity');
  this.tex = LP.texture(G, this.img);
  if (this.warps.some(function(w){return w.type==='blink';})) {
    if (!G.derivatives) throw new Error('living-picture: source-paint blink requires OES_standard_derivatives for pose-aware filtering');
    if (def.flow || def.ripples || def.sprites) throw new Error('living-picture: blink paint must share a normal mesh, without flow, ripples or sprites');
    var bw=this.img.naturalWidth||this.img.width,bh=this.img.naturalHeight||this.img.height;
    if (Math.max(bw,bh)>G.maxTex) throw new Error('living-picture: blink needs native source pixels within GPU texture capacity');
    var bc=document.createElement('canvas');bc.width=bw;bc.height=bh;
    var bx=bc.getContext('2d',{willReadFrequently:true});bx.drawImage(this.img,0,0);
    var bd=bx.getImageData(0,0,bw,bh);
    var paintKey=JSON.stringify([r,this.img.currentSrc||this.img.src||'',this.warps.filter(function(w){return w.type==='blink';}).map(function(w){return [w.at,w.r,w.rot||0,w.mask];})]);
    var cached=this.blinkPrepared,paint;
    if(this.img.tagName==='IMG'&&cached&&cached.source===this.img&&cached.key===paintKey)paint=cached.paint;
    else {paint=LP.blinkPlateData(bd.data,bw,bh,r,this.warps);this.blinkPrepared={source:this.img,key:paintKey,paint:paint};}
    bd.data.set(paint.pixels);bx.putImageData(bd,0,0);
    this.blinkTex=LP.texture(G,bc);this.blinkTexel=paint.texel;this.blinkCanvas=bc;
    var mc=document.createElement('canvas');mc.width=bw;mc.height=bh;var mx=mc.getContext('2d');
    var md=mx.createImageData(bw,bh);md.data.set(paint.coverage);mx.putImageData(md,0,0);this.blinkMask=LP.texture(G,mc);
    md.data.set(paint.sourcePaint);mx.putImageData(md,0,0);this.blinkPaint=LP.texture(G,mc);
  }
  this.parent = def.parent ? world.byId[def.parent] : null;
  this.depthAt = this.ownDepthAt;
  if (this.parent) {
    var parent = this.parent, anchor = def.attach;
    this.depthAt = function (x, y) { return parent.depthAt(anchor ? anchor[0] : x, anchor ? anchor[1] : y); };
    if (def.depth != null) {
      var probes = [[r[0], r[1]], [r[0] + r[2], r[1]], [r[0], r[1] + r[3]], [r[0] + r[2], r[1] + r[3]], [r[0] + r[2] / 2, r[1] + r[3] / 2]];
      for (var pi = 0; pi < probes.length; pi++) if (Math.abs(this.ownDepthAt(probes[pi][0], probes[pi][1]) - this.depthAt(probes[pi][0], probes[pi][1])) > 1e-5)
        throw new Error('living-picture: child ' + this.id + ' depth differs from attachment; omit child depth to inherit');
    }
  }
  if (def.sprites) { this.buildSprites(); return; }
  var cell = def.mesh || 26;
  // wherever the layer touches the picture's border it is continued a little past it with mirrored paint,
  // so a shifted view or a sway never shows an empty strip or a straight cut line at the edge
  var PW = world.size[0], PH = world.size[1], m0 = Math.max(12, PW * (world.camera.travel * 1.6 + 0.012));
  var off = def.fixed || def.mirror === false;
  var mL = !off && r[0] <= 1 ? m0 : 0, mT = !off && r[1] <= 1 ? m0 : 0;
  var mR = !off && r[0] + r[2] >= PW - 1 ? m0 : 0, mB = !off && r[1] + r[3] >= PH - 1 ? m0 : 0;
  this.mirror = (mL + mT + mR + mB) > 0;
  var x0 = r[0] - mL, y0 = r[1] - mT, w = r[2] + mL + mR, h = r[3] + mT + mB;
  var nx = LP.clamp(Math.ceil(w / cell), 1, 72), ny = LP.clamp(Math.ceil(h / cell), 1, 72);
  var n = (nx + 1) * (ny + 1);
  this.n = n; this.rest = new Float32Array(n * 2); this.pos = new Float32Array(n * 2);
  var uv = new Float32Array(n * 2); this.depth = new Float32Array(n);
  var k = 0, i, j;
  for (j = 0; j <= ny; j++) for (i = 0; i <= nx; i++, k++) {
    var x = x0 + w * i / nx, y = y0 + h * j / ny;
    this.rest[k * 2] = x; this.rest[k * 2 + 1] = y;
    uv[k * 2] = (x - r[0]) / r[2]; uv[k * 2 + 1] = (y - r[1]) / r[3];
    this.depth[k] = this.depthAt(LP.clamp(x, 0, world.size[0]), LP.clamp(y, 0, world.size[1]));
    if (this.parent && def.depth != null && Math.abs(this.ownDepthAt(LP.clamp(x, 0, world.size[0]), LP.clamp(y, 0, world.size[1])) - this.depth[k]) > 1e-5)
      throw new Error('living-picture: child ' + this.id + ' depth differs from parent on its mesh');
  }
  if (def.bulge) {
    var bg = LP.alphaGrid(this.img, Math.max(3, Math.max(r[2], r[3]) / 90)), BN = bg.w * bg.h, bd = new Float32Array(BN), bq = [], bi, bmax = 1e-6;
    for (bi = 0; bi < BN; bi++) { if (bg.a[bi] < 0.3) { bd[bi] = 0; bq.push(bi); } else bd[bi] = 1e9; }
    for (var bh = 0; bh < bq.length; bh++) {
      var bc = bq[bh], bx = bc % bg.w, by = (bc / bg.w) | 0, nb = [bx > 0 ? bc - 1 : -1, bx < bg.w - 1 ? bc + 1 : -1, by > 0 ? bc - bg.w : -1, by < bg.h - 1 ? bc + bg.w : -1];
      for (var bn = 0; bn < 4; bn++) { var bj = nb[bn]; if (bj >= 0 && bd[bj] > bd[bc] + 1) { bd[bj] = bd[bc] + 1; bq.push(bj); } }
    }
    for (bi = 0; bi < BN; bi++) if (bd[bi] < 1e8 && bd[bi] > bmax) bmax = bd[bi];
    for (k = 0; k < n; k++) {
      var cx2 = LP.clamp(Math.floor((this.rest[k * 2] - r[0]) / bg.sx), 0, bg.w - 1), cy2 = LP.clamp(Math.floor((this.rest[k * 2 + 1] - r[1]) / bg.sy), 0, bg.h - 1);
      var bt = bd[cy2 * bg.w + cx2]; if (bt > 1e8) bt = bmax;
      this.depth[k] -= def.bulge * Math.sqrt(bt / bmax);
    }
  }
  this.pos.set(this.rest);
  var idx = new Uint16Array(nx * ny * 6); k = 0;
  for (j = 0; j < ny; j++) for (i = 0; i < nx; i++) {
    var a = j * (nx + 1) + i, b = a + 1, c = a + nx + 1, d = c + 1;
    idx[k++] = a; idx[k++] = b; idx[k++] = c; idx[k++] = b; idx[k++] = d; idx[k++] = c;
  }
  this.count = idx.length;
  this.bPos = gl.createBuffer(); this.bUv = gl.createBuffer(); this.bDepth = gl.createBuffer(); this.bIdx = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bUv); gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.bIdx); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
  var list = def.motion || [];
  for (var q = 0; q < list.length; q++) {
    var md = list[q], t = md.type || 'sway';
    if (t === 'sway') this.motion.push(new LP.Sway(this, md));
    else if (t === 'billow') this.motion.push(new LP.Billow(this, md));
    else if (t === 'breathe') this.motion.push(new LP.Breathe(this, md));
    else if (t === 'undulate') this.motion.push(new LP.Undulate(this, md));
    else if (typeof md.apply === 'function') this.motion.push(md);        // custom deformer object
  }
  this.dynamic = this.motion.length > 0;
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bDepth); gl.bufferData(gl.ARRAY_BUFFER, this.depth, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferData(gl.ARRAY_BUFFER, this.pos, this.dynamic ? gl.DYNAMIC_DRAW : gl.STATIC_DRAW);
  if (def.rigid) this.rigid = LP.makeRigid(this, def.rigid);
};

/* how far (0 at the root .. 1 at the far tip) each mesh vertex is from its root,
 * walking through the painted shape rather than straight across gaps */
LP.Layer.prototype.rootField = function (roots, mode, reachOverride) {
  var r = this.rect, n = this.n, label = new Int16Array(n), w = new Float32Array(n), i, k;
  var reach = [];
  if (mode === 'none') { for (i = 0; i < n; i++) w[i] = 1; return { label: label, w: w }; }   // held all along: only trembles
  if (mode === 'axis' || mode === 'radial') {
    for (i = 0; i < n; i++) {
      var x = this.rest[i * 2], y = this.rest[i * 2 + 1], best = 0, bd = 1e18;
      for (k = 0; k < roots.length; k++) { var d0 = Math.hypot(x - roots[k].at[0], y - roots[k].at[1]); if (d0 < bd) { bd = d0; best = k; } }
      label[i] = best;
      var R = roots[best];
      if (mode === 'axis' && R.tip) {
        var ax = R.tip[0] - R.at[0], ay = R.tip[1] - R.at[1];
        w[i] = LP.clamp(((x - R.at[0]) * ax + (y - R.at[1]) * ay) / (ax * ax + ay * ay), 0, 1);
      } else w[i] = bd;
    }
    if (!(mode === 'axis' && roots[0].tip)) {
      for (k = 0; k < roots.length; k++) reach[k] = 1e-6;
      var ag = LP.alphaGrid(this.img, 6);
      for (i = 0; i < n; i++) {
        var gx = Math.floor((this.rest[i * 2] - r[0]) / ag.sx), gy = Math.floor((this.rest[i * 2 + 1] - r[1]) / ag.sy);
        if (gx >= 0 && gy >= 0 && gx < ag.w && gy < ag.h && ag.a[gy * ag.w + gx] > 0.2) reach[label[i]] = Math.max(reach[label[i]], w[i]);
      }
      for (i = 0; i < n; i++) w[i] = LP.clamp(w[i] / (reachOverride || reach[label[i]]), 0, 1);
    }
    return { label: label, w: w };
  }
  // geodesic: walk through solid paint from each root
  var g = LP.alphaGrid(this.img, Math.max(3, Math.max(r[2], r[3]) / 110));
  var N = g.w * g.h, dist = new Float32Array(N), lab = new Int16Array(N), queue = [];
  for (i = 0; i < N; i++) { dist[i] = 1e18; lab[i] = -1; }
  for (k = 0; k < roots.length; k++) {
    var cx = LP.clamp(Math.round((roots[k].at[0] - r[0]) / g.sx - 0.5), 0, g.w - 1), cy = LP.clamp(Math.round((roots[k].at[1] - r[1]) / g.sy - 0.5), 0, g.h - 1);
    var c = cy * g.w + cx; dist[c] = 0; lab[c] = k; queue.push(c);
  }
  var head = 0, OX = [1, -1, 0, 0, 1, 1, -1, -1], OY = [0, 0, 1, -1, 1, -1, 1, -1];
  while (head < queue.length) {
    var cur = queue[head++], qx = cur % g.w, qy = (cur / g.w) | 0;
    for (k = 0; k < 8; k++) {
      var nx = qx + OX[k], ny = qy + OY[k];
      if (nx < 0 || ny < 0 || nx >= g.w || ny >= g.h) continue;
      var ni = ny * g.w + nx;
      var step = (k < 4 ? 1 : 1.414) * (g.a[ni] > 0.15 ? 1 : 5);       // crossing empty space is costly, never forbidden
      var nd = dist[cur] + step;
      if (nd < dist[ni] - 1e-4) { dist[ni] = nd; lab[ni] = lab[cur]; queue.push(ni); }
    }
    if (head > 4096 && head * 2 > queue.length) { queue = queue.slice(head); head = 0; }
  }
  for (k = 0; k < roots.length; k++) reach[k] = 1e-6;
  for (i = 0; i < N; i++) if (g.a[i] > 0.2 && lab[i] >= 0) reach[lab[i]] = Math.max(reach[lab[i]], dist[i]);
  for (i = 0; i < n; i++) {
    var fx = LP.clamp(Math.floor((this.rest[i * 2] - r[0]) / g.sx), 0, g.w - 1), fy = LP.clamp(Math.floor((this.rest[i * 2 + 1] - r[1]) / g.sy), 0, g.h - 1);
    var ci = fy * g.w + fx;
    label[i] = Math.max(0, lab[ci]);
    w[i] = LP.clamp(dist[ci] / (reachOverride ? reachOverride / ((g.sx + g.sy) / 2) : reach[label[i]]), 0, 1);
  }
  return { label: label, w: w };
};

/* ---------- sway: rooted things that bend (plants, branches, cloth, hair, tails) ---------- */
LP.Sway = function (layer, def) {
  var P = LP.PRESETS[def.preset || 'plant'] || LP.PRESETS.plant, world = layer.world, rng = world.rng;
  var o = {}; for (var kk in P) o[kk] = P[kk]; for (kk in def) o[kk] = def[kk];
  this.o = o; this.layer = layer;
  var roots = (o.roots || (o.root ? [o.root] : [])).map(function (r) { return Array.isArray(r) ? { at: r } : r; });
  if (o.tip && roots.length === 1 && !roots[0].tip) roots[0].tip = o.tip;
  if (!roots[0]) roots = [{ at: [layer.rect[0] + layer.rect[2] / 2, layer.rect[1] + layer.rect[3] / 2] }];
  var mode = o.weight || ((roots.length === 1 && roots[0].tip) ? 'axis' : 'geo');
  this.mode = mode;
  var f = layer.rootField(roots, mode, o.reach);
  this.label = f.label; this.w = f.w; this.roots = roots;
  var n = layer.n, i, k;
  // each root: its own spring, its own direction, its own depth if it stands on something
  var cx = new Float32Array(roots.length), cy = new Float32Array(roots.length), cn = new Float32Array(roots.length);
  for (i = 0; i < n; i++) if (this.w[i] > 0.5) { k = this.label[i]; cx[k] += layer.rest[i * 2]; cy[k] += layer.rest[i * 2 + 1]; cn[k]++; }
  for (k = 0; k < roots.length; k++) {
    var R = roots[k], tx, ty;
    if (R.tip) { tx = R.tip[0]; ty = R.tip[1]; } else if (cn[k] > 0) { tx = cx[k] / cn[k]; ty = cy[k] / cn[k]; } else { tx = R.at[0]; ty = R.at[1] - 1; }
    var ax = tx - R.at[0], ay = ty - R.at[1], L = Math.hypot(ax, ay) || 1;
    R.perp = [-ay / L, ax / L]; R.arm = [ax / L, ay / L];
    // a thing painted mid-gust: its painted pose belongs to a wind of strength o.painted, and it relaxes when the air drops
    R.d0 = (o.painted || 0) * (world.wind.dir[0] * R.perp[0] + world.wind.dir[1] * R.perp[1]);
    var jit = 1 + (rng() - 0.5) * 0.3;
    R.sp = new LP.Spring((R.freq || o.freq) * jit, R.damp == null ? o.damp : R.damp);
    R.hist = new Float32Array(96); R.hi = 0; R.seed = rng() * 100; R.phase = rng() * 6.28; R.gust = 0;
    R.gain = (R.bend == null ? o.bend : R.bend) * LP.D2R * (1 + (rng() - 0.5) * 0.25);
    if (R.depth != null || (layer.def.depth && layer.def.depth.perRoot)) {
      var dv = R.depth != null ? R.depth : layer.def.depth;
      R.depthValue = typeof dv === 'number' ? dv : LP.depthFn({ on: dv.on, at: R.at, lift: dv.lift }, world)(R.at[0], R.at[1]);
    }
  }
  if (roots.some(function (R) { return R.depthValue != null; })) {
    for (i = 0; i < n; i++) { var Rr = roots[this.label[i]]; if (Rr.depthValue != null) layer.depth[i] = Rr.depthValue; }
  }
  if (o.oneSided !== false && mode !== 'none') {            // what lies behind the root (a pot, the foot of a trunk) stays put
    for (i = 0; i < n; i++) {
      var Rb = roots[this.label[i]];
      var along = (layer.rest[i * 2] - Rb.at[0]) * Rb.arm[0] + (layer.rest[i * 2 + 1] - Rb.at[1]) * Rb.arm[1];
      this.w[i] *= LP.smooth(-2, o.stem == null ? 10 : o.stem, along);
    }
  }
  if (o.within) {                        // act only inside this outline (the skirt of a figure, one bough of a tree)
    var poly = o.within, fe = o.feather == null ? 14 : o.feather, np = poly.length;
    for (i = 0; i < n; i++) {
      var px = layer.rest[i * 2], py = layer.rest[i * 2 + 1], inside = false, dmin = 1e18;
      for (var a0 = 0, b0 = np - 1; a0 < np; b0 = a0++) {
        var ax2 = poly[a0][0], ay2 = poly[a0][1], bx2 = poly[b0][0], by2 = poly[b0][1];
        if ((ay2 > py) !== (by2 > py) && px < (bx2 - ax2) * (py - ay2) / (by2 - ay2) + ax2) inside = !inside;
        var ex = bx2 - ax2, ey = by2 - ay2, tt2 = LP.clamp(((px - ax2) * ex + (py - ay2) * ey) / (ex * ex + ey * ey || 1), 0, 1);
        var ddx = px - ax2 - ex * tt2, ddy = py - ay2 - ey * tt2, dd = ddx * ddx + ddy * ddy;
        if (dd < dmin) dmin = dd;
      }
      var sd = Math.sqrt(dmin) * (inside ? 1 : -1);
      this.w[i] *= LP.smooth(-fe * 0.5, fe * 0.5, sd);
    }
  }
  this.jit = new Float32Array(n);
  for (i = 0; i < n; i++) this.jit[i] = LP.hash2(layer.rest[i * 2] * 0.37, layer.rest[i * 2 + 1] * 0.41) * 40;
};
LP.Sway.prototype.step = function (t, dt) {
  var o = this.o, world = this.layer.world, k;
  for (k = 0; k < this.roots.length; k++) {
    var R = this.roots[k], target;
    if (o.drive && o.drive !== 'wind') {
      target = world.ch.get(o.drive) * LP.D2R * (o.gain == null ? 1 : o.gain); R.gust = Math.abs(world.ch.get(o.drive)) * 0.1;
    } else {
      var wv = world.wind.at(R.at[0], R.at[1]), ws = Math.hypot(wv[0], wv[1]);
      // pushed across its stem it leans; blown along it, it still bobs in the turbulence
      target = R.gain * ((wv[0] * R.perp[0] + wv[1] * R.perp[1]) - R.d0 + 0.4 * ws * LP.wander(t * 1.1, R.seed));
      R.gust = ws;
    }
    var x = R.sp.step(target, dt);
    R.hi = (R.hi + 1) % 96; R.hist[R.hi] = x;
    R.phase += dt * (o.rippleSpeed || 0) * 6.283 * (0.55 + R.gust);
  }
};
LP.Sway.prototype.apply = function (pos, t) {
  var o = this.o, L = this.layer, rest = L.rest, n = L.n, lagN = (o.lag || 0) * 60, pw = o.power || 1.3;
  var fl = o.flutter || 0, rip = o.ripple || 0, ripN = (o.ripples || 1) * 6.283;
  var fsc = o.flutterScale || 44, tt = t * (o.flutterRate || 2.4);
  var local = this.mode === 'none', wind = L.world.wind, lean = o.lean || null;
  if (lean && !Array.isArray(lean)) lean = [lean, lean];
  var top = L.rect[1], height = L.rect[3] || 1;
  for (var i = 0; i < n; i++) {
    var w = this.w[i];
    if (w <= 0.001) continue;
    var R = this.roots[this.label[i]];
    var back = Math.min(95, lagN * w), i0 = Math.floor(back), fr = back - i0;
    var a = R.hist[(R.hi - i0 + 192) % 96], b = R.hist[(R.hi - i0 - 1 + 192) % 96];
    var th = (a + (b - a) * fr) * Math.pow(w, pw);
    var dx = pos[i * 2] - R.at[0], dy = pos[i * 2 + 1] - R.at[1], c = Math.cos(th), s = Math.sin(th);
    var x = R.at[0] + c * dx - s * dy, y = R.at[1] + s * dx + c * dy;
    var gustHere = R.gust;
    if (local) {
      // held all along (a field, a hedge, ivy): every bit answers the wind where it stands,
      // so a gust is seen crossing it as a moving band
      var wv = wind.at(rest[i * 2], rest[i * 2 + 1]); gustHere = Math.hypot(wv[0], wv[1]);
      if (lean) {
        var yn = LP.clamp((rest[i * 2 + 1] - top) / height, 0, 1), lk = lean[1] + (lean[0] - lean[1]) * yn;
        var pd = o.painted || 0;
        x += (wv[0] - wind.dir[0] * pd) * lk; y += (wv[1] - wind.dir[1] * pd) * lk * 0.5 + (gustHere - pd) * lk * 0.12;
      }
    }
    if (fl) {
      // leaves tremble where they are: a smooth field, so neighbouring paint moves together and nothing crumples
      var gx = rest[i * 2] / fsc, gy = rest[i * 2 + 1] / fsc, k2 = fl * 2 * Math.min(1.3, gustHere) * (0.35 + 0.65 * w);
      x += (LP.noise2(gx + tt * 0.9 + R.seed, gy - tt * 0.4) - 0.5) * k2;
      y += (LP.noise2(gx - tt * 0.5 + 31.7, gy + tt * 0.8 + R.seed) - 0.5) * k2 * 0.7;
    }
    if (rip) {
      var amp = rip * Math.pow(w, 1.3) * (0.22 + R.gust) * (0.75 + 0.5 * LP.noise1(t * 0.7 + R.seed));
      var wave = Math.sin(ripN * w - R.phase) + 0.45 * Math.sin(ripN * 1.9 * w - R.phase * 1.37 + 1.3);
      x += R.perp[0] * amp * wave; y += R.perp[1] * amp * wave;
    }
    pos[i * 2] = x; pos[i * 2 + 1] = y;
  }
};

/* ---------- billow: cloth held at two or more points, filling between them (a canopy, a sail, a banner, washing on a line) ---------- */
LP.Billow = function (layer, def) {
  this.layer = layer; this.d = def;
  var pins = def.pins, n = layer.n, i, k, rng = layer.world.rng, poly = def.within || null;
  this.w = new Float32Array(n); this.s = new Float32Array(n);
  var a = pins[0], b = pins[pins.length - 1], ax = b[0] - a[0], ay = b[1] - a[1], L2 = ax * ax + ay * ay || 1, wmax = 1e-6;
  for (i = 0; i < n; i++) {
    var x = layer.rest[i * 2], y = layer.rest[i * 2 + 1], prod = 1;
    for (k = 0; k < pins.length; k++) prod *= Math.min(1, Math.hypot(x - pins[k][0], y - pins[k][1]) / (def.reach || Math.sqrt(L2) * 0.5));
    var inside = 1;
    if (poly) {
      var ins = false, dmin = 1e18;
      for (var p0 = 0, q0 = poly.length - 1; p0 < poly.length; q0 = p0++) {
        var px = poly[p0][0], py = poly[p0][1], qx = poly[q0][0], qy = poly[q0][1];
        if ((py > y) !== (qy > y) && x < (qx - px) * (y - py) / (qy - py) + px) ins = !ins;
        var ex = qx - px, ey = qy - py, tt = LP.clamp(((x - px) * ex + (y - py) * ey) / (ex * ex + ey * ey || 1), 0, 1);
        var dd = Math.hypot(x - px - ex * tt, y - py - ey * tt); if (dd < dmin) dmin = dd;
      }
      var fe = def.feather == null ? 14 : def.feather;
      inside = LP.smooth(-fe * 0.5, fe * 0.5, ins ? dmin : -dmin);
    }
    this.w[i] = prod * inside; if (this.w[i] > wmax) wmax = this.w[i];
    this.s[i] = LP.clamp(((x - a[0]) * ax + (y - a[1]) * ay) / L2, -0.3, 1.3);      // place along the span, windward pin to leeward
  }
  for (i = 0; i < n; i++) this.w[i] /= wmax;
  this.sp = new LP.Spring(def.freq || 0.8, def.damp == null ? 0.3 : def.damp);
  this.phase = rng() * 6.28; this.seed = rng() * 100; this.gust = 0; this.x = 0;
  this.at = def.at || [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
};
LP.Billow.prototype.step = function (t, dt) {
  var d = this.d, world = this.layer.world, wv = world.wind.at(this.at[0], this.at[1]), ws = Math.hypot(wv[0], wv[1]);
  this.gust = ws; this.wv = wv;
  this.x = this.sp.step(ws - (d.painted || 0), dt);
  this.phase += dt * 6.283 * (d.waveSpeed || 1.1) * (0.5 + ws);
};
LP.Billow.prototype.apply = function (pos, t) {
  var d = this.d, n = this.layer.n, wd = this.layer.world.wind.dir, out = d.out || [wd[0], wd[1] - 0.35];
  var on = Math.hypot(out[0], out[1]) || 1, ox = out[0] / on, oy = out[1] / on;
  var fill = (d.amount == null ? 14 : d.amount) * this.x, wave = (d.wave == null ? 4 : d.wave) * (0.2 + this.gust), waves = (d.waves || 1.3) * 6.283;
  for (var i = 0; i < n; i++) {
    var w = this.w[i]; if (w < 0.004) continue;
    var k = fill * w + wave * w * (Math.sin(waves * this.s[i] - this.phase) + 0.4 * Math.sin(waves * 2.1 * this.s[i] - this.phase * 1.4 + 1.1));
    pos[i * 2] += ox * k; pos[i * 2 + 1] += oy * k;
  }
};

/* ---------- breathe: a chest that fills and empties ---------- */
LP.Breathe = function (layer, def) { this.layer = layer; this.d = def; };
LP.Breathe.prototype.step = function () {};
LP.Breathe.prototype.apply = function (pos) {
  var d = this.d, L = this.layer, b = L.world.ch.get(d.ch || 'breath'), n = L.n;
  var cx = d.at[0], cy = d.at[1], rx = d.radius[0], ry = d.radius[1], am = (d.amount == null ? 0.02 : d.amount) * b, lift = (d.lift || 0) * b;
  for (var i = 0; i < n; i++) {
    var dx = (L.rest[i * 2] - cx) / rx, dy = (L.rest[i * 2 + 1] - cy) / ry, f = Math.exp(-(dx * dx + dy * dy) * 1.6);
    if (f < 0.01) continue;
    pos[i * 2] += (pos[i * 2] - cx) * am * f;
    pos[i * 2 + 1] += (pos[i * 2 + 1] - cy) * am * f * 0.6 - lift * f;
  }
};

/* ---------- undulate: a body that swims by sending a wave down itself ---------- */
LP.Undulate = function (layer, def) {
  this.layer = layer; this.d = def;
  var hx = def.head[0], hy = def.head[1], tx = def.tail[0], ty = def.tail[1];
  var ax = tx - hx, ay = ty - hy, L2 = ax * ax + ay * ay || 1, L = Math.sqrt(L2), n = layer.n;
  this.u = new Float32Array(n); this.nrm = [-ay / L, ax / L]; this.phase = layer.world.rng() * 6.28; this.len = L;
  this.effort = 0; this.bendNow = 0; this.phase0 = this.phase;
  for (var i = 0; i < n; i++) this.u[i] = LP.clamp(((layer.rest[i * 2] - hx) * ax + (layer.rest[i * 2 + 1] - hy) * ay) / L2, -0.2, 1.3);
};
LP.Undulate.prototype.step = function (t, dt) {
  var L = this.layer, e = L.swim ? L.swim.effort : (this.d.ch ? L.world.ch.get(this.d.ch) : 0.3);
  this.effort = e; this.phase += dt * 6.283 * (this.d.beat || 1.6) * (0.35 + 0.9 * e);
  this.bendNow = L.swim ? L.swim.bend : 0;
};
LP.Undulate.prototype.apply = function (pos) {
  var d = this.d, n = this.layer.n, wake = LP.smooth(0, 0.8, this.layer.world.time);
  var amp = (d.amp == null ? this.len * 0.09 : d.amp) * (0.25 + 0.75 * this.effort) * wake;
  var waves = (d.waves || 0.9) * 6.283, stiff = d.stiff == null ? 0.25 : d.stiff, bend = this.bendNow * this.len;
  for (var i = 0; i < n; i++) {
    var u = this.u[i], v = Math.max(0, (u - stiff) / (1 - stiff));
    var off = amp * v * v * Math.sin(waves * u - this.phase) + bend * v * v;
    pos[i * 2] += this.nrm[0] * off; pos[i * 2 + 1] += this.nrm[1] * off;
  }
};

/* ---------- rigid drivers: the whole layer swings, floats or swims ---------- */
LP.makeRigid = function (layer, def) {
  var world = layer.world, rng = world.rng, t = def.type;
  if (t === 'swing') {                 // a pendulum: its period is its own, only its reach varies
    var st = { phase: (def.phase || 0) * 6.283, amp: 0, sp: new LP.Spring(1 / (def.period || 2), def.damp == null ? 0.06 : def.damp), seed: rng() * 100 };
    return function (tm, dt) {
      var ang;
      if (def.drive === 'wind') {
        var wv = world.wind.at(def.pivot[0], def.pivot[1] + (def.length || 40));
        ang = st.sp.step(-wv[0] * (def.amp || 6) * LP.D2R, dt) + LP.D2R * (def.idle || 0.4) * LP.wander(tm * 0.5, st.seed);
      } else {
        st.phase += dt * 6.283 / (def.period || 3);
        var a = (def.amp || 3) * (0.72 + 0.28 * LP.noise1(tm * 0.11 + st.seed)) * LP.smooth(0, def.rampUp == null ? 2.5 : def.rampUp, tm);
        ang = a * LP.D2R * Math.sin(st.phase);
      }
      layer.angle = ang;
      LP.setXf(layer, def.pivot[0], def.pivot[1], ang, 0, 0, 1);
    };
  }
  if (t === 'bob') {                   // something floating: drifts and tips a little, never the same twice
    var s2 = rng() * 100;
    return function (tm) {
      var r = def.rate || 0.25, k = LP.smooth(0, 1.5, tm);
      var dx = LP.wander(tm * r, s2) * (def.x || 2) * k, dy = LP.wander(tm * r * 1.3, s2 + 7) * (def.y || 2) * k;
      var ang = LP.wander(tm * r * 0.9, s2 + 13) * (def.rot || 2) * LP.D2R * k;
      var c = def.at || [layer.rect[0] + layer.rect[2] / 2, layer.rect[1] + layer.rect[3] / 2];
      LP.setXf(layer, c[0], c[1], ang, dx, dy, 1);
    };
  }
  if (t === 'pose') {                  // driven by channels: x, y, angle (deg), scale
    return function () {
      var ch = world.ch, c = def.at || [layer.rect[0] + layer.rect[2] / 2, layer.rect[1] + layer.rect[3] / 2];
      LP.setXf(layer, c[0], c[1], ch.get(def.angle) * LP.D2R, ch.get(def.x), ch.get(def.y), def.scale == null ? 1 : 1 + ch.get(def.scale));
    };
  }
  if (t === 'swim') return LP.makeSwim(layer, def);
  if (typeof def === 'function') return def;
  return null;
};
LP.setXf = function (layer, px, py, ang, dx, dy, sc) {
  var c = Math.cos(ang) * sc, s = Math.sin(ang) * sc, m = layer.xf;
  m[0] = c; m[1] = s; m[2] = 0; m[3] = -s; m[4] = c; m[5] = 0;
  m[6] = px - c * px + s * py + dx; m[7] = py - s * px - c * py + dy; m[8] = 1;
};

/* a fish: hovers, then a few tail-beats and a glide, turns before the bank, hovers again */
LP.makeSwim = function (layer, def) {
  var world = layer.world, rng = world.rng, W = world.size[0];
  var hx = def.head[0], hy = def.head[1], tx = def.tail[0], ty = def.tail[1];
  var cx = (hx + tx) / 2, cy = (hy + ty) / 2, h0 = Math.atan2(hy - ty, hx - tx), len = Math.hypot(hx - tx, hy - ty);
  var box = def.region || [0, 0, world.size[0], world.size[1]];
  var S = { x: cx, y: cy, h: h0, v: 0, mode: 'hover', until: rng.pick(def.first || [0.8, 4]), effort: 0.12, bend: 0, want: h0, turn: 0, seed: rng() * 100, burst: 0 };
  layer.swim = S;
  var cruise = (def.speed == null ? 0.55 : def.speed) * len;      // body lengths per second
  return function (t, dt) {
    if (t >= S.until) {
      if (S.mode === 'hover') { S.mode = 'cruise'; S.until = t + rng.pick(def.cruise || [5, 12]); S.burst = t; }
      else { S.mode = 'hover'; S.until = t + rng.pick(def.hover || [2.5, 7]); }
    }
    var targetV = 0, eff = 0.1 + 0.06 * LP.noise1(t * 0.6 + S.seed);
    if (S.mode === 'cruise') {
      // burst and coast: beat for a moment, then glide on what that gave
      var cyc = (t - S.burst), on = def.beatTime || 0.9, off = def.glideTime || 1.5;
      if (cyc > on + off) { S.burst = t + rng() * 0.4; cyc = 0; }
      if (cyc >= 0 && cyc < on) { targetV = cruise * 1.25; eff = 1; } else { targetV = cruise * 0.55; eff = 0.2; }
    }
    S.effort += (eff - S.effort) * Math.min(1, dt * 5);
    S.v += (targetV - S.v) * Math.min(1, dt * (targetV > S.v ? 2.2 : 0.7));
    // where to head: wander, but turn back well before the edge of the water
    var m = len * 1.1, want = S.want + LP.wander(t * 0.13, S.seed) * 0.012;
    var ahead = len * 1.8, fx = S.x + Math.cos(S.h) * ahead, fy = S.y + Math.sin(S.h) * ahead;
    if (fx < box[0] + m || fx > box[2] - m || fy < box[1] + m || fy > box[3] - m) {
      want = Math.atan2((box[1] + box[3]) / 2 - S.y + LP.wander(t * 0.05, S.seed + 3) * (box[3] - box[1]) * 0.3,
                        (box[0] + box[2]) / 2 - S.x + LP.wander(t * 0.05, S.seed + 9) * (box[2] - box[0]) * 0.3);
    }
    S.want = want;
    var dh = Math.atan2(Math.sin(want - S.h), Math.cos(want - S.h));
    var maxTurn = (def.turn || 40) * LP.D2R * (0.25 + S.v / (cruise + 1e-6));
    var rate = LP.clamp(dh * 1.2, -maxTurn, maxTurn);
    S.turn += (rate - S.turn) * Math.min(1, dt * 2.5);
    S.h += S.turn * dt;
    S.bend += (-S.turn * 0.22 - S.bend) * Math.min(1, dt * 4);     // the body curves into the turn
    S.x += Math.cos(S.h) * S.v * dt; S.y += Math.sin(S.h) * S.v * dt;
    S.x = LP.clamp(S.x, box[0] - len, box[2] + len); S.y = LP.clamp(S.y, box[1] - len, box[3] + len);
    LP.setXf(layer, cx, cy, S.h - h0, S.x - cx, S.y - cy, 1);
    if (layer.def.wake && S.mode === 'cruise' && (t - S.burst) < dt * 1.5 && world.ripple) {
      world.ripple(S.x + Math.cos(S.h) * len * 0.3, S.y + Math.sin(S.h) * len * 0.3, 0.6);
    }
  };
};

/* ---------- sprites: loose marks (petals, leaves, motes, butterflies) set free ---------- */
/* Rectangle bounds are conservative for irregular paint: the whole rotated
 * textured quad, including transparent padding, must fit the authored region.
 * The region is in picture coordinates before camera projection. */
LP.spriteFootprint = function (s, matrix) {
  var c = Math.cos(s.a) * s.sc, sn = Math.sin(s.a) * s.sc;
  var hw = s.w / 2 * (s.flap == null ? 1 : s.flap), hh = s.h / 2;
  var p = [s.x-c*hw+sn*hh, s.y-sn*hw-c*hh, s.x+c*hw+sn*hh, s.y+sn*hw-c*hh,
           s.x-c*hw-sn*hh, s.y-sn*hw+c*hh, s.x+c*hw-sn*hh, s.y+sn*hw+c*hh];
  if (matrix) for (var i = 0; i < 8; i += 2) { var x = p[i], y = p[i+1]; p[i] = matrix[0]*x+matrix[3]*y+matrix[6]; p[i+1] = matrix[1]*x+matrix[4]*y+matrix[7]; }
  return p;
};
LP.spriteRegionAlpha = function (corners, region, feather) {
  if (!region) return 1;
  var clearance = Infinity;
  for (var i = 0; i < corners.length; i += 2)
    clearance = Math.min(clearance, corners[i]-region[0], region[2]-corners[i], corners[i+1]-region[1], region[3]-corners[i+1]);
  if (!Number.isFinite(clearance) || clearance < 0) return 0;
  return feather > 0 ? LP.smooth(0, feather, clearance) : 1;
};


LP.Layer.prototype.buildSprites = function () {
  var def = this.def, sd = def.sprites === true ? {} : def.sprites, world = this.world, rng = world.rng, r = this.rect, gl = world.G.gl;
  var g = LP.alphaGrid(this.img, sd.cell || 2), N = g.w * g.h, lab = new Int32Array(N), boxes = [], i;
  for (i = 0; i < N; i++) {
    if (g.a[i] < 0.2 || lab[i]) continue;
    var id = boxes.length + 1, st = [i], b = [1e9, 1e9, -1, -1, 0]; lab[i] = id;
    while (st.length) {
      var c = st.pop(), x = c % g.w, y = (c / g.w) | 0;
      if (x < b[0]) b[0] = x; if (y < b[1]) b[1] = y; if (x > b[2]) b[2] = x; if (y > b[3]) b[3] = y; b[4]++;
      for (var oy = -2; oy <= 2; oy++) for (var ox = -2; ox <= 2; ox++) {
        var nx = x + ox, ny = y + oy;
        if (nx < 0 || ny < 0 || nx >= g.w || ny >= g.h) continue;
        var ni = ny * g.w + nx;
        if (g.a[ni] >= 0.2 && !lab[ni]) { lab[ni] = id; st.push(ni); }
      }
    }
    b[5] = b[4] * g.sx * g.sy < (sd.min || 10) ? 1 : 0;       // too small to be a piece: it stays painted where it is
    boxes.push(b);
  }
  var copies = sd.copies || 1, S = [];
  for (var k = 0; k < copies; k++) for (i = 0; i < boxes.length; i++) {
    if (k > 0 && boxes[i][5]) continue;
    var bb = boxes[i], x0 = bb[0] * g.sx - 1.5, y0 = bb[1] * g.sy - 1.5, w = (bb[2] - bb[0] + 1) * g.sx + 3, h = (bb[3] - bb[1] + 1) * g.sy + 3;
    S.push({ u0: x0 / r[2], v0: y0 / r[3], u1: (x0 + w) / r[2], v1: (y0 + h) / r[3], w: w, h: h,
             hx: r[0] + x0 + w / 2, hy: r[1] + y0 + h / 2, x: 0, y: 0, a: 0, sc: 1, al: k > 0 ? 0 : 1, clone: k > 0,
             seed: rng() * 100, mode: k > 0 ? 'wait' : 'rest', until: 0, vx: 0, vy: 0, depth: 0, still: !!bb[5] });
  }
  this.sprites = S; this.sd = sd; this.n = S.length * 4; this.count = S.length * 6;
  this.pos = new Float32Array(this.n * 2); this.depth = new Float32Array(this.n); this.fade = new Float32Array(this.n);
  var uv = new Float32Array(this.n * 2), idx = new Uint16Array(this.count), base = this.depthAt(r[0] + r[2] / 2, r[1] + r[3] / 2);
  for (i = 0; i < S.length; i++) {
    var s = S[i], o = i * 8;
    uv[o] = s.u0; uv[o + 1] = s.v0; uv[o + 2] = s.u1; uv[o + 3] = s.v0; uv[o + 4] = s.u0; uv[o + 5] = s.v1; uv[o + 6] = s.u1; uv[o + 7] = s.v1;
    idx.set([i * 4, i * 4 + 1, i * 4 + 2, i * 4 + 1, i * 4 + 3, i * 4 + 2], i * 6);
    s.depth = sd.depth ? rng.pick(sd.depth) : this.depthAt(s.hx, s.hy);
    if (s.clone && sd.depth) s.sc = LP.lerp(1.25, 0.7, (s.depth - sd.depth[0]) / ((sd.depth[1] - sd.depth[0]) || 1));
    for (var q = 0; q < 4; q++) this.depth[i * 4 + q] = s.depth;
    s.x = s.hx; s.y = s.hy;
    if (sd.region && !s.clone && LP.spriteRegionAlpha(LP.spriteFootprint(s), sd.region, 0) === 0)
      throw new Error('living-picture: original sprite in ' + this.id + ' does not fit its region; expand the region or select a different source mark');
    s.until = s.clone ? rng.range(0.5, sd.spawn || 14) : rng.pick(sd.release || [1, 9]);
  }
  this.bPos = gl.createBuffer(); this.bUv = gl.createBuffer(); this.bDepth = gl.createBuffer(); this.bIdx = gl.createBuffer(); this.bFade = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bUv); gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bDepth); gl.bufferData(gl.ARRAY_BUFFER, this.depth, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.bIdx); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferData(gl.ARRAY_BUFFER, this.pos, gl.DYNAMIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bFade); gl.bufferData(gl.ARRAY_BUFFER, this.fade, gl.DYNAMIC_DRAW);
  this.dynamic = true; this.isSprites = true;
  this.kind = sd.kind || 'fall';
};
LP.Layer.prototype.stepSprites = function (t, dt) {
  var sd = this.sd, world = this.world, rng = world.rng, W = world.size[0], H = world.size[1], S = this.sprites;
  var box = sd.region || [-W * 0.08, -H * 0.08, W * 1.08, H * 1.08];
  var fall = (sd.fall == null ? 0.045 : sd.fall) * H, carry = (sd.carry == null ? 0.3 : sd.carry) * W, fade = sd.fade || 0.7;
  for (var i = 0; i < S.length; i++) {
    var s = S[i];
    if (s.still) { s.al = 1; s.x = s.hx; s.y = s.hy; continue; }
    if (this.kind === 'flit') {          // a butterfly: flutters about a home, sometimes settles
      if (t >= s.until) { s.mode = s.mode === 'fly' ? 'rest' : 'fly'; s.until = t + (s.mode === 'fly' ? rng.pick(sd.flight || [3, 8]) : rng.pick(sd.pause || [1.5, 5])); }
      var roam = (sd.roam || 0.07) * W, k = s.mode === 'fly' ? 1 : 0;
      s.go = (s.go || 0) + (k - (s.go || 0)) * Math.min(1, dt * 1.5);
      var tx = s.hx + LP.wander(t * 0.22, s.seed) * roam * s.go, ty = s.hy + LP.wander(t * 0.27, s.seed + 5) * roam * 0.7 * s.go - s.go * roam * 0.15;
      s.vx = (tx - s.x) * 2.2; s.vy = (ty - s.y) * 2.2;
      s.x += s.vx * dt; s.y += s.vy * dt + Math.sin(t * 9 + s.seed) * 0.25 * s.go;
      s.a = LP.clamp(s.vx * 0.01, -0.5, 0.5);
      var beat = sd.beat || 9;
      s.flap = 1 - (0.25 + 0.65 * s.go) * (0.5 + 0.5 * Math.sin(t * beat * 6.283 + s.seed));   // wings close and open
      s.al = 1;
      continue;
    }
    if (s.lying == null) s.lying = sd.ground != null && s.hy >= sd.ground;
    if (s.mode === 'rest' && s.lying && !s.clone) s.mode = 'lie';
    if (s.mode === 'lie') {              // on the ground: a gust slides it along, then it settles again
      var wg = world.wind.at(s.x, s.y), sg = Math.hypot(wg[0], wg[1]);
      var push = Math.max(0, sg - (sd.threshold == null ? 0.35 : sd.threshold) * (0.7 + 0.6 * (s.seed % 1)));
      s.vx += (wg[0] / (sg + 1e-6) * push * carry * 0.9 - s.vx) * Math.min(1, dt * 3);
      s.x += s.vx * dt; s.y += Math.sin(t * 7 + s.seed) * push * 0.6;
      s.a += s.vx * dt * 0.03;
      s.al = Math.min(1, s.al + dt * 2);
      if (s.flap != null) s.flap += (1 - s.flap) * Math.min(1, dt * 3);     // a landed flake settles flat
      if (s.x < box[0] - 20 || s.x > box[2] + 20) { s.mode = 'wait'; s.until = t + rng.range(0.5, sd.respawn || 6); s.al = 0; s.lying = false; }
      continue;
    }
    if (s.mode === 'rest') {             // still where it was painted, until its moment comes
      s.al = 1; s.x = s.hx; s.y = s.hy; s.a = 0; s.flap = null;
      var wv0 = world.wind.at(s.hx, s.hy);
      if (t >= s.until || (sd.gustRelease !== false && Math.hypot(wv0[0], wv0[1]) > (sd.threshold || 0.7) && LP.hash(s.seed + Math.floor(t)) < dt * 2.5)) { s.mode = 'fly'; s.t0 = t; s.vx = 0; s.vy = 0; }
    } else if (s.mode === 'wait') {
      s.al = 0;
      if (t >= s.until) {                // enter from upwind or from above
        var wd = world.wind.dir, fromTop = rng() < (sd.fromTop == null ? 0.6 : sd.fromTop);
        if (fromTop) { s.x = rng.range(box[0], box[2]); s.y = box[1] - 10; }
        else { s.x = wd[0] >= 0 ? box[0] - 10 : box[2] + 10; s.y = rng.range(box[1], box[1] + (box[3] - box[1]) * 0.75); }
        s.mode = 'fly'; s.t0 = t; s.vx = 0; s.vy = 0;
      }
    } else {                             // adrift: falling, carried by the wind, rocking as it goes
      var wv = world.wind.at(s.x, s.y), age = t - s.t0;
      var rock = Math.sin(age * (sd.rock || 2.4) + s.seed);
      var ax = wv[0] * carry + rock * fall * 0.7, ay = fall * (0.75 + 0.35 * Math.cos(age * (sd.rock || 2.4) * 2 + s.seed)) + wv[1] * carry * 0.6;
      s.vx += (ax - s.vx) * Math.min(1, dt * 1.6); s.vy += (ay - s.vy) * Math.min(1, dt * 1.6);
      s.x += s.vx * dt; s.y += s.vy * dt;
      s.a += dt * ((sd.spin == null ? 1.2 : sd.spin) * rock + wv[0] * (sd.windSpin == null ? 2.5 : sd.windSpin));
      if (sd.tumble) {                   // a thin flake turning over: its seen width narrows and widens, faster when it moves faster
        var rel = Math.hypot(s.vx, s.vy) / (fall + 1e-6);
        s.tp = (s.tp == null ? s.seed * 6.283 : s.tp) + dt * 6.283 * (sd.flip == null ? 0.7 : sd.flip) * (0.6 + 0.6 * Math.min(2, rel));
        s.flap = 1 - LP.clamp(sd.tumble, 0, 0.9) * (0.5 - 0.5 * Math.cos(s.tp));
      }
      var out = s.x < box[0] - 20 || s.x > box[2] + 20 || s.y > box[3] + 20 || s.y < box[1] - 40;
      s.al = Math.min(1, age / fade);
      if (sd.ground != null) {           // it lands somewhere on the ground and lies there
        if (s.land == null) s.land = sd.ground + (H - sd.ground) * (0.08 + 0.84 * ((s.seed * 7.31) % 1));
        if (s.y >= s.land) { s.mode = 'lie'; s.lying = true; s.vx *= 0.3; s.land = null; }
      }
      if (out) { s.mode = 'wait'; s.until = t + rng.range(0.3, sd.respawn || 6); s.al = 0; }
    }
    if (s.mode === 'fade') { var e = (t - s.t0) / (sd.linger || 2.5); s.al = s.al0 * (1 - LP.ease(e)); if (e >= 1) { s.mode = 'wait'; s.until = t + rng.range(0.3, sd.respawn || 6); } }
  }
};
LP.Layer.prototype.writeSprites = function () {
  var S = this.sprites, p = this.pos, f = this.fade;
  for (var i = 0; i < S.length; i++) {
    var s = S[i], c = Math.cos(s.a) * s.sc, sn = Math.sin(s.a) * s.sc, hw = s.w / 2 * (s.flap == null ? 1 : s.flap), hh = s.h / 2, o = i * 8;
    f[i * 4] = f[i * 4 + 1] = f[i * 4 + 2] = f[i * 4 + 3] = s.al;
    p[o] = s.x - c * hw + sn * hh; p[o + 1] = s.y - sn * hw - c * hh;
    p[o + 2] = s.x + c * hw + sn * hh; p[o + 3] = s.y + sn * hw - c * hh;
    p[o + 4] = s.x - c * hw - sn * hh; p[o + 5] = s.y - sn * hw + c * hh;
    p[o + 6] = s.x + c * hw - sn * hh; p[o + 7] = s.y + sn * hw + c * hh;
  }
};

LP.Layer.prototype.clipSprites = function () {
  if (!this.isSprites || !this.sd.region) return;
  var feather = this.sd.regionFade == null ? 2 : Math.max(0, this.sd.regionFade);
  for (var i = 0; i < this.sprites.length; i++) {
    var s = this.sprites[i], alpha = s.al * LP.spriteRegionAlpha(LP.spriteFootprint(s, this.fullXf || this.xf), this.sd.region, feather);
    this.fade[i*4] = this.fade[i*4+1] = this.fade[i*4+2] = this.fade[i*4+3] = alpha;
  }
  var gl = this.world.G.gl; gl.bindBuffer(gl.ARRAY_BUFFER, this.bFade); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.fade);
};

/* ---------- per-frame ---------- */
LP.Layer.prototype.step = function (t, dt) {
  if (this.isSprites) { this.stepSprites(t, dt); return; }
  for (var i = 0; i < this.motion.length; i++) if (this.motion[i].step) this.motion[i].step(t, dt);
  if (this.rigid) this.rigid(t, dt);
};
LP.Layer.prototype.update = function (t) {
  var gl = this.world.G.gl;
  if (this.isSprites) this.writeSprites();
  else if (this.dynamic || this.motion.length) {      // deformers added later by a hook count too
    this.pos.set(this.rest);
    for (var i = 0; i < this.motion.length; i++) this.motion[i].apply(this.pos, t);
    // the film begins on the painted frame: whatever the deformers want, they ease into it
    var wk = LP.smooth(0, 1.3, t);
    if (wk < 1) { var p = this.pos, r0 = this.rest; for (var j = 0; j < p.length; j++) p[j] = r0[j] + (p[j] - r0[j]) * wk; }
  } else return;
  gl.bindBuffer(gl.ARRAY_BUFFER, this.bPos); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.pos);
  if (this.isSprites) { gl.bindBuffer(gl.ARRAY_BUFFER, this.bFade); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.fade); }
};
/* where a rest-space point of this layer currently is (so children can ride on it) */
LP.Layer.prototype.carry = function (x, y) {
  var best = 0, bd = 1e18, n = this.n, rest = this.rest;
  if ((this.dynamic || this.motion.length) && !this.isSprites) {
    for (var i = 0; i < n; i++) { var d = (rest[i * 2] - x) * (rest[i * 2] - x) + (rest[i * 2 + 1] - y) * (rest[i * 2 + 1] - y); if (d < bd) { bd = d; best = i; } }
    x += this.pos[best * 2] - rest[best * 2]; y += this.pos[best * 2 + 1] - rest[best * 2 + 1];
  }
  var m = this.fullXf || this.xf;
  return [m[0] * x + m[3] * y + m[6], m[1] * x + m[4] * y + m[7]];
};
