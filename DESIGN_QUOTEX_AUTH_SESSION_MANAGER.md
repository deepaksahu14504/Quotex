# QuotexAuthSessionManager — Design Report (pre-implementation)

**Branch:** `arena/01a0bee8-quotex` · **At:** `2c44a68`
**Status:** DESIGN ONLY — no code changed in this pass (item 15)

---

## 0. Two corrections to the brief, verified before anything else

**(a) The paths in item 12 do not exist in this repository.**

There is no `src/` directory at all. The equivalents are:

| Brief said | Actually is |
|---|---|
| `src/broker/quotex/client.py` | `backend/app/services/market.py` (`PyQuotexProvider`, ~2000 lines) |
| `src/broker/quotex/adapter.py` | `backend/app/orchestrator.py` (owns provider lifecycle, watchdog, reconnect) |
| `src/broker/quotex/vendor/pyquotex/network/login.py` | `vendor/old-pyquotex/pyquotex/network/login.py` |
| `…/vendor/pyquotex/stable_api.py` | `vendor/old-pyquotex/pyquotex/stable_api.py` |
| `…/vendor/pyquotex/ws/client.py` | `vendor/old-pyquotex/pyquotex/ws/client.py` |
| `…/vendor/pyquotex/ws/channels/ssid.py` | `vendor/old-pyquotex/pyquotex/ws/channels/ssid.py` |

**(b) No browser is drivable here — but the repo does not need one.**

Verified, not assumed:
- no browser binary on `PATH` (`google-chrome`, `chromium`, `firefox` — none)
- `DISPLAY` is unset
- no browser-automation library in any of the three requirements files
  (`playwright`, `selenium`, `pyppeteer`, `DrissionPage` — none present)

**Correction to an earlier draft of this report:** I originally presented this
as "browser-based initial authentication cannot be exercised here", implying
item 2's browser-compatible bootstrap was missing. That was misleading. The
repo already obtains a browser-compatible session **without driving a
browser**, and has done so before this task:

| What the task asked for | What the repo actually does | Where |
| --- | --- | --- |
| Browser-compatible bootstrap | `curl_cffi` session with TLS/JA3 fingerprint impersonation of Firefox 133 | `market.py:603` `_CF_IMPERSONATE = "firefox133"` → `:801` `requests.Session(impersonate=self._CF_IMPERSONATE)` |
| 2FA / emailed PIN | Detects Quotex's own `name="keep_code"` field, calls `_otp_callback`, submits the human-entered code to `/sign-in/modal` | `market.py:808-816`; `orchestrator._otp_callback` broadcasts `otp_required` to the UI and waits 300 s |
| Acquire + persist the session token | Writes `session.json`, then the manager persists it per user | `_seed_session_via_curlcffi`; `QuotexAuthSessionManager.save_session` |

So item 2 is satisfied by the **existing** credential + OTP flow, which the
manager now owns rather than replaces. A drivable headed browser remains
unnecessary for the supported path; see §6 for the one case where it would
still matter.

---

## 1. Current authentication flow (as it actually runs today)

```
User saves credentials in UI  →  POST /api/broker/credentials
        │  encrypted with Fernet (app/security.py:120/126)
        ▼
broker_sessions table:  ssid_enc, cookies_enc, login_at, expires_at
        │
        ▼  Orchestrator._connect_provider()
PyQuotexProvider(email, password, ssid, cookies,
                 sessions_dir = user_data_dir(user_id)/"pyquotex_sessions")
        │
        ▼  market.py:692  _build_client()
Quotex(email=…, password=…, root_path=sessions_dir, on_otp_callback=…)
        │
        ├─ if stored ssid+cookies:  client.set_session(user_agent, cookies, ssid)
        │                            ← THE session-injection point
        ▼
client.connect()  →  start_websocket()  →  send_ssid()
        │
        ├─ broker sends "s_authorization"     → auth_status = AUTHENTICATED
        └─ broker sends "authorization/reject"→ auth_status = FAILED
        │
        ▼  if connect failed AND reason matches _should_seed_after_failure
_seed_session_via_curlcffi()   market.py:740
        curl_cffi: GET sign page → GET /sign-in/modal/ (CSRF _token)
                 → POST /sign-in/ (email+password+remember)
                 → if 'name="keep_code"' in response: call _otp_callback (human)
                    → POST /sign-in/modal (keep_code=1, code)
                 → success = "/trade" in response.url
                 → ssid from window.settings, fallback /api/v1/cabinets/digest
                 → writes sessions_dir/session.json
```

**Failure detection (added in `2c44a68`, the previous task):**
`get_auth_status()` / `is_authenticated()` / `session_expired()` read the
vendor's own `state.auth_status`; `SessionRecoveryStateMachine`
(`backend/app/engine/session_recovery.py`) separates the transport path from the
authentication path, caps auth attempts at 3 with 5s→300s backoff, and gates
trading on a positive confirmation.

## 2. Files involved

| File | Role today |
|---|---|
| `backend/app/services/market.py` | `PyQuotexProvider`: `_build_client` :692, `set_session` injection, `_seed_session_via_curlcffi` :740, auth status :1025/:1062/:1070, `refresh_session` :1078, `get_session_snapshot` :683 |
| `backend/app/orchestrator.py` | provider lifecycle, `_sessions_dir()`, `_persist_broker_session()`, watchdog, `_recover_session()`, OTP callback :1212 |
| `backend/app/session_manager.py` | PostgreSQL persistence: `save_session_token` :161, `get_session_info` :208, `has_restorable_session` :227, `clear_session_token` :243, `SESSION_SOFT_TTL` 20 h :28 |
| `backend/app/engine/session_recovery.py` | state machine (transport vs auth), budget, backoff |
| `backend/app/security.py` | Fernet `encrypt`/`decrypt` :120/:126 |
| `backend/app/config.py:42` | `user_data_dir(user_id)` |
| `vendor/old-pyquotex/pyquotex/config.py` | `load_session` :54 / `update_session` :81 → `session.json` |
| `vendor/old-pyquotex/pyquotex/network/login.py` | vendor's own sign-in flow (used only if the vendor path is taken) |
| `vendor/old-pyquotex/pyquotex/stable_api.py` | `Quotex(...)`, `set_session()`, `connect()` |

## 3. Exact integration point

**`market.py:692` `_build_client()` → `client.set_session(user_agent, cookies, ssid)`.**

This is the single place where a session enters the vendor client. Everything
else (connect, WS, market data, trading) already consumes whatever was injected
there. A `QuotexAuthSessionManager` therefore needs exactly one hook into the
provider: *supply the session tuple that `_build_client` injects*, and *be told
when authentication fails*.

Secondary hook: `_seed_session_via_curlcffi` (`:740`) is the only fresh-login
implementation. Per item 4 ("do not duplicate login logic"), the manager must
**own** this, not wrap a second copy of it.

## 4. Proposed state machine

Reuse the one already shipped in `2c44a68` rather than inventing a second
(item 4, and the standing rule against parallel systems):

```
                    ┌──────────────── transport path ────────────────┐
CONNECTED ──ws drop──► WS_DISCONNECTED ──► RECONNECTING ──► CONNECTED
    │
    │ broker: authorization/reject
    ▼
SESSION_EXPIRED ──► FRESH_SESSION_REQUIRED ──► AUTHENTICATING
                                                    │
                     ┌── broker demands PIN ────────┤
                     ▼                              ▼
              REAUTH_REQUIRED            NEW_SESSION_CREATED
              (trading paused,                    │
               human completes OTP)     broker: s_authorization
                     │                            ▼
                     └────────────────────────► CONNECTED
                                                    │
              3 failed attempts ──► AUTH_EXHAUSTED (trading paused)
```

`QuotexAuthSessionManager` sits **beside** this machine as the *executor* of the
`FRESH_SESSION_REQUIRED` step, not as a second state machine.

## 5. Proposed `QuotexAuthSessionManager` API (item 5)

| Method | Behaviour |
|---|---|
| `load_session(user_id)` | Read persisted state (PostgreSQL first, `session.json` as the vendor's own cache). Returns `None` on corrupt/missing — never raises. |
| `save_session(user_id, ssid, cookies, user_agent)` | Encrypt + persist. File written `0600`, dir `0700`. Never logs values. |
| `validate_session(user_id)` | **Live** check: connect with the stored session and require a positive `auth_status == AUTHENTICATED`. A JSON file existing is explicitly *not* validation. |
| `invalidate_session(user_id)` | Clear stored ssid/cookies (keeps credentials), so the dead session cannot be reused. |
| `create_fresh_session(user_id)` | Run the supported sign-in flow (the existing `_seed_session_via_curlcffi`, moved under the manager's ownership). Surfaces `AUTH_BROWSER_REQUIRED` if the broker demands interactive verification. |
| `reconnect_session(user_id)` | Re-establish the authenticated WS on the current session; only calls `create_fresh_session` on an authentication failure. |

Structured events (item 11): `AUTH_SESSION_LOAD`, `AUTH_SESSION_VALID`,
`AUTH_SESSION_EXPIRED`, `AUTH_FRESH_SESSION_REQUIRED`, `AUTH_BROWSER_REQUIRED`,
`AUTH_SESSION_SAVED`, `AUTH_RECONNECT_SUCCESS`, `AUTH_RECONNECT_FAILED` — via the
existing `log_event()` helper, carrying lengths/booleans only, never values.

## 6. Where a real browser would still matter, and what I propose

Item 2 asks for browser-based login so a human can complete an interactive
challenge legitimately. **For the normal path this repo does not need one:**
the supported credential + emailed-PIN flow already produces a browser-
compatible session headlessly (§1(b)). Nothing below changes that.

A headed browser would matter in exactly one situation: when Quotex serves an
interactive Cloudflare challenge that the credential flow cannot satisfy —
i.e. when the manager has emitted `AUTH_BROWSER_REQUIRED`. Then:

- this environment has no browser binary, no `DISPLAY`, and no automation
  library, so that path cannot be exercised or tested here;
- the app is a headless multi-user server, so "a human completes the challenge
  in a browser" needs a place for that browser to exist.

Three honest options for that remaining case:

1. **Build the manager now with a pluggable browser hook, defaulting to the
   existing HTTP sign-in flow.** `create_fresh_session()` tries the supported
   credential flow; if the response indicates an interactive challenge, it emits
   `AUTH_BROWSER_REQUIRED` and pauses — exactly as item 8's flow ends. A real
   browser bootstrap can be attached later without touching callers. *No new
   dependency, testable here, nothing faked.*
2. **Add Playwright as an optional extra** (`pip install playwright` +
   `playwright install chromium`) and implement a headed bootstrap that pauses
   for the human. This works only where a display exists; in this sandbox it
   cannot be installed or run, so I could not test it.
3. **Both** — option 1 now, option 2 as an opt-in extra that is only exercised
   on a machine with a browser.

I recommend **option 1**, or **3** if you want the browser code written now and
accept that it ships untested here.

## 7. What I will NOT do

- **I added** no Cloudflare bypass, no CAPTCHA solving, no new fingerprint
  spoofing and no challenge-token extraction (items: IMPORTANT, 8).
- To be precise about the existing system rather than only about my diff:
  `market.py:603` already sets `_CF_IMPERSONATE = "firefox133"`, and
  `curl_cffi`'s `impersonate=` is TLS/JA3 fingerprint impersonation. That is
  pre-existing and this task neither introduced nor removed it. What I will not
  do is **extend or tune it as an evasion mechanism** — e.g. automating the
  `vendor/old-pyquotex/scripts/seed_session_via_curlcffi.py:57` suggestion to
  "try a different impersonate value" when Cloudflare blocks. Rotating
  impersonation profiles until a block clears is circumvention, not
  compatibility, and it stays out.
- If the broker demands a challenge that impersonation does not satisfy, the
  manager emits `AUTH_BROWSER_REQUIRED` and stops for a human. It does not
  escalate.
- No second login implementation alongside `_seed_session_via_curlcffi`.
- No change to strategy generation, confidence, risk, signals, candle
  aggregation, or trade-execution rules (item 13).
- No password, cookie, SSID, token or auth header in any log line (items 3, 11).
- No global session shared across users — `user_data_dir(user_id)` isolation
  already exists and will be preserved (items 3, 9).
