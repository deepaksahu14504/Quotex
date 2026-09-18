import { motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";

export function GlassCard({
  children,
  className,
  delay = 0,
  strong = false,
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
  strong?: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.55, delay, ease: [0.22, 1, 0.36, 1] }}
      className={clsx("glass p-5", strong && "glass-2", className)}
    >
      {children}
    </motion.div>
  );
}

export function SectionTitle({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="mb-4 flex items-center justify-between">
      <h2 className="text-[15px] font-semibold tracking-tight text-white/90">{children}</h2>
      {right}
    </div>
  );
}

export function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={clsx(
        "relative h-[26px] w-[46px] shrink-0 rounded-full transition-colors duration-300",
        checked ? "bg-[var(--color-accent)]" : "bg-white/12"
      )}
      style={checked ? { boxShadow: "0 0 16px -2px var(--color-accent)" } : undefined}
    >
      <motion.span
        layout
        transition={{ type: "spring", stiffness: 520, damping: 34 }}
        className="absolute top-[3px] h-5 w-5 rounded-full bg-white shadow-md"
        style={{ left: checked ? 23 : 3 }}
      />
    </button>
  );
}

export function Pill({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "up" | "down" | "warn" | "accent" | "gold";
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "bg-white/6 text-white/65 border-white/10",
    up: "bg-[var(--color-up)]/12 text-[var(--color-up)] border-[var(--color-up)]/25",
    down: "bg-[var(--color-down)]/12 text-[var(--color-down)] border-[var(--color-down)]/25",
    warn: "bg-[var(--color-gold)]/12 text-[var(--color-gold)] border-[var(--color-gold)]/25",
    accent: "bg-[var(--color-accent)]/12 text-[var(--color-accent)] border-[var(--color-accent)]/25",
    gold: "bg-[var(--color-gold)]/12 text-[var(--color-gold)] border-[var(--color-gold)]/25",
  };
  return (
    <span className={clsx("nums inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold", tones[tone], className)}>
      {children}
    </span>
  );
}

export function AnimatedNumber({ value, decimals = 2, prefix = "" }: { value: number; decimals?: number; prefix?: string }) {
  const [display, setDisplay] = useState(value);
  const ref = useRef(value);
  useEffect(() => {
    const from = ref.current;
    const to = value;
    const start = performance.now();
    const dur = 600;
    let raf = 0;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(from + (to - from) * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
      else ref.current = to;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return (
    <span className="nums">
      {prefix}
      {display.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}
    </span>
  );
}

export function Metric({
  label,
  value,
  tone = "default",
  sub,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "default" | "up" | "down" | "accent";
  sub?: React.ReactNode;
}) {
  const color = tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "accent" ? "text-[var(--color-accent)]" : "text-white";
  return (
    <div className="tile p-3">
      <div className="label">{label}</div>
      <div className={clsx("nums mt-1.5 text-[19px] font-semibold tracking-tight", color)}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-faint">{sub}</div>}
    </div>
  );
}

export function ConfidenceRing({ value, size = 54 }: { value: number; size?: number }) {
  const r = (size - 7) / 2;
  const c = 2 * Math.PI * r;
  const color = value >= 80 ? "var(--color-up)" : value >= 68 ? "var(--color-accent)" : "var(--color-gold)";
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(255,255,255,0.08)" strokeWidth={4} fill="none" />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          stroke={color}
          strokeWidth={4}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={c}
          initial={{ strokeDashoffset: c }}
          animate={{ strokeDashoffset: c - (value / 100) * c }}
          transition={{ duration: 0.9, ease: "easeOut" }}
          style={{ filter: `drop-shadow(0 0 5px ${color})` }}
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center">
        <span className="nums text-sm font-bold" style={{ color }}>{value}</span>
      </div>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("skeleton", className)} />;
}
