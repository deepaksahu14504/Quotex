import { motion } from "framer-motion";

const UP = "var(--color-up)";
const DOWN = "var(--color-down)";
const ACCENT = "var(--color-accent)";

export function BarRow({
  label,
  value,
  max,
  display,
  tone = "accent",
  sub,
}: {
  label: string;
  value: number;
  max: number;
  display: string;
  tone?: "accent" | "up" | "down" | "auto";
  sub?: string;
}) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  const color = tone === "up" ? UP : tone === "down" ? DOWN : tone === "auto" ? (value >= 50 ? UP : DOWN) : ACCENT;
  return (
    <div className="py-1.5">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="truncate text-[13px] text-white/80">{label}</span>
        <span className="nums shrink-0 text-[13px] font-semibold" style={{ color }}>{display}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-white/6">
        <motion.div
          className="h-full rounded-full"
          style={{ background: `linear-gradient(90deg, ${color}66, ${color})` }}
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.8, ease: [0.22, 1, 0.36, 1] }}
        />
      </div>
      {sub && <div className="mt-0.5 text-[10.5px] text-faint">{sub}</div>}
    </div>
  );
}

export function Donut({ wins, losses, size = 132 }: { wins: number; losses: number; size?: number }) {
  const total = wins + losses || 1;
  const r = (size - 18) / 2;
  const c = 2 * Math.PI * r;
  const winFrac = wins / total;
  const rate = Math.round(winFrac * 100);
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke={DOWN} strokeWidth={10} fill="none" opacity={0.5} />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          stroke={UP}
          strokeWidth={10}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={c}
          initial={{ strokeDashoffset: c }}
          animate={{ strokeDashoffset: c - winFrac * c }}
          transition={{ duration: 1, ease: "easeOut" }}
          style={{ filter: `drop-shadow(0 0 6px ${UP})` }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="nums text-2xl font-bold tracking-tight">{rate}%</span>
        <span className="label mt-0.5">win rate</span>
      </div>
    </div>
  );
}

export function EquityArea({ data, height = 120 }: { data: { equity: number }[]; height?: number }) {
  if (data.length < 2) return <div className="skeleton" style={{ height }} />;
  const vals = data.map((d) => d.equity);
  const min = Math.min(...vals, 0);
  const max = Math.max(...vals, 0);
  const range = max - min || 1;
  const w = 600;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * w},${height - ((v - min) / range) * (height - 8) - 4}`);
  const last = vals[vals.length - 1];
  const color = last >= 0 ? UP : DOWN;
  const zeroY = height - ((0 - min) / range) * (height - 8) - 4;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="w-full" style={{ height }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <line x1="0" y1={zeroY} x2={w} y2={zeroY} stroke="rgba(255,255,255,0.12)" strokeDasharray="4 6" strokeWidth="1" />
      <motion.polyline
        points={pts.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth="2.5"
        strokeLinejoin="round"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 1.1, ease: "easeOut" }}
      />
      <polygon points={`0,${height} ${pts.join(" ")} ${w},${height}`} fill="url(#eqfill)" />
    </svg>
  );
}

export function HourHeatmap({
  data,
  selected,
  onSelect,
}: {
  data: { hour: number; trades: number; win_rate: number }[];
  selected?: number | null;
  onSelect?: (hour: number) => void;
}) {
  return (
    <div className="grid grid-cols-12 gap-1.5">
      {data.map((h) => {
        const has = h.trades > 0;
        const intensity = has ? 0.2 + (h.win_rate / 100) * 0.8 : 0;
        const color = h.win_rate >= 50 ? UP : DOWN;
        const isSel = selected === h.hour;
        return (
          <button
            key={h.hour}
            type="button"
            onClick={() => onSelect?.(h.hour)}
            className="flex flex-col items-center gap-1 outline-none"
            title={`${h.hour}:00 · ${h.trades} trades · ${h.win_rate}%`}
          >
            <div
              className="aspect-square w-full rounded-md transition-transform active:scale-90"
              style={{
                background: has ? color : "rgba(255,255,255,0.05)",
                opacity: has ? intensity : 1,
                boxShadow: isSel ? "0 0 0 2px var(--color-accent)" : has && h.win_rate >= 60 ? `0 0 8px -2px ${color}` : undefined,
                cursor: "pointer",
              }}
            />
            {h.hour % 3 === 0 && <span className="nums text-[8px] text-faint">{h.hour}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function FormDots({ form }: { form: string[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {form.map((r, i) => (
        <motion.span
          key={i}
          initial={{ scale: 0 }}
          animate={{ scale: 1 }}
          transition={{ delay: i * 0.02 }}
          className="grid h-5 w-5 place-items-center rounded-md text-[10px] font-bold"
          style={{
            background: r === "win" ? `${UP}22` : `${DOWN}22`,
            color: r === "win" ? UP : DOWN,
          }}
        >
          {r === "win" ? "W" : "L"}
        </motion.span>
      ))}
    </div>
  );
}
