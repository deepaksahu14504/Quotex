"""Lightweight Telegram integration using the Bot HTTP API via httpx.

Outbound: signal alerts + trade results.
Inbound: long-poll getUpdates for commands (/status /pause /resume /stop /enter /exit).

No external Telegram SDK needed; only httpx. Commands are restricted to the
configured chat_id (allowlist).

One `TelegramService` instance is owned by each user's Orchestrator (see
orchestrator.py) — this used to be a module-level singleton, which meant the
last user to start their bot silently took over commands for every other
user's chat. Now each user's token/chat_id/poll loop/command handler are
fully isolated instance state.

Caveat this doesn't (and can't) fix: if two different users configure the
*same* bot token, Telegram's `getUpdates` offset is global to that token, so
two independent pollers will compete for the same update stream. Each user
should use their own bot (free, takes 30 seconds via @BotFather) — this is a
Telegram API constraint, not something fixable at this layer.

--------------------------------------------------------------------------
Bug fixes (2026-08) — why messages "just didn't arrive" with no trace:

  1. `send()` was `try: ... except Exception: pass` AND never looked at the
     HTTP response. Telegram reports almost every real-world failure as a
     200-shaped-or-400 JSON body with `ok: false` ("chat not found",
     "Unauthorized", "bot was blocked by the user", "can't parse entities"),
     none of which raise in httpx. Every one of those was swallowed
     silently. Now: status code + `ok` flag are both checked, the failure is
     logged with Telegram's own `description`, and the last error is kept on
     the instance so the UI/health endpoint can show it.
  2. `parse_mode=HTML` with unescaped text: any `<`, `>` or `&` in an asset
     name or a strategy reason string makes Telegram reject the whole
     message with 400 "can't parse entities". Outgoing text is now escaped
     around the intentional tags, and on a parse failure the message is
     re-sent once as plain text rather than being lost.
  3. `send()` was awaited inline on the scan loop — *before* the risk gate
     and trade execution. A slow or hanging Telegram call therefore delayed
     entry by up to the 10s timeout, which on 1m binaries is material.
     Sending is now fire-and-forget through a bounded queue drained by a
     background task; the trading path returns immediately.
  4. `configure()` never restarted the poller when the token or chat id
     changed, so editing them in Settings left the old values live until a
     full service restart.
  5. 429 rate limits are honoured (`retry_after`) instead of dropping the
     message.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Awaitable, Callable, Optional, Tuple

import httpx

logger = logging.getLogger("qat.telegram")

API = "https://api.telegram.org/bot{token}/{method}"

# Bounded so a long Telegram outage can't grow memory without limit. Oldest
# alerts are dropped first — a stale signal alert is worthless anyway.
_QUEUE_MAX = 200
_TAG_RE = re.compile(r"<[^>]+>")


def escape(text: str) -> str:
    """Escape user/market-derived text for parse_mode=HTML."""
    return html.escape(str(text), quote=False)


class TelegramService:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._chat_id: Optional[str] = None
        self._enabled = False
        self._allow_commands = False
        self._poll_task: Optional[asyncio.Task] = None
        self._sender_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._offset = 0
        self.command_handler: Optional[Callable[[str, list[str]], Awaitable[str]]] = None

        # Observability — surfaced by status() / the /api/telegram/status endpoint.
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.last_sent_at: Optional[float] = None
        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    def configure(self, enabled: bool, token: Optional[str], chat_id: Optional[str],
                  allow_commands: bool) -> None:
        token = (token or "").strip() or None
        chat_id = str(chat_id).strip() if chat_id else None

        creds_changed = (token != self._token) or (chat_id != self._chat_id)
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._allow_commands = bool(allow_commands)

        if enabled and not self._enabled:
            self._note_error("Telegram enabled but bot token and/or chat ID is missing")

        # --- outbound sender ---
        if self._enabled and (self._sender_task is None or self._sender_task.done()):
            self._sender_task = self._spawn(self._sender_loop(), "telegram-sender")
        if not self._enabled and self._sender_task:
            self._sender_task.cancel()
            self._sender_task = None

        # --- inbound poller ---
        want_poller = self._enabled and self._allow_commands
        if self._poll_task and (not want_poller or creds_changed):
            # BUG FIX: a token/chat change used to leave the old poller running
            # against the old credentials until the process restarted.
            self._poll_task.cancel()
            self._poll_task = None
            if creds_changed:
                self._offset = 0
        if want_poller and (self._poll_task is None or self._poll_task.done()):
            self._poll_task = self._spawn(self._poll_loop(), "telegram-poll")

    @staticmethod
    def _spawn(coro, name: str) -> Optional[asyncio.Task]:
        try:
            return asyncio.create_task(coro, name=name)
        except RuntimeError:
            # No running loop (e.g. called from sync startup code) — the next
            # configure() from inside the loop will start it.
            coro.close()
            logger.warning("Telegram %s not started: no running event loop", name)
            return None

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    async def send(self, text: str) -> None:
        """Fire-and-forget enqueue. Never blocks the caller (the scan loop
        calls this immediately before the risk gate and execution)."""
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()          # drop the oldest
                self._queue.task_done()
                self._queue.put_nowait(text)
            except Exception:
                pass
            self.dropped_count += 1
            logger.warning("Telegram queue full — dropped an alert (%d dropped total)",
                           self.dropped_count)

    async def send_now(self, text: str) -> Tuple[bool, Optional[str]]:
        """Send synchronously and report the outcome. Used by the
        'Send test message' button so a misconfiguration is visible
        immediately instead of only in the logs."""
        if not self._token or not self._chat_id:
            return False, "Bot token and chat ID must both be set"
        return await self._deliver(text)

    async def _sender_loop(self) -> None:
        try:
            while True:
                text = await self._queue.get()
                try:
                    await self._deliver(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._note_error(f"{type(exc).__name__}: {exc}")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _deliver(self, text: str, *, _plain_retry: bool = False) -> Tuple[bool, Optional[str]]:
        payload = {
            "chat_id": self._chat_id,
            "text": _TAG_RE.sub("", text) if _plain_retry else text,
            "disable_web_page_preview": True,
        }
        if not _plain_retry:
            payload["parse_mode"] = "HTML"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(API.format(token=self._token, method="sendMessage"),
                                      json=payload)
                try:
                    data = r.json()
                except Exception:
                    data = {}

                if r.status_code == 429:
                    wait = float((data.get("parameters") or {}).get("retry_after", 3))
                    logger.warning("Telegram rate limited — retrying in %.0fs", wait)
                    await asyncio.sleep(min(wait, 60))
                    r = await client.post(API.format(token=self._token, method="sendMessage"),
                                          json=payload)
                    try:
                        data = r.json()
                    except Exception:
                        data = {}

            if data.get("ok"):
                self.sent_count += 1
                self.last_sent_at = time.time()
                return True, None

            desc = str(data.get("description") or f"HTTP {r.status_code}")

            # BUG FIX: an unescaped '<' or '&' anywhere in the text used to
            # kill the whole message silently. Resend as plain text once.
            if not _plain_retry and "parse" in desc.lower():
                logger.warning("Telegram rejected HTML markup (%s) — resending as plain text", desc)
                return await self._deliver(text, _plain_retry=True)

            self.failed_count += 1
            self._note_error(desc)
            return False, desc
        except Exception as exc:
            self.failed_count += 1
            msg = f"{type(exc).__name__}: {exc}"
            self._note_error(msg)
            return False, msg

    def _note_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = time.time()
        logger.warning("Telegram: %s", message)

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #
    def _chat_matches(self, chat: dict) -> bool:
        """Accept a numeric chat id, or an @channelusername (which Telegram
        allows in sendMessage but reports as a username on inbound updates)."""
        if not self._chat_id:
            return False
        if self._chat_id.startswith("@"):
            return str(chat.get("username", "")).lower() == self._chat_id[1:].lower()
        return str(chat.get("id")) == self._chat_id

    async def _poll_loop(self) -> None:
        try:
            while True:
                try:
                    async with httpx.AsyncClient(timeout=40) as client:
                        r = await client.get(
                            API.format(token=self._token, method="getUpdates"),
                            params={"offset": self._offset + 1, "timeout": 30},
                        )
                        data = r.json()
                    if not data.get("ok"):
                        self._note_error(f"getUpdates: {data.get('description') or r.status_code}")
                        await asyncio.sleep(5)
                        continue
                    for upd in data.get("result", []):
                        self._offset = max(self._offset, upd["update_id"])
                        msg = upd.get("message") or upd.get("channel_post")
                        if not msg:
                            continue
                        if not self._chat_matches(msg.get("chat", {})):
                            continue
                        text = (msg.get("text") or "").strip()
                        if text.startswith("/") and self.command_handler:
                            # Strip the @botname suffix Telegram appends in groups.
                            parts = text[1:].split()
                            cmd = parts[0].split("@", 1)[0].lower()
                            reply = await self.command_handler(cmd, parts[1:])
                            if reply:
                                await self.send(reply)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._note_error(f"poll: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Introspection / shutdown
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "has_token": bool(self._token),
            "has_chat_id": bool(self._chat_id),
            "commands_enabled": bool(self._allow_commands),
            "poller_running": bool(self._poll_task and not self._poll_task.done()),
            "sender_running": bool(self._sender_task and not self._sender_task.done()),
            "queued": self._queue.qsize(),
            "sent_count": self.sent_count,
            "failed_count": self.failed_count,
            "dropped_count": self.dropped_count,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }

    def stop(self) -> None:
        """Cancel this instance's background tasks (called from Orchestrator.stop())."""
        for attr in ("_poll_task", "_sender_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                setattr(self, attr, None)
