"""AI 调用记下 token 用量；不再统计时长，去掉已有结果里的时长标签

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# 已有结果会被复用，所以连同历史结果一起去掉“视频时长…”标签，保持原有顺序。
STRIP_DURATION = """
UPDATE jobs SET result = jsonb_set(result, '{path}', (
    SELECT coalesce(jsonb_agg(tag ORDER BY position), '[]'::jsonb)
    FROM jsonb_array_elements(result #> '{path}') WITH ORDINALITY AS item(tag, position)
    WHERE tag #>> '{{}}' NOT LIKE '视频时长%'
))
WHERE jsonb_typeof(result #> '{path}') = 'array'
"""


def upgrade() -> None:
    for column in ("input_tokens", "output_tokens", "cached_tokens"):
        op.add_column("ai_calls", sa.Column(column, sa.Integer))
    for path in ("{recognition,tags}", "{tags}"):
        op.execute(STRIP_DURATION.format(path=path))


def downgrade() -> None:
    for column in ("input_tokens", "output_tokens", "cached_tokens"):
        op.drop_column("ai_calls", column)
