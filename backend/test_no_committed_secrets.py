"""Guard rails for RCA F3 — no broker credentials or session material in git.

Four `session.json` files were committed, each holding a live 40-character
SSID plus `laravel_session` / `remember_web_*` cookies for a real Quotex
account, and two source files contained the account's plaintext password. The
`.gitignore` had `sessions/` but pyquotex writes to `pyquotex_sessions/`, so
nothing was ever ignored.

These tests fail the build if any of it comes back. They inspect the *tracked*
file list (`git ls-files`) rather than the working tree, because the thing that
matters is what git would push.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER = re.compile(
    r"^(your[_-]?|example|changeme|placeholder|dummy|test|xxx+|\.\.\.|<|\{\{)",
    re.IGNORECASE,
)
_LITERAL_PASSWORD = re.compile(
    r"""(?:password|passwd|pwd)\s*[=:]\s*["'](?P<val>[^"']{6,})["']""",
    re.IGNORECASE,
)
_EMAIL_LITERAL = re.compile(r"""["'](?P<email>[\w.+-]+@[\w-]+\.[\w.]+)["']""")
_COOKIE_MATERIAL = re.compile(r"(laravel_session=|remember_web_[0-9a-f]{8,}=)")


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


# ── 1. No broker session files are tracked ────────────────────────────────── #

def test_no_session_json_tracked(tracked):
    offenders = [p for p in tracked if p.endswith("session.json")]
    assert not offenders, (
        "Broker session files are tracked. They contain a live SSID and auth "
        f"cookies -- untrack them (`git rm --cached`) and ignore them: {offenders}"
    )


def test_pyquotex_session_dirs_are_ignored(tracked):
    """The regression: `.gitignore` listed `sessions/` but the real directory
    is `pyquotex_sessions/`, so the rule matched nothing."""
    offenders = [p for p in tracked if "pyquotex_sessions/" in p]
    assert not offenders, f"pyquotex session material is tracked: {offenders}"


def test_runtime_data_dir_is_not_tracked(tracked):
    """`backend/data/` holds encrypted broker sessions, trade history and
    runtime_settings.json -- which contains the Telegram bot token."""
    offenders = [p for p in tracked if p.startswith("backend/data/")]
    assert not offenders, (
        "Per-user runtime state is tracked. It contains encrypted broker "
        f"sessions and a Telegram bot token: {offenders[:5]}"
    )


# ── 2. .gitignore actually covers the real paths ──────────────────────────── #

def test_gitignore_covers_the_real_session_paths():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in ("pyquotex_sessions/", "backend/data/"):
        assert needle in text, (
            f".gitignore does not ignore {needle!r} -- that is how the session "
            "files got committed in the first place"
        )


# ── 3. No hardcoded passwords in our own source ───────────────────────────── #

def test_no_hardcoded_passwords_in_source(tracked):
    offenders = []
    for path in tracked:
        if path.startswith("vendor/"):
            continue  # upstream library's own docs/examples are not ours to rewrite
        if path == "backend/test_no_committed_secrets.py":
            continue  # this module spells out the patterns it hunts for
        if not path.endswith((".py", ".ts", ".tsx", ".js", ".json", ".yml", ".yaml", ".env", ".example")):
            continue
        full = REPO_ROOT / path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _LITERAL_PASSWORD.finditer(text):
            if not _PLACEHOLDER.match(m.group("val")):
                offenders.append(f"{path}: {m.group(0)[:40]}…")
    assert not offenders, (
        "Hardcoded credential-looking literal(s) in source. Read them from the "
        f"environment instead. Offenders: {offenders}"
    )


def test_scripts_that_log_in_read_credentials_from_the_environment():
    """The two files that used to hold the real password."""
    for rel in ("backend/get_session.py", "backend/test_one.py"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "os.environ" in text, f"{rel} does not read credentials from the environment"
        assert "@gmail.com" not in text, f"{rel} still contains a real email address"


# ── 4. No live session-cookie material in our own source ──────────────────── #

def test_no_session_cookie_material_in_source(tracked):
    offenders = []
    for path in tracked:
        if path.startswith("vendor/"):
            continue
        # This module necessarily spells out the patterns it hunts for.
        if path == "backend/test_no_committed_secrets.py":
            continue
        full = REPO_ROOT / path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _COOKIE_MATERIAL.search(text):
            offenders.append(path)
    assert not offenders, (
        f"Live broker session-cookie material found in tracked files: {offenders}"
    )


# ── 5. No real personal email addresses baked into source ─────────────────── #

def test_no_real_email_literals_in_backend_source(tracked):
    """`@example.*` / `@invalid` / documented placeholders are fine; a real
    mailbox is not."""
    allowed = re.compile(r"@(example\.(com|invalid|org)|invalid|localhost|test\.com)$", re.IGNORECASE)
    offenders = []
    for path in tracked:
        if not path.startswith("backend/") or path.startswith("backend/data/"):
            continue
        if path == "backend/test_no_committed_secrets.py":
            continue
        if not path.endswith((".py", ".json", ".env", ".example")):
            continue
        full = REPO_ROOT / path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _EMAIL_LITERAL.finditer(text):
            if not allowed.search(m.group("email")):
                offenders.append(f"{path}: {m.group('email')}")
    assert not offenders, (
        f"Real-looking email address(es) hardcoded in backend source: {offenders}"
    )


# ── 6. runtime_settings.json (Telegram token) is not committed ────────────── #

def test_no_telegram_token_in_tracked_files(tracked):
    offenders = []
    for path in tracked:
        if not path.endswith(".json"):
            continue
        full = REPO_ROOT / path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "bot_token" in text:
            try:
                data = json.loads(text)
            except Exception:
                continue
            blob = json.dumps(data)
            if '"bot_token": ""' not in blob and '"bot_token": null' not in blob:
                offenders.append(path)
    assert not offenders, (
        f"A non-empty Telegram bot_token is committed: {offenders}"
    )
