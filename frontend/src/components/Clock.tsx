import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const IST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/**
 * IST clock synced to the server's UTC clock (which is NTP-synced in any real
 * deployment), so it stays accurate regardless of the local device clock.
 */
export function Clock() {
  const offset = useRef(0); // serverUTCms - Date.now()
  const [now, setNow] = useState(Date.now());
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    let alive = true;
    const sync = async () => {
      try {
        const { utc } = await api.time();
        if (!alive) return;
        offset.current = utc * 1000 - Date.now();
        setSynced(true);
      } catch {
        /* fall back to device clock */
      }
    };
    sync();
    const resync = setInterval(sync, 5 * 60 * 1000);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive = false;
      clearInterval(resync);
      clearInterval(tick);
    };
  }, []);

  const time = IST.format(new Date(now + offset.current));

  return (
    <div className="flex items-center gap-1.5 rounded-xl bg-white/5 px-2.5 py-1.5 ring-1 ring-white/8" title="India Standard Time (server-synced)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-[var(--color-accent)]">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </svg>
      <span className="nums text-[12px] font-semibold leading-none tracking-tight">{time}</span>
      <span className="text-[9px] font-medium text-faint leading-none">
        IST{!synced ? "*" : ""}
      </span>
    </div>
  );
}
