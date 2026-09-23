"""Sentiment Adjustment default-ON: defaults, migration, explicit preference.

Covers the seven behaviours the change is responsible for:

  1. a new user gets sentiment ON
  2. an existing settings file whose `false` was only the OLD shipped default is
     migrated to ON
  3. an existing file recording an EXPLICIT OFF stays OFF
  4. the UI/API can flip the toggle both ways
  5. sentiment being unavailable never blocks signal generation
  6. sentiment being available applies the existing bounded adjustment
  7. a restart preserves whichever value the user chose

Nothing here re-implements the sentiment maths: `sentiment_confidence_adjustment`
is called directly, so the bounds under test are the shipped ones.
"""

import json

import pytest

from app.config import MarketDataSettings, RuntimeSettings


# ────────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────────

USER = "sentiment-tester"


def _write_settings(tmp_path, monkeypatch, market_data: dict) -> None:
    """Point user_data_dir() at tmp_path and drop a settings file there."""
    monkeypatch.setattr("app.config.USERS_DIR", tmp_path)
    d = tmp_path / USER
    d.mkdir(parents=True, exist_ok=True)
    payload = RuntimeSettings().model_dump()
    payload["market_data"] = market_data
    (d / "runtime_settings.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _read_raw(tmp_path) -> dict:
    return json.loads(
        (tmp_path / USER / "runtime_settings.json").read_text(encoding="utf-8")
    )


# ────────────────────────────────────────────────────────────────────────────
# 1. new user default
# ────────────────────────────────────────────────────────────────────────────

def test_new_user_default_is_enabled():
    """A brand-new install must get Sentiment Adjustment ON."""
    rt = RuntimeSettings()
    assert rt.market_data.sentiment_enabled is True


def test_new_user_has_no_explicit_preference_recorded():
    """The default is a default: it must not be marked as a user choice.

    If a fresh install were marked explicit, a later default change could never
    reach it again -- the marker is what makes migration one-directional.
    """
    assert MarketDataSettings().sentiment_enabled_set_by_user is False


def test_new_user_load_persists_enabled(tmp_path, monkeypatch):
    """No settings file at all => load() creates one with sentiment ON."""
    monkeypatch.setattr("app.config.USERS_DIR", tmp_path)
    rt = RuntimeSettings.load(USER)
    assert rt.market_data.sentiment_enabled is True
    assert _read_raw(tmp_path)["market_data"]["sentiment_enabled"] is True


# ────────────────────────────────────────────────────────────────────────────
# 2. existing user, no explicit preference -> migrated ON
# ────────────────────────────────────────────────────────────────────────────

def test_existing_user_without_explicit_preference_is_migrated_to_on(
    tmp_path, monkeypatch
):
    """The core migration.

    Every settings file written before this change contains
    `"sentiment_enabled": false`, because save() dumps every field and the old
    class default was False. That false was never a choice, so it must become
    true -- and the file must be rewritten so the change sticks.
    """
    _write_settings(tmp_path, monkeypatch, {
        "sentiment_enabled": False,
        # no sentiment_enabled_set_by_user key at all: the pre-change shape
    })

    rt = RuntimeSettings.load(USER)

    assert rt.market_data.sentiment_enabled is True
    assert _read_raw(tmp_path)["market_data"]["sentiment_enabled"] is True


def test_migration_does_not_mark_the_preference_explicit(tmp_path, monkeypatch):
    """Migration is not a user choice, so it must not set the marker.

    Otherwise a migrated user who later wants OFF would look identical to one
    who never had a say, and the two cases could not be told apart.
    """
    _write_settings(tmp_path, monkeypatch, {"sentiment_enabled": False})
    rt = RuntimeSettings.load(USER)
    assert rt.market_data.sentiment_enabled is True
    assert rt.market_data.sentiment_enabled_set_by_user is False


def test_migration_leaves_an_already_enabled_file_alone(tmp_path, monkeypatch):
    """A file that already says true needs no rewriting."""
    _write_settings(tmp_path, monkeypatch, {"sentiment_enabled": True})
    rt = RuntimeSettings.load(USER)
    assert rt.market_data.sentiment_enabled is True


def test_migration_survives_a_malformed_settings_file(tmp_path, monkeypatch):
    """A broken file must not raise out of load()."""
    monkeypatch.setattr("app.config.USERS_DIR", tmp_path)
    d = tmp_path / USER
    d.mkdir(parents=True, exist_ok=True)
    (d / "runtime_settings.json").write_text("{not json", encoding="utf-8")
    # Falls through to a fresh instance rather than propagating.
    rt = RuntimeSettings.load(USER)
    assert rt.market_data.sentiment_enabled is True


# ────────────────────────────────────────────────────────────────────────────
# 3. explicit OFF is preserved
# ────────────────────────────────────────────────────────────────────────────

def test_explicit_off_is_preserved_across_load(tmp_path, monkeypatch):
    """A recorded explicit OFF must never be migrated back to ON."""
    _write_settings(tmp_path, monkeypatch, {
        "sentiment_enabled": False,
        "sentiment_enabled_set_by_user": True,
    })

    rt = RuntimeSettings.load(USER)

    assert rt.market_data.sentiment_enabled is False
    assert rt.market_data.sentiment_enabled_set_by_user is True
    # and the file was not rewritten behind the user's back
    assert _read_raw(tmp_path)["market_data"]["sentiment_enabled"] is False


def test_explicit_on_is_preserved_across_load(tmp_path, monkeypatch):
    """Symmetry: an explicit ON is equally a choice."""
    _write_settings(tmp_path, monkeypatch, {
        "sentiment_enabled": True,
        "sentiment_enabled_set_by_user": True,
    })
    rt = RuntimeSettings.load(USER)
    assert rt.market_data.sentiment_enabled is True
    assert rt.market_data.sentiment_enabled_set_by_user is True


# ────────────────────────────────────────────────────────────────────────────
# 4 + 7. UI/API flips the toggle, and a restart keeps it
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def orch(pg, tmp_path, monkeypatch):
    """A real Orchestrator with settings writes redirected to tmp_path."""
    from app.orchestrator import Orchestrator
    from app.session_manager import BrokerCredentials, session_manager

    monkeypatch.setattr("app.config.USERS_DIR", tmp_path)
    creds = BrokerCredentials(
        quotex_email="broker@example.invalid", quotex_password="pw", quotex_is_demo=True
    )
    monkeypatch.setattr(session_manager, "get_credentials", lambda uid: creds)
    monkeypatch.setattr(session_manager, "set_account_type", lambda uid, d: True)
    return Orchestrator(USER, RuntimeSettings())


def test_apply_settings_records_an_explicit_change(orch):
    """Flipping the toggle must be remembered as an explicit preference."""
    assert orch.runtime.market_data.sentiment_enabled is True

    new = orch.runtime.model_copy(deep=True)
    new.market_data.sentiment_enabled = False
    orch.apply_settings(new)

    assert orch.runtime.market_data.sentiment_enabled is False
    assert orch.runtime.market_data.sentiment_enabled_set_by_user is True


def test_apply_settings_flips_back_on(orch):
    """false -> true is equally an explicit choice."""
    new = orch.runtime.model_copy(deep=True)
    new.market_data.sentiment_enabled = False
    orch.apply_settings(new)
    assert orch.runtime.market_data.sentiment_enabled is False

    new2 = orch.runtime.model_copy(deep=True)
    new2.market_data.sentiment_enabled = True
    orch.apply_settings(new2)

    assert orch.runtime.market_data.sentiment_enabled is True
    assert orch.runtime.market_data.sentiment_enabled_set_by_user is True


def test_unrelated_settings_save_does_not_downgrade_explicitness(orch):
    """Saving some OTHER setting must not make the choice migratable again.

    The UI PUTs the whole object, so an unrelated save resends the same
    sentiment value. Losing the marker there would silently re-open the door to
    a future migration overriding a deliberate OFF.
    """
    off = orch.runtime.model_copy(deep=True)
    off.market_data.sentiment_enabled = False
    orch.apply_settings(off)
    assert orch.runtime.market_data.sentiment_enabled_set_by_user is True

    unrelated = orch.runtime.model_copy(deep=True)
    unrelated.risk.max_concurrent_trades = 7          # nothing to do with sentiment
    orch.apply_settings(unrelated)

    assert orch.runtime.market_data.sentiment_enabled is False
    assert orch.runtime.market_data.sentiment_enabled_set_by_user is True


def test_restart_preserves_explicit_off(orch, tmp_path):
    """The end-to-end guarantee: choose OFF, restart, still OFF."""
    off = orch.runtime.model_copy(deep=True)
    off.market_data.sentiment_enabled = False
    orch.apply_settings(off)
    orch.runtime.save(USER)

    reloaded = RuntimeSettings.load(USER)

    assert reloaded.market_data.sentiment_enabled is False
    assert reloaded.market_data.sentiment_enabled_set_by_user is True


def test_restart_preserves_explicit_on(orch, tmp_path):
    """And the same for a user who deliberately turned it ON."""
    on = orch.runtime.model_copy(deep=True)
    on.market_data.sentiment_enabled = False
    orch.apply_settings(on)                      # off first, to make it explicit
    on2 = orch.runtime.model_copy(deep=True)
    on2.market_data.sentiment_enabled = True
    orch.apply_settings(on2)
    orch.runtime.save(USER)

    reloaded = RuntimeSettings.load(USER)
    assert reloaded.market_data.sentiment_enabled is True
    assert reloaded.market_data.sentiment_enabled_set_by_user is True


# ────────────────────────────────────────────────────────────────────────────
# 5 + 6. runtime behaviour: never blocks, and stays bounded
# ────────────────────────────────────────────────────────────────────────────

def test_sentiment_unavailable_yields_zero_and_never_blocks():
    """No sentiment => adjustment 0.0, and nothing raises.

    Requirement 6: an unavailable/stale/failed sentiment must fall back to the
    existing OHLC confidence, not reject the signal.
    """
    from app.engine.market_features import (
        SentimentState,
        sentiment_confidence_adjustment,
    )

    now = 1_700_000_000.0

    # not available at all
    missing = sentiment_confidence_adjustment(
        SentimentState(), direction="CALL", now=now, enabled=True, max_adjustment=4.0
    )
    assert missing.adjustment == 0.0

    # stale: available and strong, but older than the 120s bound
    stale = SentimentState(
        available=True, bias=0.80, strength=0.80, timestamp=now - 500.0, stale=False
    )
    stale_adj = sentiment_confidence_adjustment(
        stale, direction="CALL", now=now, enabled=True,
        max_age_seconds=120.0, max_adjustment=4.0,
    )
    assert stale_adj.adjustment == 0.0
    assert stale_adj.reason == "stale"

    # weak: fresh, but below the 0.10 strength floor
    weak = SentimentState(
        available=True, bias=0.04, strength=0.04, timestamp=now, stale=False
    )
    weak_adj = sentiment_confidence_adjustment(
        weak, direction="CALL", now=now, enabled=True,
        min_strength=0.10, max_adjustment=4.0,
    )
    assert weak_adj.adjustment == 0.0
    assert weak_adj.reason == "weak"

    # The confidence arithmetic is untouched in every case above.
    for adj in (missing, stale_adj, weak_adj):
        assert max(0, min(100, 72.0 + adj.adjustment)) == 72.0


def test_sentiment_available_applies_the_existing_bounded_adjustment():
    """With real sentiment the shipped bounded nudge applies, capped at 4.0."""
    from app.engine.market_features import (
        normalize_sentiment,
        sentiment_confidence_adjustment,
    )

    now = 1_700_000_000.0
    state = normalize_sentiment({"call": 100, "put": 0}, now=now)

    adj = sentiment_confidence_adjustment(
        state, direction="CALL", now=now, enabled=True,
        min_strength=0.10, max_age_seconds=120.0, max_adjustment=4.0,
    )

    assert adj.applied is True
    assert 0.0 < adj.adjustment <= 4.0
    # A fully one-sided sentiment saturates the configured cap exactly.
    assert adj.adjustment == pytest.approx(4.0)


def test_sentiment_adjustment_respects_a_smaller_configured_cap():
    """The cap is config-driven, not hardcoded to 4.0."""
    from app.engine.market_features import (
        normalize_sentiment,
        sentiment_confidence_adjustment,
    )

    now = 1_700_000_000.0
    state = normalize_sentiment({"call": 100, "put": 0}, now=now)

    adj = sentiment_confidence_adjustment(
        state, direction="CALL", now=now, enabled=True,
        min_strength=0.10, max_age_seconds=120.0, max_adjustment=1.5,
    )
    assert adj.adjustment == pytest.approx(1.5)


def test_disabled_sentiment_is_inert_even_with_strong_data():
    """OFF must bypass the path completely, whatever the data says."""
    from app.engine.market_features import (
        normalize_sentiment,
        sentiment_confidence_adjustment,
    )

    now = 1_700_000_000.0
    state = normalize_sentiment({"call": 100, "put": 0}, now=now)

    adj = sentiment_confidence_adjustment(
        state, direction="CALL", now=now, enabled=False, max_adjustment=4.0
    )
    assert adj.adjustment == 0.0
    assert adj.reason == "disabled"


def test_default_bounds_are_unchanged():
    """Requirement 7: the three shipped bounds must survive this change."""
    md = MarketDataSettings()
    assert md.sentiment_max_age_seconds == 120.0
    assert md.sentiment_min_strength == 0.10
    assert md.sentiment_max_confidence_adjustment == 4.0
