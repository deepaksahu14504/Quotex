import { AnimatePresence, motion } from "framer-motion";
import { useStore } from "../store";

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const dismiss = useStore((s) => s.dismissToast);
  const tones: Record<string, string> = {
    info: "ring-[var(--color-accent)]/40 text-white",
    win: "ring-[var(--color-up)]/50 text-[var(--color-up)]",
    loss: "ring-[var(--color-down)]/50 text-[var(--color-down)]",
    warn: "ring-[var(--color-warn)]/50 text-[var(--color-warn)]",
  };
  return (
    <div className="pointer-events-none fixed left-1/2 top-20 z-[60] flex w-full max-w-sm -translate-x-1/2 flex-col gap-2 px-3">
      <AnimatePresence>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            initial={{ opacity: 0, y: -20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -10, scale: 0.95 }}
            onClick={() => dismiss(t.id)}
            className={`glass-strong pointer-events-auto rounded-2xl px-4 py-3 text-sm font-medium ring-1 ${tones[t.kind]}`}
          >
            {t.message}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
