import { useEffect, useMemo, useRef } from 'react';
import type { Theme } from '../theme';

type BgMode = 'constellation' | 'waves' | 'ticker' | 'grid';

type Props = {
  mode?: BgMode;
  density?: number;
  speed?: number;
  grain?: number;
  // Multiplier on node/link alpha. 1 = stock; 1.5 = 50% brighter. Capped at 1.0 alpha.
  intensity?: number;
  // 'document' (default) — constellation fills the whole scrolling document
  //   and nodes appear anchored to the page as it scrolls (landing page).
  // 'viewport' — nodes live in the viewport box and ignore scroll, so
  //   scrolling content over them leaves the field visibly stationary
  //   (dashboard on mobile, where the body scrolls).
  anchor?: 'document' | 'viewport';
  theme: Theme;
  accent: string;
  // Outer vignette overlay color — defaults to a dark radial that pulls the
  // eye to the center. Light themes pass 'transparent' so the cream page
  // background isn't tinted dark in the corners.
  vignette?: string;
};

type Node = {
  x: number; y: number; vx: number; vy: number; r: number; phase: number;
  // Twinkle event state. `tStart` is the elapsed-seconds timestamp when the
  // current shimmer began; -1 means the node is not currently shimmering.
  // Each shimmer plays a bell-curve halo + brightness pulse and emits a
  // one-shot velocity impulse on neighboring nodes (the "ripple").
  tStart: number;
};
type Candle = { x: number; o: number; c: number; h: number; l: number; up: boolean; offset: number };
type Wave = { amp: number; freq: number; phase: number; yBase: number; speed: number; alpha: number };

function hexToRgb(color: string) {
  // theme.text arrives as hex at the brightness endpoints but as an rgba()
  // string mid-flip (the dashboard theme crossfades tokens), so accept both.
  const m = color.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(',').map(s => Number(s.trim()));
    return { r, g, b };
  }
  const h = color.replace('#', '');
  const v = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
  const n = parseInt(v, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

export function LandingBackground({
  mode = 'constellation',
  density = 0.6,
  speed = 1,
  grain = 0.4,
  intensity = 1,
  anchor = 'document',
  theme,
  accent,
  vignette = 'rgba(0,0,0,0.45)',
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const mouseRef = useRef({ x: -9999, y: -9999, active: false });
  const rafRef = useRef(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let w = 0, h = 0, dpr = 1, docH = 0;
    let nodes: Node[] = [];
    let candles: Candle[] = [];
    let waves: Wave[] = [];

    const getDocH = () => anchor === 'viewport'
      ? h
      : Math.max(
        document.documentElement.scrollHeight,
        document.body.scrollHeight,
        window.innerHeight,
      );

    const resize = () => {
      const prevW = w, prevDocH = docH;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      // Use window.inner* instead of canvas.client* — on iOS the resize event
      // fires before CSS layout is repainted, so clientWidth/clientHeight still
      // return the pre-rotation values at the time of the call. inner* updates
      // synchronously, giving the correct new dimensions for the rescale math.
      w = window.innerWidth;
      h = window.innerHeight;
      docH = getDocH();
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // A resize (e.g. phone rotation) changes w/docH out from under the
      // constellation's existing node positions — without rescaling, a field
      // laid out for the old box renders squished/offset inside the new one.
      // Scale proportionally so the field keeps its layout instead of
      // jumping or cramming into a corner.
      if (mode === 'constellation' && prevW > 0 && prevDocH > 0 && (prevW !== w || prevDocH !== docH)) {
        const sx = w / prevW;
        const sy = docH / prevDocH;
        nodes.forEach(n => { n.x *= sx; n.y *= sy; });
      }
    };
    resize();
    window.addEventListener('resize', resize);

    // In viewport mode we don't care about document height changes — the field
    // is locked to the visible box, so the ResizeObserver is skipped.
    const docObserver = anchor === 'document'
      ? new ResizeObserver(() => { docH = getDocH(); })
      : null;
    if (docObserver && document.body) docObserver.observe(document.body);

    const mx = (e: MouseEvent) => {
      // Document-anchored field lives in page coords → add scrollY so mouse
      // interaction tracks the same coordinate space as the nodes. Viewport-
      // anchored field is already in viewport coords, so use clientY as-is.
      const y = anchor === 'viewport' ? e.clientY : e.clientY + window.scrollY;
      mouseRef.current = { x: e.clientX, y, active: true };
    };
    const mout = () => { mouseRef.current.active = false; };
    window.addEventListener('mousemove', mx);
    window.addEventListener('mouseleave', mout);

    // Constellation strokes follow the theme's text color so the field stays
    // visible in Light mode (cream strokes on a cream page would vanish).
    const accentRgb = hexToRgb(accent);
    const textRgb = hexToRgb(theme.text);

    const buildConstellation = () => {
      const area = w * docH;
      const n = Math.floor(Math.max(30, Math.min(320, area / 18000 * density)));
      nodes = Array.from({ length: n }, () => ({
        x: Math.random() * w,
        y: Math.random() * docH,
        vx: (Math.random() - 0.5) * 0.18 * speed,
        vy: (Math.random() - 0.5) * 0.18 * speed,
        r: 0.6 + Math.random() * 1.6,
        phase: Math.random() * Math.PI * 2,
        tStart: -1,
      }));
    };

    const buildCandles = () => {
      const step = Math.max(14, 34 - density * 20);
      const count = Math.floor(w / step) + 2;
      let price = 50;
      candles = Array.from({ length: count }, (_, i) => {
        const drift = (Math.random() - 0.5) * 8;
        const o = price, c = price + drift;
        const hi = Math.max(o, c) + Math.random() * 4;
        const lo = Math.min(o, c) - Math.random() * 4;
        price = c;
        return { x: i * step, o, c, h: hi, l: lo, up: c >= o, offset: Math.random() * 2 };
      });
    };

    const buildWaves = () => {
      const layerCount = Math.max(3, Math.floor(density * 6));
      waves = Array.from({ length: layerCount }, (_, i) => ({
        amp: 40 + i * 18,
        freq: 0.004 + i * 0.0012,
        phase: Math.random() * Math.PI * 2,
        yBase: h * (0.35 + i * 0.1),
        speed: (0.4 + i * 0.12) * speed,
        alpha: 0.05 + (layerCount - i) * 0.04,
      }));
    };

    const rebuild = () => {
      if (mode === 'ticker') buildCandles();
      else if (mode === 'waves') buildWaves();
      else if (mode === 'grid') { /* procedural */ }
      else buildConstellation();
    };
    rebuild();

    const t0 = performance.now();

    const frame = (now: number) => {
      const elapsed = (now - t0) / 1000;
      ctx.clearRect(0, 0, w, h);

      if (mode === 'constellation') {
        const mouse = mouseRef.current;
        // Viewport-anchored field ignores page scroll entirely — the nodes
        // stay locked to the viewport box so scrolling content over them
        // reads as parallax-free stationary stars.
        const scrollY = anchor === 'viewport' ? 0 : window.scrollY;
        const linkR = 130;
        // Twinkle / ripple constants. TWINKLE_DUR is the bell-curve length
        // (seconds) — shimmer fades in to peak at the midpoint, fades back
        // out. TWINKLE_RATE is the per-node-per-frame chance of starting a
        // shimmer; tuned so the whole field produces a few shimmers per
        // second without ever feeling busy. RIPPLE_R / RIPPLE_F set how far
        // the shock travels and how hard it shoves neighbors. Impulses are
        // one-shot velocity adds — the existing 0.995 damping naturally
        // settles them back to the floaty drift speed within a second or so.
        const TWINKLE_DUR = 1.6;
        const TWINKLE_RATE = 0.0006;
        const RIPPLE_R = 220;
        const RIPPLE_F = 0.18;
        nodes.forEach(n => {
          n.x += n.vx;
          n.y += n.vy;
          if (n.x < 0 || n.x > w) n.vx *= -1;
          if (n.y < 0 || n.y > docH) n.vy *= -1;
          if (mouse.active) {
            const dx = n.x - mouse.x, dy = n.y - mouse.y;
            const d2 = dx * dx + dy * dy;
            if (d2 < 180 * 180) {
              const f = (1 - Math.sqrt(d2) / 180) * 0.3;
              n.vx += (dx / Math.sqrt(d2 + 1)) * f;
              n.vy += (dy / Math.sqrt(d2 + 1)) * f;
            }
          }
          n.vx += (Math.random() - 0.5) * 0.04 * speed;
          n.vy += (Math.random() - 0.5) * 0.04 * speed;
          n.vx *= 0.995; n.vy *= 0.995;
          const sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
          const minSp = 0.15 * speed;
          const maxSp = 0.9 * speed;
          if (sp < minSp && sp > 0) {
            n.vx = (n.vx / sp) * minSp;
            n.vy = (n.vy / sp) * minSp;
          } else if (sp > maxSp) {
            n.vx = (n.vx / sp) * maxSp;
            n.vy = (n.vy / sp) * maxSp;
          }
          // End a shimmer that has run its course. Done here so the rest of
          // the frame sees a clean tStart=-1 before the next dice roll.
          if (n.tStart >= 0 && elapsed - n.tStart > TWINKLE_DUR) {
            n.tStart = -1;
          }
          // Roll for a new shimmer. Only nodes not currently shimmering can
          // start one — keeps the visual rate predictable and prevents back-
          // to-back stacked halos on the same dot.
          if (n.tStart < 0 && Math.random() < TWINKLE_RATE) {
            n.tStart = elapsed;
            // Fire the ripple — one-shot outward velocity impulse on
            // neighbors. Falls off linearly with distance so far nodes get a
            // gentle nudge and close ones get a stronger shove. The 0.995
            // damping already in place will settle these back to baseline
            // drift within ~a second, so this stays floaty.
            for (let i = 0; i < nodes.length; i++) {
              const m = nodes[i];
              if (m === n) continue;
              const dx = m.x - n.x, dy = m.y - n.y;
              const d2 = dx * dx + dy * dy;
              if (d2 > RIPPLE_R * RIPPLE_R || d2 < 0.5) continue;
              const d = Math.sqrt(d2);
              const f = (1 - d / RIPPLE_R) * RIPPLE_F;
              m.vx += (dx / d) * f;
              m.vy += (dy / d) * f;
            }
          }
        });
        const vTop = scrollY - 200;
        const vBot = scrollY + h + 200;
        const visible = nodes.filter(n => n.y >= vTop && n.y <= vBot);
        ctx.lineWidth = 0.6;
        for (let i = 0; i < visible.length; i++) {
          for (let j = i + 1; j < visible.length; j++) {
            const a = visible[i], b = visible[j];
            const dx = a.x - b.x, dy = a.y - b.y;
            const d = Math.sqrt(dx * dx + dy * dy);
            if (d < linkR) {
              const alpha = Math.min(1, (1 - d / linkR) * 0.22 * intensity);
              ctx.strokeStyle = `rgba(${textRgb.r},${textRgb.g},${textRgb.b},${alpha})`;
              ctx.beginPath();
              ctx.moveTo(a.x, a.y - scrollY);
              ctx.lineTo(b.x, b.y - scrollY);
              ctx.stroke();
            }
          }
        }
        visible.forEach(n => {
          const twinkle = 0.5 + 0.5 * Math.sin(elapsed * 1.2 + n.phase);
          // Shimmer envelope: bell curve over the event's lifetime. 0 at
          // start/end, 1 at the midpoint. sin(πt) is the simplest expression
          // that gives a smooth grow-and-fade with no popping at the edges.
          const shimmer = n.tStart >= 0
            ? Math.sin(((elapsed - n.tStart) / TWINKLE_DUR) * Math.PI)
            : 0;
          // Soft halo behind the node during a shimmer. Radial gradient from
          // accent at the center to fully transparent at haloR; haloR scales
          // with shimmer so the halo grows in and shrinks back out with the
          // same envelope as the brightness.
          if (shimmer > 0) {
            const haloR = n.r + 14 + 14 * shimmer;
            const grad = ctx.createRadialGradient(
              n.x, n.y - scrollY, 0,
              n.x, n.y - scrollY, haloR,
            );
            grad.addColorStop(0, `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},${0.45 * shimmer})`);
            grad.addColorStop(0.4, `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},${0.12 * shimmer})`);
            grad.addColorStop(1, `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},0)`);
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(n.x, n.y - scrollY, haloR, 0, Math.PI * 2);
            ctx.fill();
          }
          // Node core. During a shimmer we bump brightness toward 1 and grow
          // the radius slightly so the dot reads as a brief flare, not just
          // a halo around the same dot.
          const baseAlpha = 0.35 + twinkle * 0.35;
          const nodeAlpha = Math.min(1, (baseAlpha + shimmer * 0.6) * intensity);
          const nodeR = n.r * (1 + shimmer * 0.5);
          ctx.fillStyle = `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},${nodeAlpha})`;
          ctx.beginPath();
          ctx.arc(n.x, n.y - scrollY, nodeR, 0, Math.PI * 2);
          ctx.fill();
        });
        if (mouse.active) {
          const mxv = mouse.x, myv = mouse.y - scrollY;
          const grad = ctx.createRadialGradient(mxv, myv, 0, mxv, myv, 160);
          grad.addColorStop(0, `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},0.07)`);
          grad.addColorStop(1, `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},0)`);
          ctx.fillStyle = grad;
          ctx.fillRect(mxv - 160, myv - 160, 320, 320);
        }
      }

      else if (mode === 'ticker') {
        const scroll = (elapsed * 28 * speed) % 34;
        const step = Math.max(14, 34 - density * 20);
        candles.forEach((c, i) => {
          const x = (i * step - scroll + w + 200) % (w + 200) - 100;
          const y0 = h * 0.5;
          const scale = 1.4;
          const top = y0 - c.h * scale;
          const bot = y0 - c.l * scale;
          const openY = y0 - c.o * scale;
          const clsY = y0 - c.c * scale;
          const clr = c.up ? theme.green : theme.red;
          ctx.globalAlpha = 0.18;
          ctx.strokeStyle = clr;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x, top);
          ctx.lineTo(x, bot);
          ctx.stroke();
          ctx.fillStyle = clr;
          ctx.fillRect(x - 2, Math.min(openY, clsY), 4, Math.max(Math.abs(openY - clsY), 1));
        });
        ctx.globalAlpha = 1;
      }

      else if (mode === 'waves') {
        waves.forEach((lyr, idx) => {
          ctx.beginPath();
          for (let x = 0; x <= w; x += 6) {
            const y = lyr.yBase + Math.sin(x * lyr.freq + elapsed * lyr.speed + lyr.phase) * lyr.amp
                    + Math.sin(x * lyr.freq * 2.3 + elapsed * lyr.speed * 1.3) * (lyr.amp * 0.3);
            if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          const isAccent = idx % 2 === 0;
          const rgb = isAccent ? accentRgb : textRgb;
          ctx.strokeStyle = `rgba(${rgb.r},${rgb.g},${rgb.b},${lyr.alpha})`;
          ctx.lineWidth = 1.2;
          ctx.stroke();
        });
      }

      else if (mode === 'grid') {
        const gridSize = 60;
        const mouse = mouseRef.current;
        for (let x = 0; x < w; x += gridSize) {
          for (let y = 0; y < h; y += gridSize) {
            let a = 0.04 + Math.sin(elapsed * 1.2 + (x + y) * 0.008) * 0.04;
            if (mouse.active) {
              const dx = x - mouse.x, dy = y - mouse.y;
              const d = Math.sqrt(dx * dx + dy * dy);
              if (d < 200) a += (1 - d / 200) * 0.35;
            }
            ctx.fillStyle = `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},${Math.min(a, 0.5)})`;
            ctx.fillRect(x - 0.5, y - 0.5, 1, 1);
          }
        }
        ctx.strokeStyle = `rgba(${accentRgb.r},${accentRgb.g},${accentRgb.b},0.05)`;
        ctx.lineWidth = 1;
        for (let x = 0; x < w; x += gridSize * 2) {
          ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
        }
      }

      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);

    // Pause the animation loop entirely while the page is hidden (phone screen
    // off, app backgrounded, or a different browser tab). A canvas RAF that
    // keeps running in the background is pure wasted battery/heat — there is
    // nothing to see. Browsers throttle background RAF but don't fully stop it;
    // this does. Resumes seamlessly on return (elapsed is absolute-time based).
    const onVisibility = () => {
      if (document.hidden) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = 0;
      } else if (!rafRef.current) {
        rafRef.current = requestAnimationFrame(frame);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('resize', resize);
      window.removeEventListener('mousemove', mx);
      window.removeEventListener('mouseleave', mout);
      document.removeEventListener('visibilitychange', onVisibility);
      try { docObserver?.disconnect(); } catch { /* noop */ }
    };
  }, [mode, density, speed, accent, theme, intensity, anchor]);

  const grainSvg = useMemo(() => {
    const s = `<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.55 0'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='1'/></svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(s).replace(/%23/g, '%23')}`;
  }, []);

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none', overflow: 'hidden' }}>
      <div style={{
        position: 'absolute', inset: 0,
        background: `
          radial-gradient(ellipse 70% 55% at 72% 35%, ${accent}22, transparent 62%),
          radial-gradient(ellipse 55% 40% at 15% 85%, ${accent}16, transparent 60%),
          radial-gradient(ellipse 42% 36% at 22% 22%, ${accent}18, transparent 62%),
          radial-gradient(ellipse 45% 38% at 88% 68%, ${accent}14, transparent 62%)
        `,
      }} />
      <canvas ref={canvasRef} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }} />
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `url("${grainSvg}")`,
        opacity: grain,
        mixBlendMode: 'overlay',
      }} />
      <div style={{
        position: 'absolute', inset: 0,
        background: `radial-gradient(ellipse at center, transparent 40%, ${vignette} 100%)`,
      }} />
    </div>
  );
}
