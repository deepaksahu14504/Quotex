#!/usr/bin/env python3
"""Run the FastAPI lifespan directly and print the REAL startup exception.

    cd /path/to/backend && .venv/bin/python diagnose_startup.py

Why this exists
---------------
`status=3/NOTIMPLEMENTED` is not a mystery code: it is
`uvicorn.config.STARTUP_FAILURE = 3`, raised at `uvicorn/main.py:629`
(`if not server.started: sys.exit(STARTUP_FAILURE)`). It means one thing
and only one thing -- **the ASGI lifespan startup raised an exception**.
The application never got as far as serving.

Uvicorn does print that exception, at `uvicorn/lifespan/on.py:97`:

    self.logger.error("Exception in 'lifespan' protocol\\n", exc_info=exc)

on the `uvicorn.error` logger. But `app/main.py:17` calls
`configure_logging()` at IMPORT time, which sets `root.handlers = [handler]`
with the JSON formatter. `uvicorn.error` propagates to root, so the
traceback is still emitted -- as an `"exc_info"` FIELD inside one very
long single-line JSON object. Under journalctl that is trivially missed
or truncated, which is why it reads as "returned to the shell without
showing a useful traceback".

This script bypasses uvicorn and the JSON formatter entirely: plain
logging, plain traceback, one exception, on stderr.

It starts the lifespan and then immediately shuts it down. It does not
serve traffic. It DOES run db.init_db() and therefore Alembic, exactly as
production startup would.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback


def main() -> int:
    # Plain formatter, before app.main can install the JSON one.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    print("=" * 70, file=sys.stderr)
    print("STARTUP DIAGNOSTIC — running the lifespan directly", file=sys.stderr)
    print(f"  cwd            : {os.getcwd()}", file=sys.stderr)
    print(f"  python         : {sys.executable}", file=sys.stderr)
    print(f"  DATABASE_URL   : {'set' if os.environ.get('DATABASE_URL') else 'NOT SET'}",
          file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    try:
        from app.main import app
    except BaseException:
        print("\n--- IMPORT of app.main FAILED (startup never even began) ---",
              file=sys.stderr)
        traceback.print_exc()
        return 2

    # configure_logging() ran at import; take the handlers back so the
    # traceback below is human-readable rather than a JSON blob.
    logging.getLogger().handlers = logging.StreamHandler(sys.stderr),
    logging.getLogger().handlers[0].setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    async def run() -> int:
        ctx = app.router.lifespan_context(app)
        try:
            await ctx.__aenter__()
        except BaseException as exc:
            print("\n" + "=" * 70, file=sys.stderr)
            print("LIFESPAN STARTUP RAISED — this is what uvicorn exits 3 on:",
                  file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            return 3
        print("\n" + "=" * 70, file=sys.stderr)
        print("LIFESPAN STARTUP OK — uvicorn would now be serving.", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        try:
            await ctx.__aexit__(None, None, None)
        except BaseException:
            print("\n--- shutdown raised (startup was still fine) ---", file=sys.stderr)
            traceback.print_exc()
        return 0

    try:
        return asyncio.run(run())
    except BaseException as exc:
        print("\n--- diagnostic itself failed ---", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
