"""Keep OIDC login secrets and webhook URLs out of the database.

Revision ID: 0013
Revises: 0012

- `oidc_login_states` loses `nonce` and `code_verifier`. Both now live in the AES-256-GCM
  sealed login-state cookie (auth/oidc.py), keyed by `LUMEWARP_SECRET_KEY`; the database
  keeps only the one-time state hash, return path and redirect URI. Pending logins are
  discarded so no row is expected to carry the removed columns.
- `notifier_channels` refuses inline webhook configuration: a channel of kind `webhook`
  has exactly `url_ref` (an environment variable name) and optionally `timeout_seconds`.
  Rows with a literal URL cannot be migrated automatically because the URL must be moved
  into the environment by the operator first.

Downgrade re-adds the removed columns as NOT NULL after deleting pending logins, so a
downgraded server simply starts a fresh login.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEBHOOK_CONFIG_CHECK = (
    "kind <> 'webhook' OR ("
    "jsonb_typeof(config) = 'object'"
    " AND config ? 'url_ref' AND config->>'url_ref' <> ''"
    " AND config - 'url_ref' - 'timeout_seconds' = '{}'::jsonb)"
)


def upgrade() -> None:
    op.execute("DELETE FROM oidc_login_states")
    op.drop_column("oidc_login_states", "nonce")
    op.drop_column("oidc_login_states", "code_verifier")
    op.create_check_constraint("webhook_config_url_ref", "notifier_channels", WEBHOOK_CONFIG_CHECK)


def downgrade() -> None:
    op.drop_constraint("webhook_config_url_ref", "notifier_channels", type_="check")
    op.execute("DELETE FROM oidc_login_states")
    op.add_column("oidc_login_states", sa.Column("nonce", sa.Text(), nullable=False))
    op.add_column("oidc_login_states", sa.Column("code_verifier", sa.Text(), nullable=False))
