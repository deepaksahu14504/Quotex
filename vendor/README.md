# Vendored `pyquotex` broker library

This directory holds the pyquotex library the backend uses for **real** Quotex
connectivity (`app/services/market.py::PyQuotexProvider`). It is vendored
rather than installed from PyPI because the app patches transport-level
behaviour (`WS_SEND_TIMEOUT` in `pyquotex/ws/client.py`) and pins the API
surface.

## Which copy is actually used

`backend/app/pyquotex_vendor.py` **searches** for the package instead of
hardcoding one path. A directory only counts when it contains
`pyquotex/stable_api.py`.

Resolution order, first match wins:

1. `$PYQUOTEX_VENDOR_PATH` — either the directory that directly contains
   `pyquotex/`, or a `vendor/`-style directory whose subdirectories are then
   searched.
2. `<ancestor>/vendor/` for every ancestor of `backend/app/`, **nearest
   first**, plus `./vendor/`. Inside each: the directory itself first, then
   `pyquotex`, then `old-pyquotex`, then any other subdirectory in sorted
   order.

So with this repo's layout the resolved path is normally:

```
<repo>/vendor/old-pyquotex
```

`vendor/pyquotex/` is checked **first**, and wins the moment it is populated —
but it is *empty* in a fresh clone, because git cannot track an empty
directory. That empty directory used to be the only path the code looked at,
which is why real broker connections failed with
`ModuleNotFoundError: No module named 'pyquotex'` (RCA F1). An empty directory
is now skipped instead of shadowing a working copy.

Inside the Docker image the tree is copied to `/app/vendor` and
`PYQUOTEX_VENDOR_PATH=/app/vendor` is pinned in `backend/Dockerfile`.

## Dependencies are NOT in `requirements.txt`

pyquotex imports `bs4` (and uses `httpx`, `fake-useragent`, `pyfiglet`,
`rich`) at module load. Those live in `backend/requirements-quotex.txt`.
Install **both**:

```bash
cd backend
pip install -r requirements.txt
pip install -r requirements-quotex.txt
```

Without the second one the import fails even though `vendor/` is present.

## Verify it works

```bash
cd backend
python -c "from app.pyquotex_vendor import ensure_pyquotex_on_path as e; \
p = e(); assert p, 'vendored pyquotex NOT FOUND'; \
import pyquotex.stable_api; print('pyquotex OK ->', p)"
```

## sys.path ordering — append, never prepend

The vendor tree ships its own top-level `app.py`, `test.py`, `tests.py` and
`close.py`. Putting the vendor directory at the **front** of `sys.path`
shadows this backend's own `app` package and breaks pytest's `test_*`
collection. `ensure_pyquotex_on_path()` therefore **appends**.
