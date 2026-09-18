import { motion } from "framer-motion";
import { useState } from "react";
import { useStore } from "../store";

export function Login() {
  const login = useStore((s) => s.login);
  const register = useStore((s) => s.register);
  const authBusy = useStore((s) => s.authBusy);
  const authError = useStore((s) => s.authError);

  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLocalError(null);
    if (mode === "register" && password.length < 8) {
      setLocalError("Password must be at least 8 characters");
      return;
    }
    try {
      if (mode === "login") await login(email.trim(), password);
      else await register(email.trim(), password);
    } catch {
      /* surfaced via authError */
    }
  }

  const error = localError || authError;

  return (
    <div className="relative flex min-h-dvh items-center justify-center px-4">
      <div className="bg-scene" />
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
        className="glass glass-2 relative w-full max-w-sm !rounded-2xl p-6"
      >
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-12 w-12 place-items-center rounded-2xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] text-xl font-black text-black shadow-lg">
            Q
          </div>
          <div>
            <div className="text-[16px] font-bold tracking-tight">QuotexAutoTrader</div>
            <div className="text-[12px] text-faint">
              {mode === "login" ? "Sign in to your account" : "Create your account"}
            </div>
          </div>
        </div>

        <form onSubmit={submit} className="flex flex-col gap-3">
          <div>
            <label className="mb-1 block text-[11px] font-medium text-dim">Email</label>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-xl bg-white/5 px-3.5 py-2.5 text-[14px] text-white ring-1 ring-white/10 outline-none transition focus:ring-[var(--color-accent)]"
              placeholder="you@example.com"
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] font-medium text-dim">Password</label>
            <input
              type="password"
              required
              minLength={mode === "register" ? 8 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-xl bg-white/5 px-3.5 py-2.5 text-[14px] text-white ring-1 ring-white/10 outline-none transition focus:ring-[var(--color-accent)]"
              placeholder={mode === "register" ? "At least 8 characters" : "••••••••"}
            />
          </div>

          {error && (
            <div className="rounded-xl bg-[var(--color-down)]/10 px-3 py-2 text-[12px] text-[var(--color-down)] ring-1 ring-[var(--color-down)]/20">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={authBusy}
            className="mt-1 rounded-xl bg-gradient-to-br from-[var(--color-accent)] to-[var(--color-accent-2)] px-4 py-2.5 text-[14px] font-bold text-black transition active:scale-[0.98] disabled:opacity-60"
          >
            {authBusy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        <div className="mt-4 text-center text-[12px] text-faint">
          {mode === "login" ? (
            <>
              No account yet?{" "}
              <button className="font-semibold text-white/80 underline underline-offset-2" onClick={() => { setMode("register"); setLocalError(null); }}>
                Register
              </button>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <button className="font-semibold text-white/80 underline underline-offset-2" onClick={() => { setMode("login"); setLocalError(null); }}>
                Sign in
              </button>
            </>
          )}
        </div>
      </motion.div>
    </div>
  );
}
