"""Locate the vendored `pyquotex` broker library and put it on `sys.path`.

WHY THIS EXISTS
---------------
`PyQuotexProvider.connect()` used to hardcode one path:

    vendor = Path(__file__).resolve().parents[3] / "vendor" / "pyquotex"
    sys.path.insert(0, str(vendor))
    from pyquotex.stable_api import Quotex

That broke in two independent ways (RCA F1 / F2):

  * The `vendor/pyquotex/` directory is EMPTY in the repository -- git cannot
    track an empty directory, so a fresh clone has nothing there and the
    import raised `ModuleNotFoundError`, silently disabling every real broker
    connection. The only copy of the library actually committed lives at
    `vendor/old-pyquotex/pyquotex/`.
  * Inside the Docker image the module lives at `/app/backend/app/services/
    market.py`, so `parents[3]` resolves to `/app` and the computed path
    became `/app/vendor/pyquotex` -- empty again, and outside the
    `backend/`-scoped build context in any case.

The fix is to *search* for the package instead of assuming where it is, and to
let an environment variable override the result for container layouts.

RESOLUTION ORDER (first match wins)
-----------------------------------
  1. `$PYQUOTEX_VENDOR_PATH` -- either the directory that directly contains
     `pyquotex/`, or a `vendor/`-style directory whose subdirectories are
     searched.
  2. `<ancestor>/vendor/` for each ancestor of this file, nearest first, and
     `./vendor/`. Within each, the directory itself is tried first, then the
     subdirectories named in `_PREFERRED_SUBDIRS` (`pyquotex`, then
     `old-pyquotex`), then any remaining subdirectory in sorted order.

A directory only counts as a match when it actually contains
`pyquotex/stable_api.py` -- an empty `vendor/pyquotex/` is skipped rather than
shadowing a working copy further down the list.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

#: Marker file that proves a directory really is the pyquotex package parent.
_MARKER = ("pyquotex", "stable_api.py")

#: Subdirectories of a `vendor/` root to try FIRST, in this order.
#:
#: A plain alphabetical scan of the vendor root would visit `old-pyquotex`
#: before `pyquotex` ("o" < "p"), so a newer copy dropped into `vendor/pyquotex`
#: would be permanently shadowed by the older one -- the opposite of the
#: documented intent. Naming the preference explicitly fixes that, and any
#: other subdirectory still gets tried afterwards in sorted order.
_PREFERRED_SUBDIRS = ("pyquotex", "old-pyquotex")

_ENV_VAR = "PYQUOTEX_VENDOR_PATH"


def _candidate_roots() -> List[Path]:
    """Directories that might be a `vendor/` root, in preference order."""
    roots: List[Path] = []
    env = os.environ.get(_ENV_VAR)
    if env:
        roots.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    for parent in here.parents:
        roots.append(parent / "vendor")
    roots.append(Path.cwd() / "vendor")
    # De-duplicate while preserving order.
    seen = set()
    out: List[Path] = []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _is_package_parent(path: Path) -> bool:
    try:
        return path.joinpath(*_MARKER).is_file()
    except OSError:
        return False


def select_from_roots(roots) -> Optional[Path]:
    """Pure search over an explicit list of `vendor/`-style roots.

    Split out from `find_pyquotex_path()` so the ordering rules can be unit
    tested against synthetic trees: `find_pyquotex_path()` anchors its ancestor
    walk at *this module's* file path, which a test cannot relocate.

    For each root, in order: the root itself is tried first (so a root that
    directly contains `pyquotex/` wins), then the preferred subdirectories,
    then the rest in sorted order. A directory only matches when
    `pyquotex/stable_api.py` is a real file -- an empty `vendor/pyquotex/` is
    skipped rather than shadowing a working copy later in the list.
    """
    for root in roots:
        if _is_package_parent(root):
            return root
        try:
            if not root.is_dir():
                continue
            present = {p.name for p in root.iterdir() if p.is_dir()}
        except OSError:
            continue
        ordered = [n for n in _PREFERRED_SUBDIRS if n in present]
        ordered += sorted(n for n in present if n not in _PREFERRED_SUBDIRS)
        for name in ordered:
            child = root / name
            if _is_package_parent(child):
                return child
    return None


def find_pyquotex_path() -> Optional[Path]:
    """Return the directory to put on `sys.path`, or None if not found."""
    return select_from_roots(_candidate_roots())


def ensure_pyquotex_on_path() -> Optional[Path]:
    """Idempotently add the vendored pyquotex to `sys.path`.

    Returns the directory that was added (or that was already present), or
    None when the library could not be located. Never raises -- callers decide
    how to report a missing broker library, and the import immediately after
    this call is what produces the real error message.

    IMPORTANT: the entry is APPENDED, never inserted at the front. The vendor
    tree ships its own top-level `app.py`, `test.py`, `tests.py` and
    `close.py`; putting that directory first on `sys.path` would shadow this
    backend's own `app` package (and pytest's `test_*` collection). Appending
    still resolves `import pyquotex` correctly, because nothing else provides
    a module of that name -- and the existing test modules already append for
    exactly this reason (see the ordering note in `test_pipeline_progress.py`).
    """
    path = find_pyquotex_path()
    if path is None:
        return None
    entry = str(path)
    if entry not in sys.path:
        sys.path.append(entry)
        logger.info("pyquotex vendor resolved to %s (appended to sys.path)", entry)
    return path
