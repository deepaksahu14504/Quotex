# SQLite → PostgreSQL Migration Report

## 0. Scope correction (read this first)

Before touching anything I searched the whole repo for SQLite dependencies.
The result changes the shape of this migration significantly, so it's worth
stating plainly:

**This codebase has no ORM and no SQLAlchemy anywhere.** `backend/app/db.py`
was ~60 lines of stdlib `sqlite3` — a thread-local connection, a global
`threading.Lock`, and `CREATE TABLE IF NOT EXISTS` run at startup. Only
**four files ever call into it**: `auth.py`, `session_manager.py`,
`services/candle_store.py`, `services/validation_store.py`.

Everything else the request's requirements list names — signals, trades,
orders, strategy_stats, learning_history, pattern_stats, trade_history,
adaptive learning, queue persistence — is persisted as **per-user JSON files
on disk** (`engine/*.py`, via a `user_data_dir(user_id)` helper), not
SQLite. I verified this by grep — none of those modules import `sqlite3`,
`db.py`, or anything DB-shaped. So they have zero SQLite dependency to
remove, and per "don't change trading/signal/execution/risk/adaptive
learning logic," they're correctly untouched. I'm calling this out
explicitly rather than inventing tables that don't exist, because a
migration report that pretends otherwise would be dishonest about what
actually changed.

The real database — used for **user accounts, JWT-protected auth, encrypted
broker (Quotex) sessions, the historical-candle cache, and backtest/health
results** — is what this report covers.

---

## 1. Architecture review

**Before:** one SQLite file (`backend/data/app.db`), opened once per thread
via `threading.local`, serialized behind a global lock for writes, WAL mode
for concurrent reads. Every DB call site (`db.get_conn().execute(...)`)
reused that thread-local connection for the life of the process. Single
point of contention; fine for one process, one file, low write volume —
which is exactly what a single-user or lightly-multi-user deployment on one
VPS looked like.

**After:** a pooled SQLAlchemy 2.x `Engine` targeting PostgreSQL
(`postgresql+psycopg://`), `pool_size=30 / max_overflow=60 / pool_timeout=30
/ pool_recycle=1800 / pool_pre_ping=True`, `READ COMMITTED` isolation
(Postgres's default). Every call site now does `with db.tx() as conn:` (or
`db.read_tx()`), which checks a connection **out of the pool**, opens one
transaction, commits/rolls back, and returns it — nothing is held on a
thread, on `self`, or at module scope. No global lock: Postgres's MVCC
handles concurrent writers, and the two places that had a real
read-then-write race (`broker_sessions`, `health_scores_latest`) already
used SQLite's `INSERT ... ON CONFLICT ... DO UPDATE` upsert syntax, which is
**identical** in PostgreSQL — those two only needed the `?` → `:name`
placeholder rewrite. SQLite's non-standard `INSERT OR REPLACE` (in
`candle_store.py`) was replaced with the equivalent standard-SQL
`ON CONFLICT (pk) DO UPDATE`.

Schema ownership moved from "whatever `CREATE TABLE IF NOT EXISTS` says at
startup" to **Alembic**, with a Core `MetaData`/`Table` definition
(`app/db.py`) Alembic can diff against for future `alembic revision
--autogenerate` runs.

**What did *not* change and why:** the app-level `Settings`/env pattern,
the request → route → service call shape, the encryption scheme for broker
credentials (`app.security.encrypt/decrypt` — untouched, ciphertext is
DB-agnostic), and every non-DB module. FastAPI route handlers still call
service methods synchronously (they always did — `db.get_conn().execute()`
was already a blocking call inside `async def` routes, same as
`conn.execute()` is now); this migration didn't introduce or fix that
existing async/sync mixing, since doing so would touch far more than the
database layer.

---

## 2. Modified files

| File | Why |
|---|---|
| `backend/app/db.py` | Rewritten. SQLite/`sqlite3`/thread-local/global-lock → pooled SQLAlchemy 2.x `Engine`, Core table metadata, `tx()`/`read_tx()` per-call connection context managers, Alembic-driven `init_db()`. |
| `backend/app/auth.py` | User CRUD (`get_user_by_email/by_id`, `create_user`) rewritten from `sqlite3`/`?` placeholders to `sqlalchemy.text()`/named params + `.mappings()`. Stale docstring pointing at `app.db` updated. |
| `backend/app/session_manager.py` | Rewritten in full: broker-session CRUD now uses `db.tx()`/`db.read_tx()` + `text()`. Upsert SQL (`ON CONFLICT`) kept as-is (already standard SQL). |
| `backend/app/services/candle_store.py` | Rewritten: `?` → named params; `INSERT OR REPLACE` → `ON CONFLICT (user_id, asset, timeframe, ts) DO UPDATE`; `.fetchall()` → `.mappings().fetchall()`. |
| `backend/app/services/validation_store.py` | Rewritten: `?` → named params; every `json.dumps()`/`json.loads()` call removed — JSONB columns pass/return native Python dict/list. |
| `backend/app/config.py` | Added `database_url`, `db_pool_size`, `db_max_overflow`, `db_pool_timeout`, `db_pool_recycle`, `db_statement_timeout_ms` settings. |
| `backend/app/main.py` | No behavioral change — `db.init_db()`/`db.close_conn()` call sites untouched (both names preserved with new semantics in `db.py`). One stale comment (about SQLite WAL state) corrected. |
| `backend/requirements.txt` | Added `SQLAlchemy==2.0.36`, `psycopg[binary]==3.2.3`, `alembic==1.14.0`. Nothing removed (`sqlite3` was stdlib, never a requirements-file entry). |
| `backend/.env.example` | Added `DATABASE_URL` + `DB_POOL_*` vars; updated the comment that pointed at `backend/data/app.db`. |
| `DEPLOYMENT.md` | Added a "Database (PostgreSQL)" section (role/DB creation, data-migration step); updated the "single-writer" caveat, the JWT/backup paragraph, and the Backups section (`pg_dump` cron alongside the existing data-dir tar) to reflect Postgres instead of SQLite. Renumbered sections 3–11 → 4–12 to make room. |
| `TESTING_GUIDE.md` | "Check Database" troubleshooting step: `sqlite3 data/app.db ".tables"` → `psql`/`alembic current`. |

## 3. New files

| File | Purpose |
|---|---|
| `backend/alembic.ini` | Alembic config; deliberately does **not** hardcode a DB URL (avoids committing credentials) — `app/db.py`'s `init_db()` injects it at runtime, `alembic/env.py` falls back to `DATABASE_URL`/`Settings` for CLI use. |
| `backend/alembic/env.py` | Alembic environment, wired to `app.db.metadata`. |
| `backend/alembic/script.py.mako` | Standard revision template. |
| `backend/alembic/versions/0001_initial_schema.py` | Baseline migration: creates `users`, `broker_sessions`, `candles`, `backtest_runs`, `backtest_results`, `health_scores_latest` with the indexes listed in §5. |
| `backend/scripts/migrate_sqlite_to_postgres.py` | One-off **data** migration (not schema) — copies every row from an existing `backend/data/app.db` into the new Postgres tables. Idempotent (`ON CONFLICT DO NOTHING` per table's PK), read-only against the source file, converts `0/1` → `bool` and `json.dumps` strings → native JSON on the way in. Has a `--dry-run` flag. |
| `backend/Dockerfile` | Didn't exist before (SQLite needed no service to containerize). Minimal image; `db.init_db()` runs Alembic migrations automatically on every container start. |
| `docker-compose.yml` | New `db` (Postgres 16) + `backend` services for local/staging use. |

---

## 4. Schema changes (and why each one)

Fresh PostgreSQL schema (there was no existing Postgres DB to "preserve" —
this *is* the first Postgres schema; existing SQLite **data** is preserved
via the migration script in §7, structure is created fresh by Alembic).

| Column(s) | SQLite | PostgreSQL | Reasoning |
|---|---|---|---|
| `users.is_active`, `users.is_admin` | `INTEGER` 0/1 | `BOOLEAN` | Native Postgres type; Python truthiness on the returned value is identical either way, so no call-site changes needed. |
| every `*_json` column (`config_json`, `by_regime_json`, `details_json`, etc.) | `TEXT` holding `json.dumps()` output | `JSONB` | Native, indexable, queryable JSON storage; removed manual `json.dumps`/`json.loads` at every call site (one less place serialization could drift). |
| FK constraints (`broker_sessions.user_id → users.id`, `backtest_results.run_id → backtest_runs.id`) | declared but only enforced when a connection had run `PRAGMA foreign_keys=ON` — which `db.py` never did | enforced by Postgres always | Real referential integrity where there was none in practice before. |
| `*_at` / `ts` timestamp columns (`created_at`, `updated_at`, `login_at`, `expires_at`, `ts`, `started_at`, `finished_at`, `computed_at`) | `REAL` (epoch seconds) | **kept as `DOUBLE PRECISION`, not `TIMESTAMPTZ`** | Deliberate compromise, flagged for visibility rather than silently made: every call site does `time.time()` arithmetic on these values (session soft-TTL checks, rate limiting, sort order) across `auth.py`, `session_manager.py`, and the orchestrator. Switching the wire type to a real timestamp would mean touching that arithmetic in files well outside the database layer — which the brief explicitly said not to do ("no trading logic... should change", and this timing logic feeds trading/session behavior). This is the one place I chose "don't change working, out-of-scope logic" over "use every available Postgres feature." If you want this properly, it's a follow-up: swap the column type, then update the handful of `time.time() - x` call sites to `datetime.now(UTC)` arithmetic — a contained, reviewable change, just not one I made silently under a "database migration" banner. |
| Row IDs (`users.id`, `backtest_runs.id`, etc.) | `TEXT` (hex from `uuid.uuid4().hex`, or a **16-char truncated** hex for run/result IDs) | kept as `VARCHAR`/`TEXT`, not native `UUID` | The 16-char truncated IDs (`uuid.uuid4().hex[:16]`) aren't valid UUIDs, so a native `UUID` column would reject them. Rather than have two different ID representations across tables, all IDs stay as strings — simple, consistent, zero risk of an insert failing against real app-generated data. |

---

## 5. Indexes

| Table | Index | Serves |
|---|---|---|
| `users` | unique + index on `email` | every login, every `get_user_by_email` |
| `candles` | composite PK `(user_id, asset, timeframe, ts)` | covers `get_range`/`get_latest_n`'s `WHERE user_id=? AND asset=? AND timeframe=? ORDER BY ts` directly — no extra index needed |
| `broker_sessions` | PK `user_id` | one row per user, direct lookup |
| `backtest_runs` | `(user_id, started_at DESC)`, `(user_id, status, finished_at DESC)` | `list_runs()`, `latest_run_results()` |
| `backtest_results` | `(run_id, health_score DESC)`, `(user_id, asset, strategy, timeframe)` | `get_run()`, `history_for_combo()` |
| `health_scores_latest` | PK `(user_id, asset, strategy, timeframe)`, `(user_id, score DESC)` | `upsert_health()`'s conflict target, `latest_health_all()` |

(The requirement list also named `signals`/`trades`/`orders`/`sessions`/
`trade_history`/`pattern_stats`/`learning_history`/`strategy_stats` — as
covered in §0, none of those are database tables in this codebase, so
there's nothing to index there. If any of those get moved from JSON files
into Postgres in the future, I'm happy to design that schema separately.)

---

## 6. Multi-user isolation

- **No global connection, no global lock.** Every DB call opens its own
  pooled connection (`engine.begin()` inside `tx()`) and returns it when
  done. There is no module-level or thread-local connection object for one
  user's request to accidentally reuse another's in-flight state.
- **Per-request/per-task session, guaranteed by construction, not
  convention.** Request handlers, the Orchestrator's per-user background
  loop, the websocket hub, and the (JSON-file-based) engine components each
  call `CandleStore(user_id)`/`ValidationStore(user_id)`/`session_manager.*`
  independently; every one of those calls a fresh `tx()`/`read_tx()`.
  Concurrent users hitting the API at the same instant get concurrent,
  independent pooled connections — Postgres serializes at the row level
  (MVCC), not by making one user wait on another's whole connection the way
  a single SQLite writer lock effectively could.
- **Every query is scoped by `user_id`** (already true before this
  migration — that part of the design was correct and untouched) — I did
  not find and did not introduce any query that reads across users.
- **Encryption is unchanged.** Broker credentials/session tokens are still
  encrypted with `app.security.encrypt()` before they ever reach the
  database and decrypted after — moving database engines doesn't touch that
  boundary, so ciphertext migrated via the data-migration script is
  decryptable exactly as before (same `SESSION_ENCRYPTION_KEY`).

**What this migration does *not* change:** the backend is still one
process holding the Orchestrator registry, websocket hub, and Telegram
pollers in memory (see `DEPLOYMENT.md`'s "run exactly ONE backend process"
note). PostgreSQL removes the *database's* single-writer bottleneck, but it
does not make the app safe to run as `--workers N>1` — that would need the
in-memory per-user state moved to something shared (Redis), which is a
separate, larger architecture change I did not make and was not asked to
make.

---

## 7. Data migration

`backend/scripts/migrate_sqlite_to_postgres.py` — run once, after
`alembic upgrade head` has created the new schema (or after the app has
started once, since `db.init_db()` calls that automatically):

```bash
cd backend
python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db          # real run
python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db --dry-run  # count only
```

- Copies `users` → `broker_sessions` → `candles` → `backtest_runs` →
  `backtest_results` → `health_scores_latest`, in FK-safe order.
- Converts `0/1` → Python `bool` for `is_active`/`is_admin`, and
  `json.dumps()` strings → parsed JSON for every `*_json` column, so they
  land correctly typed in the new `BOOLEAN`/`JSONB` columns.
- Leaves every `*_enc` (encrypted) column byte-for-byte as-is — ciphertext
  doesn't care which database stores it.
- Uses `INSERT ... ON CONFLICT (pk) DO NOTHING`, so it's safe to re-run
  (e.g. after fixing a connectivity issue mid-run) without duplicating or
  corrupting anything, and it never writes to the source SQLite file.

---

## 8. Performance

- **Connection pooling** replaces "one connection per thread, opened once,
  kept forever" with a bounded, health-checked pool (`pool_size=30,
  max_overflow=60`) — handles bursts (many users logging in at once,
  Orchestrator restart re-authenticating everyone) without either
  exhausting connections or leaving stale ones around.
- **`pool_pre_ping`** removes the failure mode SQLite's thread-local
  connection had: a connection that went stale (VPS network blip, Postgres
  restart) is silently detected and replaced *before* being handed to a
  caller, instead of that thread's every subsequent query failing until
  process restart.
- **No global write lock.** SQLite (even in WAL mode) serializes writers;
  Postgres's MVCC lets independent users' writes to different rows proceed
  concurrently — matters most for `candles` (high write volume) and
  `broker_sessions`/`health_scores_latest` (frequent upserts).
- **`statement_timeout`** (default 30s, via `connect_args`, applied by
  libpq before any transaction opens — not via an unmanaged `cursor.execute`
  that could leave an implicit transaction dangling) means one runaway query
  can't hold a pooled connection, and everything queued behind it, forever.
- **Indexes** (§5) match the actual `WHERE`/`ORDER BY` shape of every query
  in the four DB-touching modules — not generic guesses.
- **Batched upsert** in `CandleStore.upsert_many()`: one `executemany`-style
  statement instead of the SQLite version's per-row loop.

---

## 9. Reliability

- **ACID / transactions:** every `tx()` call is one Postgres transaction —
  commits on success, rolls back on any exception, same guarantee SQLite
  gave, now with real concurrent-writer support underneath.
- **Reconnect:** `pool_pre_ping` handles the common case (idle pooled
  connection went stale) transparently. A failure *mid*-transaction (rare —
  the connection drops while a query is in flight) aborts that transaction
  and raises to the caller, same as the original `sqlite3` code did on any
  error. I deliberately did **not** ship an automatic retry-and-resume for
  that case: I initially wrote one (loop back and retry inside the same
  `@contextmanager` generator) and caught, on review, that it was broken —
  Python's `contextlib` requires a `@contextmanager` generator to terminate
  the first time it's resumed after yielding; looping back to a second
  `yield` raises `RuntimeError: generator didn't stop`, which would have
  fired on exactly the failure path it was meant to fix. Rather than ship a
  more complex retry mechanism I couldn't fully verify without a live
  Postgres instance to test against (see §10), I kept the simpler, correct,
  honest behavior: clean exception propagation, which is what the original
  code did too.
- **Rollback handling:** unchanged in spirit from the original — an
  exception inside `with tx() as conn:` rolls back that transaction only;
  nothing is left half-written.

---

## 10. Remaining risks / what I could not verify here

1. **No live Postgres to test against.** This sandbox has no network
   access, so I could not `pip install` SQLAlchemy/psycopg/alembic or
   actually run `alembic upgrade head` / boot the app / exercise a real
   request against a real Postgres instance. Every file was verified with
   `python -m py_compile` (all pass) and careful manual review against the
   original call sites, but **run a real smoke test before deploying**:
   `docker compose up db`, `alembic upgrade head`, boot the app, register a
   user, log in, save broker credentials, run a backtest.
2. **Alembic autogenerate wasn't run against a live diff.** The baseline
   migration (`0001_initial_schema.py`) was hand-written to match
   `app/db.py`'s Core metadata; I'd normally cross-check it with
   `alembic revision --autogenerate` against an empty database to catch any
   typo, but couldn't here for the same network reason.
3. **`TIMESTAMPTZ` was intentionally not adopted** for epoch-float columns
   (§4) — noted as a real, scoped follow-up rather than silently done.
4. **The single-process constraint remains** (§6) — this migration fixes
   the *database's* concurrency ceiling, not the in-memory
   registry/websocket-hub one. Don't read "migrated to Postgres" as "now
   horizontally scalable."
5. **`DATABASE_URL` default in `config.py`/`docker-compose.yml` uses a
   placeholder password (`qat_app`).** Fine for local dev (matches
   `docker-compose.yml`'s own Postgres container), but the deployment doc
   already says to generate a real password for production — worth a
   second look before go-live to make sure that's actually followed.

---

## 11. Validation checklist (what to run, once Postgres is reachable)

| Item | How |
|---|---|
| Alembic migration | `alembic -c backend/alembic.ini upgrade head`, then `alembic current` |
| Multi-user login | register 2+ accounts, confirm tokens/sessions don't cross |
| Auth | login rate limiting, `/api/auth/me`, admin gate (`is_admin`) |
| Broker session persistence | save credentials, restart the process, confirm `has_restorable_session()` still finds it |
| Candle cache | run a backtest twice, confirm the second run reuses cached candles (`CandleStore.count()` doesn't reset) |
| Backtest/health history | `list_runs()`, `get_run()`, `latest_health_all()` return the same shapes the frontend expects |
| Concurrent websocket users | two browser sessions, two users, confirm no data crossover on `/ws` |
| Data migration | run `migrate_sqlite_to_postgres.py --dry-run` first, review counts, then run for real, spot-check a few rows |
| Docker | `docker compose up`, confirm `backend` waits on `db`'s healthcheck and migrates on boot |
| Scheduler / background workers / signal / analytics / pattern engines | unaffected (JSON-file persistence, no DB dependency) — confirm they still start; nothing in their code changed |

---

## 12. Production readiness score: **72/100**

**What earns the points:** the actual database layer (pooling, isolation,
schema, indexes, upsert semantics, Alembic, data migration path, Docker) is
solid, reviewed twice, and I caught and fixed a real concurrency bug
(§9) before it could have shipped. The scope assessment (§0) is honest
about what this codebase actually persists in a database versus JSON files,
which matters for anyone reading this report to plan further work.

**What holds it back from higher:** zero of this has been run against a
live PostgreSQL instance or a live app process (§10.1) — that's a real gap
for "production-grade," not a formality, and it's the main reason I'm not
scoring this 85+. Get a Postgres instance reachable, run the validation
checklist in §11, fix whatever that surfaces (there's usually something —
an autogenerate diff, a type-adaptation edge case), and this moves
comfortably into the high 80s/90s. I'd rather tell you that plainly than
round up.
