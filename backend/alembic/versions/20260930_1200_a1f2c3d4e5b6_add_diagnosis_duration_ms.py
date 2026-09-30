"""Add `diagnosis.duration_ms`.

`run_diagnosis()` has always computed the elapsed time and returned it, but the
ORM model had no column for it, and the auto-diagnosis path discarded it
(`row, _duration_ms = ...`). So `GET /api/v1/diagnoses` reported `durationMs: null`
for every row — the column in the UI showed only "—".

Nullable on purpose: rows written before this migration have no measured value,
and back-filling a guess would be the same class of dishonesty the project keeps
refusing elsewhere (a fabricated number that looks like a measurement).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a1f2c3d4e5b6'
down_revision: str | None = '26ee841d18ba'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'diagnosis',
        sa.Column('duration_ms', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('diagnosis', 'duration_ms')
