# Fresh Session Recovery — AUDIT (pre-implementation)

**Branch:** `arena/01a0bee8-quotex` · **Audited at:** `722caf7`
**Status:** INSPECTION ONLY — no code modified in this pass
**Method:** every claim below was read out of this repository's own vendor
(`vendor/old-pyquotex/`) and backend code. Nothing is inferred from upstream
PyQuotex documentation.

---

## A. What currently happens when Session A expires

The vendor **does** have a real authentication state, and this backend
**does not read it**.

On an expired/revoked session the broker sends `authorization/reject`. The
vendor handles it at `pyquotex/api.py:432-441`:

```python
if "authorization/reject" in msg_str:
    self.state.websocket_error_reason = "Websocket connection rejected."
    self.state.auth_status = AuthStatus.FAILED
    await self.event_registry.set_event("auth_changed", self.state.auth_status)
    return
```

Note what is **missing**: it never touches `state.status`. Verified — the only
`state.status =` assignments in `api.py` are `:391` (CONNECTED), `:445`
(CONNECTED), `:810` (ERROR), `:833` (DISCONNECTED, inside `_on_close`), `:1027`
(ERROR), `:1218` (CONNECTING). None is in the reject branch.

Meanwhile `check_connect()` (`api.py:1029`) is:

```python
return self.state.status == WebsocketStatus.CONNECTED
```

— **WebSocket connectivity only, not authentication.**

So an expired session produces two paths, and neither is a real detection:

| Broker behaviour | What the system does |
|---|---|
| Rejects **and closes** the socket | `_on_close` (`api.py:~827`) → `state.status = DISCONNECTED` → `provider.connected` false → `_reconnect_with_backoff` (`orchestrator.py:1101`) → `connect()` → reuses the **same dead ssid** (`state.SSID = session_data["token"]`, `api.py:875`) → `send_ssid()` fires the dead token → rejected again → **loops forever**, 2s→60s backoff, no attempt cap |
| Rejects but **keeps** the socket open | `check_connect()` still returns True, `provider.connected` stays True, **trading continues on an unauthenticated socket** (orders silently fail). The only backstop is `_tick_watcher`'s 45-second no-tick-growth stall detector (`market.py:1180`), which then forces the same reconnect loop |

**Two aggravating facts, both verified:**

1. `send_ssid()` (`api.py:1268`) returns `True` merely because the SSID was
   non-empty and the frame was written. It never confirms the broker accepted
   it — `Ssid.__call__` (`ws/channels/ssid.py`) is fire-and-forget. So
   `connect() → (True, "Connected")` means *"socket up + auth frame sent"*,
   **not** *"authenticated"*.
2. The orchestrator **never checks authentication before trading**. Grep for
   `auth_status|is_authenticated|AuthStatus` in `orchestrator.py` → **zero
   matches**. Trades are gated on `state.connected` / `provider.connected`,
   which as shown above is a WebSocket-level fact.

**The one genuinely correct behaviour that exists today:** at *connect* time,
`market.py:972` checks `_should_seed_after_failure(reason, exc)`, whose marker
list (`market.py:725-738`) includes the exact string the reject handler sets —
`"websocket connection rejected"`. When it matches, `connect()` runs
`_seed_session_via_curlcffi(...)` at `:978`, which is a **real fresh login**.
So a correct recovery path exists — but it is reachable **only from inside
`connect()`**, and only when the failure reason string happens to match.

---

## B. Does the existing implementation have a legitimate refresh/re-auth mechanism?

**Yes — two. Neither is a refresh token.**

1. **Vendor `Login.__call__(username, password, user_data_dir)`** —
   `pyquotex/network/login.py:226`. The full supported sign-in flow:
   `get_sign_page()` (`:31`) → `get_token()` (`:60`, scrapes the CSRF `_token`
   from `/sign-in/modal/`) → `_post(data)` (`:159`) POSTing
   `_token`/`email`/`password`/`remember=1` to `/sign-in/` →
   `success_login()` (`:190`) which declares success only if the response URL
   contains `"trade"` → `get_settings()` (`:133`) scraping `window.settings`
   for the new `token`, plus cookies and user-agent → `update_session()`
   persisting to `session.json`.

2. **This repository's own `_seed_session_via_curlcffi`** —
   `backend/app/services/market.py:740-875`. The *same* sign-in flow, done with
   `curl_cffi` instead of the vendor's browser class, and it is **already
   wired into `connect()` at `:978`**. It also handles OTP (see C).

**There is no refresh-token or silent-renewal endpoint anywhere in the vendor.**
The closest thing is `login.get_profile()` (`network/login.py:117`), which
refreshes `session_data["token"]` from `GET /api/v1/cabinets/digest` — but it
sends `"Cookie": self.api.session_data["cookies"]`, i.e. it **requires an
already-valid session**. It can top up a token on a live session; it cannot
resurrect a dead one.

Also present and unused by the backend: the vendor's own
`EventRegistry` (`pyquotex/utils/async_utils.py:110`, with `set_event` `:128`
and `wait_event(key, timeout)` `:133`) already emits an `auth_changed` event on
every auth transition, and `AuthStatus` (`global_value.py:19`) already has the
four states your lifecycle asks for:

```python
NOT_AUTHENTICATED = 0
AUTHENTICATING    = 1
AUTHENTICATED     = 2
FAILED            = -1
```

---

## C. Can it create a genuinely NEW authenticated session without OTP?

**Not guaranteed — and I will not pretend otherwise.**

Both mechanisms POST credentials to the same `/sign-in/` endpoint. Whether OTP
is demanded afterwards is **the broker's decision, not ours**: Quotex signals it
by returning a page containing `<input name="keep_code">`. Both implementations
already detect exactly that and then ask a human:

- vendor `awaiting_pin()` (`login.py:74`): `self.api.on_otp_callback(...)` if
  set, otherwise `input(input_message)`
- this repo (`market.py:807-839`): `if 'name="keep_code"' in response.text:` →
  requires `self._otp_callback`, prompts the user, validates `.isdigit()`, then
  POSTs `/sign-in/modal` with `keep_code=1` and the code

So the honest answer:

- A genuinely new, fully authenticated Session B **can** be created without OTP
  **if and only if** the broker does not demand a PIN for that login. The flow
  already sends `remember=1` (and `keep_code=1` on the OTP path), which is
  Quotex's own "keep this device" mechanism — that is what makes OTP-free
  re-auth possible at all, and it is the broker's policy that decides.
- **If the broker demands a PIN, there is no supported way to obtain it
  non-interactively.** Per your item 4 I will not attempt to bypass, defeat or
  automate around that. The correct behaviour is to surface
  `OTP_REQUIRED` / `REAUTH_REQUIRED` and pause trading — which this codebase
  already has the plumbing for (`orchestrator._otp_callback` `:1212` sets
  `state.otp_required = True` `:1217` and broadcasts `otp_required` `:1219`;
  `clear_otp_prompt` `:1241`; surfaced at `main.py:169,173,581`).

---

## D. Exactly which files/functions are involved

**Vendor — authentication (`vendor/old-pyquotex/pyquotex/`)**

| Location | Role |
|---|---|
| `network/login.py:226` `Login.__call__` | The supported sign-in flow |
| `network/login.py:31 / :60 / :159 / :190 / :133` | sign page, CSRF token, POST, success check, settings scrape |
| `network/login.py:74` `awaiting_pin` | OTP prompt (`on_otp_callback` / `input()`) |
| `network/login.py:117` `get_profile` | token top-up, **requires live cookies** |
| `network/logout.py:26` | `GET /{lang}/logout` — session invalidation |
| `config.py:54 / :81` | `load_session` / `update_session` → `session.json` |
| `api.py:1275` `connect(is_demo)` | `start_websocket()` then `send_ssid()` |
| `api.py:1268` `send_ssid` | fire-and-forget; **no confirmation** |
| `api.py:1029` `check_connect` | `state.status == CONNECTED` — **WS only** |
| `api.py:417` `_on_message` | auth signal handling |
| `api.py:432-441` | `authorization/reject` → `auth_status = FAILED` (**does not set `state.status`**) |
| `api.py:442-451` | `s_authorization` → `AUTHENTICATED` + `CONNECTED` |
| `api.py:~827` `_on_close` | `state.status = DISCONNECTED` |
| `api.py:875-888` | `state.SSID`, cookies, user-agent from `session_data` |
| `ws/channels/ssid.py` | sends `42["authorization", {...}]` |
| `global_value.py:19` | `AuthStatus` enum |
| `utils/async_utils.py:110/128/133` | `EventRegistry`, `set_event`, `wait_event` |

**Backend — authentication & recovery (`backend/app/`)**

| Location | Role |
|---|---|
| `services/market.py:894` `connect()` | rebuilds client `:945`, redirects `base_dir` `:944`, clears stream state `:964-967`, connects `:968` |
| `services/market.py:725` `_should_seed_after_failure` | marker match, includes `"websocket connection rejected"` |
| `services/market.py:740` `_seed_session_via_curlcffi` | **real fresh login + OTP**, called at `:978` |
| `services/market.py:984-1000` | 5×/1.5s `check_connect()` re-poll (vendor's own ack wait is only ~2s) |
| `services/market.py:1004-1008` | the **only** place `auth_status` is read — a log line |
| `services/market.py:1180` `_tick_watcher` | 45s stall → force reconnect |
| `session_manager.py:28` | `SESSION_SOFT_TTL = 60*60*20` (20 h) |
| `session_manager.py:81` `is_stale` | hard `expires_at`, else `login_at + 20h`, else True |
| `session_manager.py:161 / :208 / :227 / :243` | save token, get info, `has_restorable_session`, `clear_session_token` |
| `orchestrator.py:1101` `_reconnect_with_backoff` | `_reconnecting` guard `:1102`; loop `:1106`; backoff ×2 to 60s `:1155`; reset `:1137` |
| `orchestrator.py:83-84` | `RECONNECT_BASE_DELAY = 2.0`, `RECONNECT_MAX_DELAY = 60.0` |
| `orchestrator.py:1212 / :1241` | `_otp_callback`, `clear_otp_prompt` |
| `schemas.py:343` | `state.otp_required` |

---

## E. Explicit statement on what does and does not exist

- **A supported fresh-authentication mechanism EXISTS** (B). It is
  email+password sign-in, available both in the vendor and already
  implemented in this repo at `market.py:740`.
- **A refresh-token / silent-renewal mechanism DOES NOT EXIST.** I looked; there
  is none. I am not going to invent one or dress up `get_profile()` as one —
  it needs live cookies and therefore cannot recover a dead session.
- **OTP-free re-authentication is NOT guaranteed.** It depends entirely on
  whether Quotex demands a PIN for that login. Where it does, the correct
  behaviour is to surface `OTP_REQUIRED` and pause — not to work around it.
- **I therefore cannot honestly promise 24-hour unattended operation.** What can
  be promised is: real expiry detection, one genuine fresh-auth attempt, correct
  retirement of Session A only after Session B is confirmed, and a safe
  `OTP_REQUIRED` pause when the broker demands a PIN.
- I will **not** write anything that evades Cloudflare antibot, spoofs a
  residential IP, or circumvents the broker's datacenter-IP block. That
  boundary from the earlier task still stands.

---

## Gaps this audit identifies (what implementation would have to fix)

1. **`auth_status` is never consulted.** The vendor already tells us
   `FAILED`; nothing reads it. This is the root cause — everything else follows.
2. **`check_connect()` conflates connectivity with authentication.** Any
   "are we OK to trade?" decision built on it is wrong after a reject.
3. **A rejected session is retried indefinitely.** `_reconnect_with_backoff`
   loops on `not provider.connected` with no attempt cap and no
   session-vs-transport distinction, so a dead session produces an endless
   60-second reconnect loop that re-sends the same dead token.
4. **`_seed_session_via_curlcffi` is only reachable from `connect()`**, and only
   on a reason-string match — so mid-session expiry usually never reaches the
   one code path that could actually fix it.
5. **No auth state machine.** The lifecycle you specified
   (`SESSION_EXPIRED → FRESH_SESSION_REQUIRED → AUTHENTICATING →
   NEW_SESSION_CREATED → CONNECTED`) does not exist in any form; nor does the
   `WS_DISCONNECTED → RECONNECTING → CONNECTED` distinction from it.
6. **Session A is not protected.** Nothing enforces "don't destroy the valid
   session until the new one is confirmed authenticated".
7. **Trading is not gated on authentication**, only on WebSocket connectivity.
