/**
 * Named accent presets for RuntimeSettings.ux.accent.
 *
 * The backend stores a preset *name* (e.g. "cyan"), not a raw hex value --
 * matches the two-tone --color-accent / --color-accent-2 gradient system
 * already defined in index.css's @theme block, so every preset is a
 * matched pair rather than a single arbitrary color that could clash with
 * the existing up/down/gold palette.
 */
export interface AccentPreset {
  label: string;
  accent: string;
  accent2: string;
}

export const ACCENT_PRESETS: Record<string, AccentPreset> = {
  cyan:   { label: "Cyan",   accent: "#4ad6ff", accent2: "#8a7bff" }, // default -- matches index.css's built-in @theme values exactly
  violet: { label: "Violet", accent: "#8a7bff", accent2: "#c084fc" },
  green:  { label: "Green",  accent: "#2bdca0", accent2: "#4ad6ff" },
  amber:  { label: "Amber",  accent: "#ffce5c", accent2: "#ff9d5c" },
  rose:   { label: "Rose",   accent: "#ff6b9d", accent2: "#ff9d5c" },
};

export const DEFAULT_ACCENT = "cyan";

/** Applies a preset's two CSS custom properties to the document root.
 * Falls back to the default (cyan) for any unrecognized/legacy value
 * rather than leaving stale or invalid CSS in place. */
export function applyAccent(name: string | undefined | null): void {
  const preset = (name && ACCENT_PRESETS[name]) || ACCENT_PRESETS[DEFAULT_ACCENT];
  const root = document.documentElement;
  root.style.setProperty("--color-accent", preset.accent);
  root.style.setProperty("--color-accent-2", preset.accent2);
}
