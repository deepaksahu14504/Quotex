"""Regression tests for RCA F1 / F2 — vendored pyquotex resolution.

Before the fix, `PyQuotexProvider.connect()` hardcoded

    vendor = Path(__file__).resolve().parents[3] / "vendor" / "pyquotex"
    sys.path.insert(0, str(vendor))
    from pyquotex.stable_api import Quotex

Two independent ways that broke:

  * `vendor/pyquotex/` is EMPTY in a fresh clone (git cannot track an empty
    directory), while the only committed copy of the library is
    `vendor/old-pyquotex/pyquotex/`. The hardcoded path therefore existed as a
    directory but contained no package -> `ModuleNotFoundError: No module named
    'pyquotex'`, which silently killed every real broker connection.
  * In the container the module lives at `/app/backend/app/services/market.py`,
    so `parents[3]` is `/app` and the computed path was outside the
    `backend/`-scoped build context entirely.

The ordering rules are tested against synthetic trees through the pure
`select_from_roots()`; the ancestor-walk and env-override behaviour is tested
in a subprocess against a *relocated copy* of the module, because
`_candidate_roots()` anchors on the importing module's own file path.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.pyquotex_vendor import (
    ensure_pyquotex_on_path,
    find_pyquotex_path,
    select_from_roots,
)

BACKEND = Path(__file__).resolve().parent
REPO = BACKEND.parent
MODULE = BACKEND / "app" / "pyquotex_vendor.py"

# Loader used by the subprocess tests: loads the module standalone from a
# relocated copy, so its ancestor walk anchors inside the synthetic tree.
_LOADER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pv", r"{module}")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
p = m.find_pyquotex_path()
print(p if p else "NONE")
"""


def _make_vendor_tree(root: Path, *populated: str) -> Path:
    """Create `root/vendor/<name>/pyquotex/[stable_api.py]` for both names.

    A name only gets `stable_api.py` if listed in `populated`; otherwise the
    package directory is created empty. That reproduces the real bug: an empty
    `vendor/pyquotex/` sitting next to a working `vendor/old-pyquotex/`.
    """
    vendor = root / "vendor"
    for name in ("pyquotex", "old-pyquotex"):
        pkg = vendor / name / "pyquotex"
        pkg.mkdir(parents=True, exist_ok=True)
        if name in populated:
            (pkg / "stable_api.py").write_text("class Quotex: ...\n")
    return vendor


def _relocate(tmp_path: Path, at: Path) -> Path:
    """Copy pyquotex_vendor.py under `at` and return the copy's path.

    Mirrors a real deployment: the module's ancestors become `at`'s ancestors,
    which is what makes the ancestor search testable.
    """
    at.mkdir(parents=True, exist_ok=True)
    copy = at / "pyquotex_vendor.py"
    shutil.copyfile(MODULE, copy)
    return copy


def _resolve_relocated(module: Path, env: dict | None = None) -> str | None:
    out = subprocess.run(
        [sys.executable, "-c", _LOADER.format(module=module)],
        env={**(env or {}), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    line = out.stdout.strip().splitlines()[-1]
    return None if line == "NONE" else line


# --------------------------------------------------------------------------- #
# The shipped tree
# --------------------------------------------------------------------------- #

def test_resolver_finds_a_populated_vendor_copy():
    """The committed library lives in vendor/old-pyquotex; vendor/pyquotex is
    empty. The resolver must return the populated one."""
    path = find_pyquotex_path()
    assert path is not None, "no vendored pyquotex found in this checkout"
    assert (path / "pyquotex" / "stable_api.py").is_file()


def test_empty_vendor_pyquotex_does_not_shadow_the_working_copy():
    """THE regression. The old hardcoded path exists as a directory but has no
    package inside; it must be skipped, not returned."""
    empty = REPO / "vendor" / "pyquotex"
    if (empty / "pyquotex" / "stable_api.py").is_file():
        pytest.skip("vendor/pyquotex is populated here -- nothing is being shadowed")
    path = find_pyquotex_path()
    assert path is not None
    assert path != empty, "resolver returned the EMPTY vendor/pyquotex directory"


def test_old_hardcoded_path_is_not_importable_but_the_resolver_result_is():
    """Direct comparison of the two paths on the shipped tree."""
    old = REPO / "vendor" / "pyquotex"
    assert not (old / "pyquotex" / "stable_api.py").is_file(), (
        "vendor/pyquotex is now populated -- this test's premise changed"
    )
    new = find_pyquotex_path()
    assert new is not None
    assert (new / "pyquotex" / "stable_api.py").is_file()


# --------------------------------------------------------------------------- #
# Pure ordering rules
# --------------------------------------------------------------------------- #

def test_falls_back_to_old_pyquotex_when_pyquotex_is_empty(tmp_path):
    """Exactly the shipped repo's situation."""
    vendor = _make_vendor_tree(tmp_path, "old-pyquotex")
    assert select_from_roots([vendor]) == vendor / "old-pyquotex"


def test_prefers_populated_pyquotex_over_old_pyquotex(tmp_path):
    vendor = _make_vendor_tree(tmp_path, "pyquotex", "old-pyquotex")
    assert select_from_roots([vendor]) == vendor / "pyquotex"


def test_preferred_names_beat_an_alphabetically_earlier_directory(tmp_path):
    """'old-pyquotex' sorts before 'pyquotex'. Without an explicit preference
    list the older copy would permanently shadow a newer one dropped into
    `vendor/pyquotex` -- this is the bug the ordering fixes."""
    vendor = _make_vendor_tree(tmp_path, "pyquotex", "old-pyquotex")
    assert sorted(p.name for p in vendor.iterdir() if p.is_dir())[0] == "old-pyquotex"
    assert select_from_roots([vendor]) == vendor / "pyquotex"


def test_unlisted_subdirectory_is_still_tried_after_the_preferred_names(tmp_path):
    """A vendor root holding the library under some other directory name must
    still resolve -- the preference list is an ordering, not a whitelist."""
    vendor = tmp_path / "vendor"
    pkg = vendor / "third-party-bundle" / "pyquotex"
    pkg.mkdir(parents=True)
    (pkg / "stable_api.py").write_text("class Quotex: ...\n")
    (vendor / "old-pyquotex" / "pyquotex").mkdir(parents=True)   # empty decoy
    assert select_from_roots([vendor]) == vendor / "third-party-bundle"


def test_returns_none_when_nothing_is_populated(tmp_path):
    vendor = _make_vendor_tree(tmp_path)          # both empty
    assert select_from_roots([vendor]) is None


def test_returns_none_when_the_root_is_missing(tmp_path):
    assert select_from_roots([tmp_path / "does" / "not" / "exist"]) is None


def test_first_matching_root_wins(tmp_path):
    a = _make_vendor_tree(tmp_path / "a", "old-pyquotex")
    b = _make_vendor_tree(tmp_path / "b", "old-pyquotex")
    assert select_from_roots([a, b]) == a / "old-pyquotex"
    assert select_from_roots([b, a]) == b / "old-pyquotex"


# --------------------------------------------------------------------------- #
# Ancestor search + env override (relocated module, subprocess)
# --------------------------------------------------------------------------- #

def test_ancestor_search_finds_vendor_beside_the_package(tmp_path):
    """Host layout: <root>/backend/app/pyquotex_vendor.py + <root>/vendor."""
    _make_vendor_tree(tmp_path, "old-pyquotex")
    module = _relocate(tmp_path, tmp_path / "backend" / "app")
    assert _resolve_relocated(module) == str(tmp_path / "vendor" / "old-pyquotex")


def test_container_layout_resolves_without_the_env_var(tmp_path):
    """Image layout: /app/backend + /app/vendor. The ancestor search alone must
    find it, so the app keeps working even if the pinned ENV is dropped."""
    app_dir = tmp_path / "app"
    _make_vendor_tree(app_dir, "old-pyquotex")
    module = _relocate(tmp_path, app_dir / "backend" / "app")
    assert _resolve_relocated(module) == str(app_dir / "vendor" / "old-pyquotex")


def test_container_layout_with_pinned_env_var(tmp_path):
    app_dir = tmp_path / "app"
    vendor = _make_vendor_tree(app_dir, "old-pyquotex")
    module = _relocate(tmp_path, app_dir / "backend" / "app")
    got = _resolve_relocated(module, env={"PYQUOTEX_VENDOR_PATH": str(vendor)})
    assert got == str(vendor / "old-pyquotex")


def test_env_var_pointing_straight_at_the_package_parent(tmp_path):
    vendor = _make_vendor_tree(tmp_path, "old-pyquotex")
    target = vendor / "old-pyquotex"
    module = _relocate(tmp_path, tmp_path / "backend" / "app")
    got = _resolve_relocated(module, env={"PYQUOTEX_VENDOR_PATH": str(target)})
    assert got == str(target)


def test_dead_env_var_falls_back_instead_of_failing(tmp_path):
    """A misconfigured PYQUOTEX_VENDOR_PATH must not disable the fallback."""
    _make_vendor_tree(tmp_path, "old-pyquotex")
    module = _relocate(tmp_path, tmp_path / "backend" / "app")
    got = _resolve_relocated(
        module, env={"PYQUOTEX_VENDOR_PATH": "/nonexistent/nowhere"}
    )
    assert got == str(tmp_path / "vendor" / "old-pyquotex")


def test_env_var_takes_priority_over_the_ancestor_search(tmp_path):
    """Explicit config beats the search even when the search would succeed."""
    _make_vendor_tree(tmp_path, "old-pyquotex")
    other = tmp_path / "override"
    other_vendor = _make_vendor_tree(other, "old-pyquotex")
    module = _relocate(tmp_path, tmp_path / "backend" / "app")
    got = _resolve_relocated(module, env={"PYQUOTEX_VENDOR_PATH": str(other_vendor)})
    assert got == str(other_vendor / "old-pyquotex")


# --------------------------------------------------------------------------- #
# sys.path hygiene
# --------------------------------------------------------------------------- #

def test_ensure_appends_and_is_idempotent():
    """The vendor tree ships its own top-level `app.py`; prepending it would
    shadow this backend's `app` package. The entry must be appended."""
    path = ensure_pyquotex_on_path()
    if path is None:
        pytest.skip("no vendored pyquotex in this checkout")
    entry = str(path)
    assert entry in sys.path
    if str(BACKEND) in sys.path:
        assert sys.path.index(entry) > sys.path.index(str(BACKEND))
    n = sys.path.count(entry)
    ensure_pyquotex_on_path()
    assert sys.path.count(entry) == n, "ensure_pyquotex_on_path is not idempotent"


def test_ensure_never_raises_and_returns_path_or_none():
    result = ensure_pyquotex_on_path()
    assert result is None or isinstance(result, Path)


def test_vendor_entry_does_not_shadow_the_backend_app_package():
    """Guard the append rule end to end: after the vendor dir is on sys.path,
    `import app` must still resolve inside this backend."""
    ensure_pyquotex_on_path()
    import importlib

    import app as app_pkg

    importlib.reload(app_pkg)
    assert str(BACKEND) in str(app_pkg.__file__), app_pkg.__file__
