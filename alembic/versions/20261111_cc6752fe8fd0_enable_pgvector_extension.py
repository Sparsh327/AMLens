"""enable pgvector extension

Revision ID: cc6752fe8fd0
Revises:
Create Date: 2026-11-11 10:05:56.231577

pgvector adds the `vector` column type + similarity operators (<->, <=>) that
the RAG index (F6) will use. The extension binary ships in the
pgvector/pgvector Docker image; this just switches it on for our database.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "cc6752fe8fd0"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
