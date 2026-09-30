/**
 * Chart colours, resolved from the app's own CSS custom properties.
 *
 * The dashboard already defines its palette in index.css (`--color-up`,
 * `--color-down`, `--color-accent`, ...) and components/charts.tsx already
 * consumes them as `var(...)`. The previous Chart.tsx instead hardcoded
 * #22e0a1 / #ff5d73 / #7c5cff, which drifted from those tokens -- the chart's
 * green did not match the green used everywhere else on the same screen.
 *
 * This resolves the tokens at runtime so the canvas uses the same palette as
 * the DOM. Resolution is necessary rather than cosmetic: lightweight-charts
 * draws on a canvas, which cannot interpret `var(--color-up)` -- only the DOM
 * can. So the variables are read through getComputedStyle and cached.
 */

export interface ChartTheme {
  up: string;
  down: string;
  accent: string;
  accent2: string;
  gold: string;
  ink: string;
  inkDim: string;
  inkFaint: string;
  hair: string;
  background: string;
}

/** Used when a token cannot be read (server rendering, detached element). */
const FALLBACK: ChartTheme = {
  up: "#2bdca0",
  down: "#ff5a76",
  accent: "#4ad6ff",
  accent2: "#8a7bff",
  gold: "#ffce5c",
  ink: "#eaf0ff",
  inkDim: "#9aa6c4",
  inkFaint: "#5d6788",
  hair: "rgba(255, 255, 255, 0.09)",
  background: "transparent",
};

const TOKEN_MAP: Record<keyof ChartTheme, string | null> = {
  up: "--color-up",
  down: "--color-down",
  accent: "--color-accent",
  accent2: "--color-accent-2",
  gold: "--color-gold",
  ink: "--color-ink",
  inkDim: "--color-ink-dim",
  inkFaint: "--color-ink-faint",
  hair: "--color-hair",
  background: null, // transparent: the card behind it shows through
};

let cached: ChartTheme | null = null;

/**
 * Read the palette from the document. Cached, because this runs on every
 * chart creation and the tokens do not change during a session.
 */
export function getChartTheme(): ChartTheme {
  if (cached) return cached;

  if (typeof window === "undefined" || typeof document === "undefined") {
    return FALLBACK;
  }

  let styles: CSSStyleDeclaration;
  try {
    styles = getComputedStyle(document.documentElement);
  } catch {
    return FALLBACK;
  }

  const theme = { ...FALLBACK };
  for (const [key, token] of Object.entries(TOKEN_MAP) as [keyof ChartTheme, string | null][]) {
    if (!token) continue;
    const value = styles.getPropertyValue(token).trim();
    // An empty string means the token is not defined; keep the fallback
    // rather than setting a colour the canvas would reject.
    if (value) theme[key] = value;
  }

  cached = theme;
  return theme;
}

/** Clear the cache. Used by tests, and if the theme ever becomes switchable. */
export function resetChartThemeCache(): void {
  cached = null;
}

/**
 * Indicator line colours.
 *
 * Each indicator keeps a stable colour across timeframe and asset changes, so
 * a line does not silently change meaning while the user is watching it.
 */
export function indicatorColor(key: "ema9" | "ema21" | "ema50" | "boll"): string {
  const t = getChartTheme();
  switch (key) {
    case "ema9":
      return t.accent;
    case "ema21":
      return t.gold;
    case "ema50":
      return t.accent2;
    case "boll":
      return t.inkDim;
  }
}
