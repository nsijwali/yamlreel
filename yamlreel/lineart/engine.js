// Vendored from github.com/JDhruv14/line-art-animation (MIT): lib/ink.ts and lib/gait.ts, compiled to plain JS.
// See yamlreel/lineart/LICENSE-line-art-animation. The engine below it is yamlreel code.
(() => {
  // lib/ink.ts
  var NS = "http://www.w3.org/2000/svg";
  var INK = "currentColor";
  var PAPER = "#fdfdfc";
  var INK_DAY = "#1c1c1c";
  var PEN = 1.35;
  function el(tag, attrs = {}, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, String(attrs[k]));
    if (attrs.fill === PAPER) n.classList.add("pp");
    if (parent) parent.appendChild(n);
    return n;
  }
  var f = (n) => n.toFixed(2);
  var pt = (p) => `${f(p[0])} ${f(p[1])}`;
  var pen = (parent, w = PEN) => el("path", { fill: "none", stroke: INK, "stroke-width": w, "stroke-linecap": "round", "stroke-linejoin": "round" }, parent);
  var blot = (parent) => el("path", { fill: INK }, parent);
  var paint = (parent, colour, w = PEN) => el("path", { fill: colour, stroke: INK, "stroke-width": w, "stroke-linejoin": "round", "stroke-linecap": "round" }, parent);
  function limb(parent, width, w = PEN) {
    const g = el("g", {}, parent);
    const fill = el("path", { fill: PAPER }, g);
    const a = pen(g, w), b = pen(g, w);
    const half = (width - w) / 2;
    return {
      g,
      draw(joints, endGap = [0.4, 0.9]) {
        const s = joints[0], e = joints[joints.length - 1];
        let c = [(s[0] + e[0]) / 2, (s[1] + e[1]) / 2];
        if (joints.length === 3) {
          const m = joints[1];
          c = [m[0] + 0.55 * (m[0] - c[0]), m[1] + 0.55 * (m[1] - c[1])];
        }
        const P = [];
        for (let i = 0; i <= 16; i++) {
          const t = i / 16, u = 1 - t;
          P.push([u * u * s[0] + 2 * u * t * c[0] + t * t * e[0], u * u * s[1] + 2 * u * t * c[1] + t * t * e[1]]);
        }
        const side = (sgn) => P.map((p, i) => {
          const q = P[Math.min(P.length - 1, i + 1)], o = P[Math.max(0, i - 1)];
          const dx = q[0] - o[0], dy = q[1] - o[1], l = Math.hypot(dx, dy) || 1;
          return [p[0] - dy / l * half * sgn, p[1] + dx / l * half * sgn];
        });
        const L = side(1), R = side(-1);
        fill.setAttribute("d", "M" + L.map(pt).join("L") + "L" + R.slice().reverse().map(pt).join("L") + "Z");
        const cut = (arr, gap) => arr.slice(0, arr.length - Math.round(gap * 2));
        a.setAttribute("d", "M" + cut(L, endGap[0]).map(pt).join("L"));
        b.setAttribute("d", "M" + cut(R, endGap[1]).map(pt).join("L"));
      }
    };
  }
  function shoe(node, ankle, toe = 0) {
    const r = toe * Math.PI / 180, c = Math.cos(r), s = Math.sin(r);
    const S2 = (x, y) => [ankle[0] + x * c - y * s, ankle[1] + x * s + y * c];
    node.setAttribute("d", `M${pt(S2(-1.6, -0.6))} L${pt(S2(4.6, -0.2))} Q${pt(S2(6.6, 0.4))} ${pt(S2(5.6, 1.6))} L${pt(S2(-1.4, 1.5))} Q${pt(S2(-2.2, 0.5))} ${pt(S2(-1.6, -0.6))}Z`);
  }
  var place = (g, x, y, dir = 1, s = 0.5) => g.setAttribute("transform", `translate(${f(x)} ${f(y)}) scale(${f(dir * s)} ${f(s)}) translate(-40 -120)`);
  function inkDefs(svg2, id = "ink") {
    if (!svg2.style.color) svg2.style.color = INK_DAY;
    const defs = el("defs", {}, svg2);
    const flt = el("filter", { id: `${id}-edge`, x: "-2%", y: "-2%", width: "104%", height: "104%" }, defs);
    el("feTurbulence", { type: "fractalNoise", baseFrequency: "0.6", numOctaves: 1, seed: 7, result: "n" }, flt);
    el("feDisplacementMap", { in: "SourceGraphic", in2: "n", scale: 0.45, xChannelSelector: "R", yChannelSelector: "G", result: "d" }, flt);
    el("feGaussianBlur", { in: "d", stdDeviation: 0.12 }, flt);
    const stripes = el("pattern", { id: `${id}-stripes`, width: 4, height: 2.2, patternUnits: "userSpaceOnUse", patternTransform: "rotate(-12)" }, defs);
    el("rect", { width: 4, height: 2.2, fill: PAPER }, stripes);
    el("rect", { width: 4, height: 1, fill: INK }, stripes);
    return { defs, filter: `url(#${id}-edge)`, stripes: `url(#${id}-stripes)` };
  }

  // lib/gait.ts
  var clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  var smooth = (e0, e1, x) => {
    const t = clamp((x - e0) / (e1 - e0), 0, 1);
    return t * t * (3 - 2 * t);
  };
  var minJerk = (s) => s * s * s * (10 - 15 * s + 6 * s * s);
  function knee(hip, ankle, l1, l2) {
    const dx = ankle[0] - hip[0], dy = ankle[1] - hip[1];
    const d = Math.min(Math.hypot(dx, dy), l1 + l2 - 0.01);
    const th = Math.atan2(dy, dx);
    const al = Math.acos(clamp((l1 * l1 + d * d - l2 * l2) / (2 * l1 * d), -1, 1));
    const c1 = [hip[0] + l1 * Math.cos(th - al), hip[1] + l1 * Math.sin(th - al)];
    const c2 = [hip[0] + l1 * Math.cos(th + al), hip[1] + l1 * Math.sin(th + al)];
    return c1[0] > c2[0] ? c1 : c2;
  }
  var GAIT_PRESETS = {
    // natural adult walk, ~2 steps/s
    walk: { period: 1, duty: 0.62, stride: 24, lift: 3.4, heelOff: 15, heelKick: 0, thigh: 23, shin: 23, ankleY: 118.4, ground: 120, toe: 4.2 },
    // kimono, tight skirt, geta: shorter stride, very low lift
    shuffle: { period: 0.9, duty: 0.64, stride: 18, lift: 2.4, heelOff: 12, heelKick: 0, thigh: 23, shin: 23, ankleY: 116, ground: 120, toe: 4.2 },
    // child's or adult's run: short ground contact, flight phase, heel kick
    run: { period: 0.5, duty: 0.36, stride: 16, lift: 12, heelOff: 10, heelKick: 3.5, thigh: 13, shin: 13, ankleY: 118, ground: 120, toe: 3 }
  };
  function gait(opts = {}) {
    const kind = opts.kind ?? "walk";
    const o = { ...GAIT_PRESETS[kind], ...opts, kind };
    const { period, duty, stride: S2, lift, heelOff, heelKick, thigh, shin, ankleY, ground, toe } = o;
    const reach = (thigh + shin) * 0.983;
    const restHip = ankleY - reach;
    const speed = S2 / (duty * period);
    const foot = (u) => {
      u = (u % 1 + 1) % 1;
      if (u < duty) {
        const x2 = 40 + S2 / 2 - S2 * u / duty;
        const peel = smooth(duty - 0.14, duty, u);
        const th = heelOff * peel * Math.PI / 180;
        const px = x2 + toe, py = ground, ox = -toe, oy = ankleY - ground;
        return { ankle: [px + ox * Math.cos(th) - oy * Math.sin(th), py + ox * Math.sin(th) + oy * Math.cos(th)], pitch: heelOff * peel, stance: true };
      }
      const s = (u - duty) / (1 - duty);
      const x = 40 - S2 / 2 + S2 * minJerk(s) - heelKick * Math.sin(Math.PI * Math.min(1, s * 1.6));
      const y = ankleY - lift * Math.pow(Math.sin(Math.PI * Math.pow(s, heelKick ? 0.7 : 1)), 1.2);
      return { ankle: [x, y], pitch: heelOff * Math.pow(1 - smooth(0, 0.55, s), 1.4), stance: false };
    };
    return {
      ...o,
      speed,
      pose(phase, go = 1) {
        const raw = [foot(phase), foot(phase + 0.5)];
        if (go < 1) raw.forEach((fo, i) => {
          const rest = 40 + (i ? -2 : 2);
          fo.ankle = [rest + (fo.ankle[0] - rest) * go, ankleY + (fo.ankle[1] - ankleY) * go];
          fo.pitch *= go;
          fo.stance = true;
        });
        let hipY = restHip;
        const half = (phase % 0.5 + 0.5) % 0.5;
        if (heelKick && go >= 1) {
          hipY = half < duty ? restHip + 1.4 * Math.sin(Math.PI * half / duty) : restHip - 2.6 * Math.sin(Math.PI * (half - duty) / (0.5 - duty));
        } else {
          for (const fo of raw) if (fo.stance) {
            const dx = fo.ankle[0] - 40;
            hipY = Math.max(hipY, fo.ankle[1] - Math.sqrt(Math.max(1, reach * reach - dx * dx)));
          }
        }
        const hip = [40, hipY];
        const feet = raw.map((fo) => ({ ...fo, knee: knee(hip, fo.ankle, thigh, shin) }));
        const front = feet[0].ankle[0] >= feet[1].ankle[0] ? 0 : 1;
        return { hip, feet, front, bob: hipY - restHip };
      }
    };
  }


  // ---- yamlreel engine: draws a `lineart` scene as a pure function of time -------------------------------
  const cl = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const seg = (t, a, b) => cl((t - a) / (b - a));
  const ease = (u) => u * u * (3 - 2 * u);
  const GEO = 'Georgia, "Times New Roman", serif', SANS = 'Arial, Helvetica, sans-serif';
  const walk = gait({ kind: 'walk' });
  let svg, art, C, W, H, U, built = null;

  function text(parent, s, x, y, size, o = {}) {
    const t = el('text', { x, y, 'font-size': size, 'font-family': o.fam || GEO, 'font-weight': o.w || 'bold', fill: o.fill || C.ink, 'text-anchor': o.anchor || 'middle', 'letter-spacing': o.ls || 0 }, parent);
    t.textContent = s; return t;
  }
  function wrapText(s, size, maxW, fam, weight) {
    const ctx = (wrapText.cv || (wrapText.cv = document.createElement('canvas'))).getContext('2d');
    ctx.font = `${weight} ${size}px ${fam}`;
    const out = []; let cur = '';
    for (const w of String(s).split(/\s+/)) {
      const next = cur ? cur + ' ' + w : w;
      if (cur && ctx.measureText(next).width > maxW) { out.push(cur); cur = w; } else cur = next;
    }
    if (cur) out.push(cur);
    return out;
  }
  function inkline(parent, d, w, col) {
    const p = el('path', { d, fill: 'none', stroke: col || C.ink, 'stroke-width': w, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', pathLength: 1 }, parent);
    p.setAttribute('stroke-dasharray', '1 1');
    return (u) => { p.setAttribute('stroke-dashoffset', String(1 - cl(u))); p.setAttribute('opacity', u <= 0 ? '0' : '1'); };
  }
  function drum(parent, cx, cy, w, h) {
    const g = el('g', {}, parent), rx = w / 2, ry = w * 0.11;
    el('path', { d: `M${cx - rx} ${cy} L${cx - rx} ${cy + h} A${rx} ${ry} 0 0 0 ${cx + rx} ${cy + h} L${cx + rx} ${cy}`, fill: C.bg, stroke: C.ink, 'stroke-width': 3 * U }, g);
    const fill = el('rect', { x: cx - rx + 1, y: cy + h, width: w - 2, height: 0, fill: C.accent, opacity: 0.85 }, g);
    el('ellipse', { cx, cy, rx, ry, fill: C.bg, stroke: C.ink, 'stroke-width': 3 * U }, g);
    return (u) => { const hh = (h - 6) * u; fill.setAttribute('y', cy + h - hh); fill.setAttribute('height', hh); };
  }

  // A faceless ink figure. pose(phase, go, carry, wave, x, y, s): carry 1 holds a box, wave 1 raises the near hand.
  function figure(parent, coat) {
    const g = el('g', {}, parent);
    const farArm = limb(g, 3.6), legs = [limb(g, 4.2), limb(g, 4.2)], shoes = [blot(g), blot(g)];
    const body = paint(g, coat), head = el('ellipse', { rx: 3.8, ry: 4.8, fill: C.bg, stroke: 'currentColor', 'stroke-width': PEN }, g);
    const hair = blot(g), nearArm = limb(g, 3.6);
    return {
      g,
      pose(phase, go, carry, wave, x, y, s) {
        place(g, x, y, 1, s);
        const p = walk.pose(phase, go), b = p.bob;
        p.feet.forEach((fo, i) => { legs[i].draw([p.hip, fo.knee, fo.ankle], [0.3, 0.8]); shoe(shoes[i], fo.ankle, fo.pitch); });
        g.insertBefore(legs[p.front].g, body); g.insertBefore(shoes[p.front], body);
        const kF = p.feet[p.front].knee, aF = p.feet[p.front].ankle, sh = [41, 41 + b], hem = 92, fx = Math.max(kF[0] + 2.4, 46.4);
        body.setAttribute('d', `M${f(sh[0] - 6)} ${f(sh[1] + 1)} Q${f(sh[0] - 1)} ${f(sh[1] - 2.4)} ${f(sh[0] + 5)} ${f(sh[1] + 1)} L${f(fx - 0.6)} ${f(70 + b)} Q${f(fx)} ${f(82 + b * 0.5)} ${f(Math.max(fx, aF[0] - 4))} ${hem} L${f(30.6)} ${hem + 1} L${f(33.2)} ${f(70 + b)}Z`);
        const hc = [42.6, 31.6 + b];
        head.setAttribute('cx', f(hc[0])); head.setAttribute('cy', f(hc[1]));
        hair.setAttribute('d', `M${f(hc[0] - 4.2)} ${f(hc[1] - 1)} Q${f(hc[0] - 3.4)} ${f(hc[1] - 6.6)} ${f(hc[0] + 1)} ${f(hc[1] - 5.6)} Q${f(hc[0] + 4)} ${f(hc[1] - 5)} ${f(hc[0] + 4)} ${f(hc[1] - 1.6)} Q${f(hc[0])} ${f(hc[1] - 3.8)} ${f(hc[0] - 4.2)} ${f(hc[1] - 1)}Z`);
        const sw = (p.feet[0].ankle[0] - 40) / (walk.stride / 2), hold = [sh[0] + 14, sh[1] + 20], up = [sh[0] + 17, sh[1] - 14];
        const arm = (l, s0, a, k, aim, mix) => {
          const e = [s0[0] + 17 * Math.sin(a), s0[1] + 17 * Math.cos(a)], free = [e[0] + 16 * Math.sin(a + 0.15), e[1] + 16 * Math.cos(a + 0.15)];
          const tgt = aim === 'up' ? up : [hold[0] + k, hold[1] + k * 0.3];
          const h2 = [free[0] + (tgt[0] - free[0]) * mix, free[1] + (tgt[1] - free[1]) * mix];
          const e2 = aim === 'up' ? [e[0] + (s0[0] + 14 - e[0]) * mix, e[1] + (s0[1] + 2 - e[1]) * mix] : [e[0] + (s0[0] + 6 - e[0]) * mix * 0.6, e[1] + (s0[1] + 18 - e[1]) * mix * 0.3 + 2 * mix];
          l.draw([s0, e2, h2], [0.2, 0.5]);
          return h2;
        };
        const fa = -sw * 14 * go * (1 - carry) * Math.PI / 180, na = sw * 12 * go * (1 - carry - wave) * Math.PI / 180;
        arm(farArm, [sh[0] - 1, sh[1] + 2], fa, -2, 'hold', carry);
        return arm(nearArm, [sh[0] + 1.6, sh[1] + 2.4], na, 1, wave > carry ? 'up' : 'hold', Math.max(carry, wave));
      },
    };
  }
  const stride = (s) => walk.speed * s;
  const COATS = () => [C.accent, C.ink, C.muted, '#8a7f6a'];

  function buildPresenter(spec) {
    const g = el('g', {}, art), vert = H > W, m = Math.min(W, H);
    const s = (vert ? 0.42 : 0.5) * m / 95, ground = vert ? H * 0.70 : H * 0.84, figH = 95 * s;
    // the text column: above the figure in a vertical frame, beside it (right) in landscape and square
    const colW = vert ? W * 0.84 : W * 0.5, cx = vert ? W / 2 : W * 0.66;
    const top = vert ? H * 0.12 : H * 0.14, bottom = vert ? ground - figH - H * 0.04 : ground - H * 0.04, maxH = bottom - top;
    let size = (vert ? 92 : 84) * U, lines, lh;
    const floor = size * 0.4;
    for (;;) { lines = wrapText(spec.text, size, colW, GEO, 'bold'); lh = size * 1.18; if (lines.length * lh <= maxH || size <= floor) break; size *= 0.94; }
    const shrunk = size < (vert ? 92 : 84) * U - 0.5, y0 = top + Math.max(0, (maxH - lines.length * lh) / 2) + size * 0.85;
    const words = lines.map((ln, i) => ({ t: text(g, '', cx, y0 + i * lh, size), ln }));
    const gl = inkline(g, `M${W * 0.08} ${ground} L${W * 0.92} ${ground}`, 3 * U, C.line);
    const fig = figure(g, C.accent), tx = vert ? W * 0.5 : W * 0.2;
    const draw = (t, dur) => {
      const total = lines.join(' ').length, shown = Math.floor(total * cl((t - 0.2) / Math.max(0.5, dur * 0.55)));
      let left = shown;
      words.forEach((w) => { w.t.textContent = w.ln.slice(0, Math.max(0, Math.min(w.ln.length, left))); left -= w.ln.length + 1; });
      gl(seg(t, 0, 0.5));
      const walkDur = 2.0, arrive = 0.4 + walkDur, going = t < arrive && t > 0.4;
      const x = tx - stride(s) * walkDur + stride(s) * cl(t - 0.4, 0, walkDur);
      fig.g.setAttribute('opacity', String(seg(t, 0, 0.3)));
      const wave = ease(seg(t, arrive + 0.3, arrive + 0.9)) * (0.85 + 0.15 * Math.sin(t * 4));
      fig.pose(going ? (t - 0.4) / walk.period : 0, going ? 1 : 0, 0, wave, x, ground, s);
    };
    draw.info = { shrunk, size };
    return draw;
  }

  function buildCarry(spec) {
    const g = el('g', {}, art), vert = H > W, n = spec.items.length, ground = vert ? H * 0.80 : H * 0.88, m0 = W * 0.08;
    const s = Math.min((vert ? 0.26 : 0.30) * Math.min(W, H) / 95, 0.9 * (W - 2 * m0) / (n * 50));
    const slot = (W - 2 * m0) / n, dw = Math.min(slot * 0.8, 300 * U) * 0.8, dh = dw * 0.9;
    const xs = spec.items.map((_, i) => m0 + slot * (i + 0.5)), dy = vert ? H * 0.36 : H * 0.28;
    const gl = inkline(g, `M${W * 0.05} ${ground} L${W * 0.95} ${ground}`, 3 * U, C.line);
    const cv = document.createElement('canvas').getContext('2d');
    let shrunk = false;
    const drums = xs.map((x, i) => {
      const d = drum(g, x, dy, dw, dh), label = spec.items[i].toUpperCase();
      let fs = 34 * U; cv.font = `bold ${fs}px ${SANS}`;
      const wide = cv.measureText(label).width + label.length * 3 * U;
      if (wide > slot * 0.94) { fs *= slot * 0.94 / wide; shrunk = true; }
      text(g, label, x, dy + dh + 56 * U, fs, { fam: SANS, ls: 3 * U * fs / (34 * U) });
      return d;
    });
    const figs = xs.map((_, i) => figure(g, COATS()[i % 4]));
    const boxes = xs.map(() => { const b = el('g', {}, g), r = paint(b, '#e8b78f', 3 * U), q = 22 * U; r.setAttribute('d', `M${-q} ${-q} L${q} ${-q} L${q} ${q} L${-q} ${q}Z`); return b; });
    const draw = (t, dur) => {
      gl(seg(t, 0, 0.5));
      const step = cl((dur - 4.2) / Math.max(1, n - 1), 0.3, 1.7), sp = stride(s);
      xs.forEach((x, k) => {
        const arrive = 2.4 + k * step, stand = Math.max(x - 34 * s, 22 * s), x0 = (stand - sp * 2.2 > -95 * s && stand - sp * 2.2 < 50 * s) ? -95 * s : stand - sp * 2.2, walkDur = (stand - x0) / sp, t0 = arrive - walkDur;
        const going = t >= t0 && t < arrive, px = t < t0 ? x0 : t < arrive ? x0 + sp * (t - t0) : stand;
        const carry = t >= t0 - 0.3 && t < arrive + 0.15 ? 1 : 0;
        const hand = figs[k].pose(going ? (t - t0) / walk.period : 0, going ? 1 : 0, carry, 0, px, ground, s);
        figs[k].g.setAttribute('opacity', t < t0 - 0.6 ? '0' : '1');
        const bx = px + (hand[0] - 40) * s + 14 * U, by = ground + (hand[1] - 120) * s;
        const up = ease(seg(t, arrive + 0.2, arrive + 1.1)), bxx = bx + (x - bx) * up, byy = by + (dy - by) * up;
        boxes[k].setAttribute('transform', `translate(${f(bxx)} ${f(byy)}) scale(${f(1 - up * 0.5)})`);
        boxes[k].setAttribute('opacity', t >= t0 - 0.3 && up < 1 ? '1' : '0');
        drums[k](0.45 * ease(seg(t, arrive + 1.0, arrive + 1.6)));
      });
    };
    draw.info = { shrunk, size: 0 };
    return draw;
  }

  window.LA = {
    setup(w, h, colors) {
      W = w; H = h; U = Math.min(w, h) / 1080; C = colors; built = null;
      document.body.innerHTML = ''; document.body.style.cssText = 'margin:0';
      svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H }, document.body);
      svg.style.display = 'block'; svg.style.color = C.ink;
      el('rect', { width: W, height: H, fill: C.bg }, svg);
      const flt = inkDefs(svg, 'la').filter;
      art = el('g', { filter: flt }, svg);
      return true;
    },
    frame(spec, key, t, dur) {
      if (built === null || built.key !== key) {
        while (art.firstChild) art.removeChild(art.firstChild);
        built = { key, draw: spec.kind === 'carry' ? buildCarry(spec) : buildPresenter(spec) };
      }
      built.draw(t, dur);
      return built.draw.info;
    },
  };
})();
