// Pixel-art aquarium background — fish, octopus, bubbles, kelp.
// Ported from the research_reports/internal/fish-tank-background.html prototype.
// Renders on a fixed canvas at z-index 0, same slot as LandingBackground.

import { useEffect, useRef } from 'react';

// ── Constants (tuned in the prototype) ─────────────────────────────────────
let PIX = 4;              // pixel size — set dynamically by viewport width
const FISH_COUNT = 12;    // default fish
const SPEED = 1.0;        // animation speed multiplier
const OCTO = true;        // Claude-orange octopus enabled

function pixForWidth(w: number): number {
  if (w < 480) return 2;       // phone — zoomed out, smaller critters
  if (w < 1024) return 3;      // tablet — intermediate
  return 4;                     // desktop — original
}

const FISH_COLORS = [
  { b: '#6f9fd8', l: '#9cc1ec', d: '#3f6fa8' },
  { b: '#7fc8a0', l: '#a9e3c2', d: '#4f9170' },
  { b: '#d9c46b', l: '#efe1a0', d: '#a8923f' },
  { b: '#c98ad8', l: '#e3b5ec', d: '#8f5aa8' },
  { b: '#d88a8a', l: '#ecb5b5', d: '#a85a5a' },
  { b: '#79c6cf', l: '#a6e6ec', d: '#499199' },
  { b: '#e0905a', l: '#f0b487', d: '#b56636' },
];

const OCTO_PAL = { b: '#d9774f', l: '#eda07a', d: '#b9542e', eye: '#fdf6ee', pupil: '#241a14' };

// Species profiles — determines body shape, tail, fins, patterns.
const SPECIES: Record<string, SpeciesDef> = {
  tang:   { minL: 12, maxL: 16, hRatio: 1.05, profile: 'disc',    tail: 'fork',  dorsal: 'big',   pattern: 'none',    speedMul: 1.0,  wave: 0 },
  angel:  { minL: 11, maxL: 15, hRatio: 1.18, profile: 'disc',    tail: 'fan',   dorsal: 'tall',  pattern: 'stripes', speedMul: 0.9,  wave: 0 },
  tuna:   { minL: 16, maxL: 22, hRatio: 0.55, profile: 'spindle', tail: 'fork',  dorsal: 'small', pattern: 'belly',   speedMul: 1.25, wave: 0 },
  eel:    { minL: 22, maxL: 30, hRatio: 0.34, profile: 'eel',     tail: 'taper', dorsal: 'ridge', pattern: 'none',    speedMul: 0.7,  wave: 2.4 },
  puffer: { minL: 10, maxL: 13, hRatio: 1.0,  profile: 'round',   tail: 'fan',   dorsal: 'none',  pattern: 'spots',   speedMul: 0.85, spikes: true, wave: 0 },
  darter: { minL: 6,  maxL: 9,  hRatio: 0.72, profile: 'ellipse', tail: 'fork',  dorsal: 'none',  pattern: 'none',    speedMul: 1.6,  wave: 0 },
};
const POOL = ['darter', 'darter', 'darter', 'tang', 'tang', 'tuna', 'tuna', 'angel', 'puffer', 'eel'];

// ── Types ───────────────────────────────────────────────────────────────────
type ProfileType = 'disc' | 'spindle' | 'eel' | 'round' | 'ellipse';
type TailType = 'fork' | 'fan' | 'taper';
type DorsalType = 'big' | 'tall' | 'small' | 'ridge' | 'none';
type PatternType = 'none' | 'stripes' | 'belly' | 'spots';

interface SpeciesDef {
  minL: number; maxL: number; hRatio: number;
  profile: ProfileType; tail: TailType; dorsal: DorsalType;
  pattern: PatternType; speedMul: number; wave: number;
  spikes?: boolean;
}

interface Fish {
  sp: SpeciesDef; name: string; len: number;
  x: number; y: number; dir: number; vx: number;
  bob: number; bobPh: number; flapPh: number;
  col: typeof FISH_COLORS[number];
}

interface Bubble { x: number; y: number; r: number; vy: number; sway: number; ph: number; }
interface Snow { x: number; y: number; vy: number; vx: number; a: number; }
interface Kelp { x: number; h: number; ph: number; col: string; w: number; }
interface Pebble { x: number; w: number; h: number; c: string; }
interface Octopus { x: number; y: number; ph: number; face: number; }

// ── Helpers ─────────────────────────────────────────────────────────────────
function rand(a: number, b: number) { return a + Math.random() * (b - a); }
function pick<T>(a: T[]) { return a[(Math.random() * a.length) | 0]; }

function profileFactor(sp: SpeciesDef, u: number): number {
  const a = Math.abs(u);
  switch (sp.profile) {
    case 'disc':    return Math.pow(Math.max(0, 1 - u * u), 0.34);
    case 'spindle': return Math.max(0, 1 - u * u);
    case 'eel':     return Math.sqrt(Math.max(0, 1 - Math.pow(a, 2.2))) * 0.9;
    case 'round':   return Math.sqrt(Math.max(0, 1 - u * u));
    default:        return Math.sqrt(Math.max(0, 1 - u * u));
  }
}

// ── Component ───────────────────────────────────────────────────────────────
export function FishTankBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const vignetteRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    // Capture as non-null for the closures (frame / resize) below.
    const _canvas = canvas;
    const _ctx = ctx;

    // Low-res buffer → fat pixels on the visible canvas.
    const buf = document.createElement('canvas');
    const bctx = buf.getContext('2d')!;
    bctx.imageSmoothingEnabled = false;

    let W = 0, H = 0, bw = 0, bh = 0;
    let t = 0;

    // Scene state
    const fish: Fish[] = [];
    const bubbles: Bubble[] = [];
    const snow: Snow[] = [];
    const kelp: Kelp[] = [];
    const pebbles: Pebble[] = [];
    let octopus: Octopus = { x: 0, y: 0, ph: 0, face: 1 };

    // ── Factory helpers ───────────────────────────────────────────────────
    function makeFish(): Fish {
      const name = pick(POOL);
      const sp = SPECIES[name];
      const len = (rand(sp.minL, sp.maxL)) | 0;
      return {
        sp, name, len,
        x: rand(0, bw), y: rand(bh * 0.14, bh * 0.82),
        dir: Math.random() < 0.5 ? 1 : -1,
        vx: rand(7, 15) * sp.speedMul,
        bob: rand(0.5, 1.4), bobPh: rand(0, 6.28), flapPh: rand(0, 6.28),
        col: pick(FISH_COLORS),
      };
    }
    function makeBubble(): Bubble {
      return { x: rand(0, bw), y: rand(0, bh), r: rand(0.6, 2.2), vy: rand(6, 14), sway: rand(0.3, 1), ph: rand(0, 6.28) };
    }
    function makeSnow(): Snow {
      return { x: rand(0, bw), y: rand(0, bh), vy: rand(1.5, 4), vx: rand(-1, 1), a: rand(0.1, 0.35) };
    }

    function buildScene() {
      fish.length = 0;
      for (let i = 0; i < 24; i++) fish.push(makeFish());
      bubbles.length = 0;
      for (let j = 0; j < 36; j++) bubbles.push(makeBubble());
      snow.length = 0;
      for (let k = 0; k < 60; k++) snow.push(makeSnow());
      octopus = { x: bw * 0.5, y: bh * 0.55, ph: rand(0, 6.28), face: 1 };
    }

    function buildStatic() {
      kelp.length = 0;
      const n = Math.max(4, (bw / 60) | 0);
      for (let i = 0; i < n; i++) {
        kelp.push({
          x: rand(0, bw), h: rand(bh * 0.28, bh * 0.55), ph: rand(0, 6.28),
          col: Math.random() < 0.5 ? '#3f6f4a' : '#2f5238', w: 2 + ((Math.random() * 2) | 0),
        });
      }
      pebbles.length = 0;
      const pn = (bw / 12) | 0;
      for (let p = 0; p < pn; p++) {
        pebbles.push({
          x: rand(0, bw), w: rand(2, 5) | 0, h: rand(1, 3) | 0,
          c: pick(['#3a352c', '#2e2a22', '#454434', '#4a4438']),
        });
      }
    }

    // ── Drawing primitives ────────────────────────────────────────────────
    function rect(x: number, y: number, w: number, h: number, color: string) {
      bctx.fillStyle = color;
      bctx.fillRect(x | 0, y | 0, Math.max(1, w | 0), Math.max(1, h | 0));
    }

    // ── Environment passes ────────────────────────────────────────────────
    function drawWater() {
      const g = bctx.createLinearGradient(0, 0, 0, bh);
      g.addColorStop(0, '#0e323b'); g.addColorStop(0.55, '#0a2129'); g.addColorStop(1, '#06141a');
      bctx.fillStyle = g; bctx.fillRect(0, 0, bw, bh);
    }
    function drawRays() {
      bctx.save(); bctx.globalAlpha = 0.06; bctx.fillStyle = '#bfe6ec';
      for (let i = 0; i < 4; i++) {
        const bx = (bw * (i + 0.5) / 4) + Math.sin(t * 0.15 + i) * bw * 0.06;
        bctx.beginPath(); bctx.moveTo(bx - 3, 0); bctx.lineTo(bx + 3, 0);
        bctx.lineTo(bx + 22, bh); bctx.lineTo(bx + 10, bh); bctx.closePath(); bctx.fill();
      }
      bctx.restore();
    }
    function drawSnow() {
      for (const s of snow) { bctx.globalAlpha = s.a; rect(s.x, s.y, 1, 1, '#cfeef2'); }
      bctx.globalAlpha = 1;
    }
    function drawKelp() {
      for (const k of kelp) {
        const segs = (k.h / 4) | 0;
        for (let s = 0; s < segs; s++) {
          const f = s / segs, sway = Math.sin(t * 0.8 + k.ph + f * 3) * (f * 6);
          rect(k.x + sway - (k.w >> 1), bh - 3 - s * 4, k.w, 4, s % 2 ? k.col : '#356b46');
        }
      }
    }
    function drawFloor() {
      rect(0, bh - 4, bw, 4, '#2a2620'); rect(0, bh - 5, bw, 1, '#37322a');
      for (const p of pebbles) rect(p.x, bh - 4 - (p.h - 1), p.w, p.h, p.c);
    }
    function drawBubbles() {
      for (const b of bubbles) {
        const x = b.x + Math.sin(t * b.sway + b.ph) * 2;
        bctx.globalAlpha = 0.5; rect(x, b.y, Math.max(1, b.r | 0), Math.max(1, b.r | 0), '#bfe6ec');
        bctx.globalAlpha = 0.85; rect(x, b.y, 1, 1, '#eafbff');
      }
      bctx.globalAlpha = 1;
    }

    // ── Fish ──────────────────────────────────────────────────────────────
    function drawFish(f: Fish) {
      const { sp } = f;
      const L = f.len;
      const Hh = Math.max(2, (L * sp.hRatio * 0.6) | 0);
      const y0 = f.y + Math.sin(t * 1.6 + f.bobPh) * f.bob;
      const flap = Math.sin(t * 8 + f.flapPh);
      const { col } = f;

      bctx.save();
      bctx.translate(f.x | 0, y0 | 0);
      if (f.dir < 0) bctx.scale(-1, 1);

      // Body — column by column
      for (let cx = 0; cx < L; cx++) {
        const u = (cx / (L - 1)) * 2 - 1;
        const colH = Math.max(1, (profileFactor(sp, u) * Hh) | 0);
        const wy = sp.wave ? Math.sin(t * 3 + cx * 0.5 + f.flapPh) * sp.wave : 0;
        const top = -(colH >> 1) + wy;
        rect(cx, top, 1, colH, col.b);
        rect(cx, top, 1, 1, col.l);
        rect(cx, top + colH - 1, 1, 1, col.d);
        if (sp.pattern === 'belly' && colH > 3) rect(cx, top + colH - 2, 1, 2, col.l);
        if (sp.pattern === 'stripes' && cx % 3 === 0 && colH > 2) rect(cx, top + 1, 1, colH - 2, col.d);
        if (sp.pattern === 'spots' && (cx * 7 + colH * 5) % 6 === 0) rect(cx, top + (colH >> 1), 1, 1, col.d);
      }

      // Dorsal fin / ridge
      if (sp.dorsal && sp.dorsal !== 'none') {
        const dh = sp.dorsal === 'tall' ? Hh * 0.55 : sp.dorsal === 'big' ? Hh * 0.38 : sp.dorsal === 'small' ? Hh * 0.22 : Hh * 0.18;
        const a = (L * 0.28) | 0, b = (L * 0.72) | 0;
        for (let dx = a; dx < b; dx++) {
          const du = (dx - (a + b) / 2) / ((b - a) / 2);
          const topY = -(profileFactor(sp, (dx / (L - 1)) * 2 - 1) * Hh) / 2;
          const fy = topY - (1 - du * du) * dh + Math.sin(t * 4 + f.flapPh) * 0.4;
          if (sp.dorsal === 'ridge') rect(dx, topY - 1, 1, 1, col.l);
          else rect(dx, fy, 1, Math.max(1, topY - fy), col.d);
        }
      }

      // Tail
      const spread = 2 + Math.abs(flap) * 3;
      if (sp.tail === 'fan') {
        for (let ty = -spread; ty <= spread; ty++) { const tw = (1 - Math.abs(ty) / spread) * 5 + 1; rect(-tw, ty + flap, tw, 1, col.d); }
      } else if (sp.tail === 'fork') {
        for (let k = 0; k <= spread; k++) { const fw = (1 - k / spread) * 5 + 1; rect(-fw, -k - 1 + flap, fw, 1, col.d); rect(-fw, k + 1 + flap, fw, 1, col.d); }
      } else if (sp.tail === 'taper') {
        rect(-3, -1 + flap, 3, 2, col.d); rect(-2, -1 + flap, 2, 3, col.b);
      }

      // Puffer spikes
      if (sp.spikes) {
        for (let sx = 1; sx < L; sx += 3) {
          const ch = (profileFactor(sp, (sx / (L - 1)) * 2 - 1) * Hh) | 0;
          rect(sx, -(ch >> 1) - 2, 1, 2, col.d);
          rect(sx, (ch >> 1) + 1, 1, 2, col.d);
        }
      }

      // Eye
      const ex = L - Math.max(3, (L * 0.22) | 0);
      rect(ex, -1, 2, 2, '#1a1712'); rect(ex, -1, 1, 1, '#fff');
      bctx.restore();
    }

    // ── Claude octopus ────────────────────────────────────────────────────
    function drawOctopus() {
      const o = octopus;
      const ox = o.x + Math.sin(t * 0.4 + o.ph) * bw * 0.18;
      const oy = o.y + Math.cos(t * 0.31 + o.ph) * bh * 0.12;
      o.face = Math.cos(t * 0.4 + o.ph) >= 0 ? 1 : -1;

      const R = Math.max(10, (bw / 60) | 0);
      const headW = R * 2.0;
      const squash = 1 + Math.sin(t * 2 + o.ph) * 0.05;
      const headH = R * 1.7 * squash;
      const topY = -headH / 2, botY = headH / 2;

      bctx.save();
      bctx.translate(ox | 0, oy | 0);

      // Tentacles (behind the head)
      const legs = 8, span = headW * 0.86;
      for (let i = 0; i < legs; i++) {
        const baseX = -span / 2 + (i + 0.5) * (span / legs);
        const dirOut = baseX >= 0 ? 1 : -1;
        const legLen = headH * 0.95 + ((i % 2) ? R * 0.45 : 0);
        const phase = i * 0.6;
        for (let s = 0; s < legLen; s++) {
          const f = s / legLen;
          const wob = Math.sin(t * 3 + phase + f * 3.2) * (f * f * R * 0.65);
          const lx = baseX + dirOut * f * R * 0.5 + wob;
          const ly = botY - 2 + s;
          const w = Math.max(1, Math.round(3 * (1 - f)) + 1);
          rect(lx - w / 2, ly, w, 1, OCTO_PAL.b);
          rect(lx - w / 2, ly, 1, 1, OCTO_PAL.d);
          if (s % 3 === 1 && f < 0.85) rect(lx, ly, 1, 1, OCTO_PAL.l);
        }
      }

      // Head — rounded square
      const cornerR = Math.max(2, (R * 0.4) | 0);
      for (let ry = 0; ry < headH; ry++) {
        const dTop = cornerR - ry;
        const dBot = cornerR - (headH - 1 - ry);
        const inset = Math.max(0, Math.max(dTop, dBot));
        rect(-headW / 2 + inset, topY + ry, headW - inset * 2, 1, OCTO_PAL.b);
      }
      rect(-headW / 2 + cornerR, topY, headW - cornerR * 2, 1, OCTO_PAL.l);
      rect(-headW / 2 + cornerR, botY - 1, headW - cornerR * 2, 1, OCTO_PAL.d);

      // Face
      const eyeW = Math.max(3, (R * 0.6) | 0), eyeH = Math.max(4, (R * 0.78) | 0);
      const eyeY = topY + headH * 0.3, eyeDX = headW * 0.23;
      const blink = (t % 4.3) < 0.13;
      const sides = [-1, 1];
      for (let e = 0; e < 2; e++) {
        const ex2 = sides[e] * eyeDX - eyeW / 2;
        if (blink) { rect(ex2, eyeY + eyeH / 2, eyeW, 1, OCTO_PAL.d); continue; }
        rect(ex2, eyeY, eyeW, eyeH, OCTO_PAL.eye);
        const pw = Math.max(2, (eyeW * 0.5) | 0), ph2 = Math.max(2, (eyeH * 0.5) | 0);
        const px2 = ex2 + (eyeW - pw) / 2 + o.face * (eyeW * 0.2);
        const py2 = eyeY + (eyeH - ph2) / 2 + 1;
        rect(px2, py2, pw, ph2, OCTO_PAL.pupil);
        rect(px2 + (o.face < 0 ? pw - 1 : 0), py2, 1, 1, OCTO_PAL.eye);
      }
      // Smile
      const my = eyeY + eyeH + 2;
      rect(-2, my, 4, 1, OCTO_PAL.d); rect(-3, my - 1, 1, 1, OCTO_PAL.d); rect(2, my - 1, 1, 1, OCTO_PAL.d);

      bctx.restore();
    }

    // ── Simulation ────────────────────────────────────────────────────────
    function update(dt: number) {
      for (let i = 0; i < FISH_COUNT; i++) {
        const f = fish[i];
        f.x += f.dir * f.vx * SPEED * dt;
        const m = f.len + 10;
        if (f.x > bw + m) { f.x = -m; f.y = rand(bh * 0.14, bh * 0.82); }
        if (f.x < -m) { f.x = bw + m; f.y = rand(bh * 0.14, bh * 0.82); }
      }
      for (const b of bubbles) { b.y -= b.vy * SPEED * dt; if (b.y < -3) { b.y = bh + 3; b.x = rand(0, bw); } }
      for (const s of snow) { s.y += s.vy * SPEED * dt; s.x += s.vx * SPEED * dt; if (s.y > bh + 2) { s.y = -2; s.x = rand(0, bw); } }
    }

    // ── Main loop ─────────────────────────────────────────────────────────
    let last = 0;
    let raf = 0;
    function frame(now: number) {
      const dt = Math.min(0.05, last ? (now - last) / 1000 : 0.016);
      last = now;
      t += dt;
      update(dt);

      bctx.clearRect(0, 0, bw, bh);
      drawWater(); drawRays(); drawSnow(); drawKelp();
      const half = (FISH_COUNT / 2) | 0;
      for (let i = 0; i < half; i++) drawFish(fish[i]);
      if (OCTO) drawOctopus();
      for (let j = half; j < FISH_COUNT; j++) drawFish(fish[j]);
      drawBubbles(); drawFloor();

      _ctx.imageSmoothingEnabled = false;
      _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
      _ctx.drawImage(buf, 0, 0, bw, bh, 0, 0, _canvas.width, _canvas.height);
      raf = requestAnimationFrame(frame);
    }

    // ── Resize ────────────────────────────────────────────────────────────
    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      const newPix = pixForWidth(W);
      const pixChanged = newPix !== PIX;
      PIX = newPix;
      _canvas.width = W; _canvas.height = H;
      _canvas.style.width = W + 'px'; _canvas.style.height = H + 'px';
      bw = Math.max(80, Math.ceil(W / PIX));
      bh = Math.max(60, Math.ceil(H / PIX));
      buf.width = bw; buf.height = bh;
      bctx.imageSmoothingEnabled = false;
      buildStatic();
      if (pixChanged || !fish.length) buildScene();
      else { octopus.x = bw * 0.5; octopus.y = bh * 0.55; }
    }

    resize();
    raf = requestAnimationFrame(frame);
    window.addEventListener('resize', resize);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return (
    <>
      <canvas
        ref={canvasRef}
        style={{
          position: 'fixed', inset: 0, display: 'block',
          imageRendering: 'pixelated', zIndex: 0, pointerEvents: 'none',
        }}
      />
      <div
        ref={vignetteRef}
        style={{
          position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none',
          background: 'radial-gradient(120% 90% at 50% 35%, transparent 45%, rgba(0,0,0,0.55) 100%)',
        }}
      />
    </>
  );
}
