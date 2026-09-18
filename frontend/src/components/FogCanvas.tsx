import { useEffect, useRef } from "react";

/**
 * Lightweight animated fog/smoke using layered drifting radial blobs on a
 * single canvas. GPU-cheap, pauses when tab hidden, respects reduced-motion,
 * and scales density by `intensity` (0..1).
 */
export function FogCanvas({ intensity = 0.6 }: { intensity?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current!;
    const ctx = canvas.getContext("2d", { alpha: true })!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let running = true;
    let w = 0;
    let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const blobCount = Math.max(3, Math.round(6 * intensity));
    const blobs = Array.from({ length: blobCount }, () => ({
      x: Math.random(),
      y: Math.random(),
      r: 0.25 + Math.random() * 0.35,
      dx: (Math.random() - 0.5) * 0.00006,
      dy: (Math.random() - 0.5) * 0.00006,
      hue: Math.random() > 0.5 ? "74,214,255" : "138,123,255",
      a: (0.045 + Math.random() * 0.055) * intensity,
    }));

    const resize = () => {
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    const draw = () => {
      ctx.clearRect(0, 0, w, h);
      for (const b of blobs) {
        b.x += b.dx;
        b.y += b.dy;
        if (b.x < -0.2 || b.x > 1.2) b.dx *= -1;
        if (b.y < -0.2 || b.y > 1.2) b.dy *= -1;
        const cx = b.x * w;
        const cy = b.y * h;
        const rad = b.r * Math.max(w, h);
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad);
        g.addColorStop(0, `rgba(${b.hue},${b.a})`);
        g.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = g;
        ctx.fillRect(cx - rad, cy - rad, rad * 2, rad * 2);
      }
      if (running && !reduced) raf = requestAnimationFrame(draw);
    };

    if (reduced) {
      draw();
    } else {
      raf = requestAnimationFrame(draw);
    }

    const onVis = () => {
      running = !document.hidden;
      if (running && !reduced) raf = requestAnimationFrame(draw);
      else cancelAnimationFrame(raf);
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [intensity]);

  return <canvas ref={ref} className="pointer-events-none fixed inset-0 -z-[1] h-full w-full" />;
}
