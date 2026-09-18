import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";

export function OtpModal() {
  const state = useStore((s) => s.state);
  const pushToast = useStore((s) => s.pushToast);
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const verifying = !!state?.otp_verifying;
  // Stay open through verification. The dialog used to close the instant the
  // code was handed over -- before Quotex had accepted or rejected anything.
  const open = !!state?.otp_required || verifying;

  useEffect(() => {
    if (!open) {
      setCode("");
      setError(null);
    }
  }, [open]);

  const submit = async () => {
    if (!/^\d{4,8}$/.test(code)) {
      pushToast("warn", "Enter the numeric PIN from your email");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await api.submitOtp(code);
      pushToast("info", "PIN submitted — verifying with Quotex…");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to submit PIN";
      setError(msg);
      pushToast("warn", msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-[70] flex items-center justify-center p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
          <motion.div
            initial={{ y: 30, opacity: 0, scale: 0.97 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 30, opacity: 0 }}
            transition={{ type: "spring", stiffness: 320, damping: 30 }}
            className="glass glass-2 relative z-10 w-full max-w-sm p-6 text-center"
          >
            <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-2xl bg-[var(--color-gold)]/15 text-[var(--color-gold)]">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="10" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
            </div>
            <h2 className="text-lg font-bold">Enter Quotex PIN</h2>
            <p className="mt-1 text-[12.5px] text-dim">{state?.otp_message || "Quotex emailed you a verification code. Enter it to finish connecting."}</p>

            <input
              autoFocus
              inputMode="numeric"
              disabled={verifying}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              placeholder="••••••"
              className="nums mt-4 w-full rounded-xl bg-white/8 px-4 py-3 text-center text-2xl font-bold tracking-[0.4em] outline-none ring-1 ring-white/12 focus:ring-[var(--color-accent)]/60"
            />

            <button
              onClick={submit}
              disabled={submitting || verifying}
              className="mt-4 w-full rounded-2xl bg-[var(--color-accent)]/25 py-3 font-semibold text-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/50 transition active:scale-[0.98] disabled:opacity-50"
            >
              {verifying ? "Verifying with Quotex…" : submitting ? "Submitting…" : "Verify & Connect"}
            </button>
            {error && <p className="mt-2 text-[11.5px] text-down">{error}</p>}
            <p className="mt-3 text-[11px] text-faint">
              Check your email inbox (and spam) for the code. Take your time — the login waits for you.
            </p>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
