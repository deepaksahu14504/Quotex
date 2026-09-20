"""persist candle volume_source

RCA C.1: `CandleStore.upsert_many()` wrote only (ts, open, high, low, close,
volume) and both readers hardcoded `volume_source="real"` on the way back out.
For the pyquotex path that is wrong — the stored `volume` is a *tick count*, and
re-labelling it "real" makes `indicators._has_real_volume()` return True, which
enables the exact volume mathematics its own docstring says are invalid for tick
counts (measured: `volume_strength` returned 0.5 instead of the neutral 1.0 that
`indicators.py` promises for synthetic volume).

The column is nullable with no server default on purpose: rows written before
this migration have no recorded provenance, and NULL is the honest encoding of
that. `CandleStore.get_latest_n()`/`get_range()` map NULL -> "real" to preserve
the previous behaviour for pre-existing public-provider rows, while any row
written from now on carries its true tag.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "candles",
        sa.Column("volume_source", sa.String, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candles", "volume_source")
