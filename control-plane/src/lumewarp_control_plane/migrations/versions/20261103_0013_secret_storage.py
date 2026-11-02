"""Keep OIDC login secrets and webhook URLs out of the database.

Revision ID: 0013
Revises: 0012

- `oidc_login_states` loses `nonce` and `code_verifier`. Both now live in the AES-256-GCM
  sealed login-state cookie (auth/oidc.py), keyed by `LUMEWARP_SECRET_KEY`; the database
  keeps only the one-time state hash, return path and redirect URI. Pending logins are
  discarded so no row is expected to carry the removed columns.
- `notifier_channels` refuses inline webhook configuration: a channel of kind `webhook`
  has exactly `url_ref` (an environment variable name matching the same pattern as
  `upstreams.api_key_ref`) and optionally a numeric `timeout_seconds` in [0.001, 30]. The CHECK
  refuses a literal URL, a JSON null, other keys, non-string refs and non-numeric or
  out-of-range timeouts. Rows that do not satisfy it cannot be migrated automatically
  because the URL must be moved into the environment by the operator first.
- `upstreams.metrics_url` no longer accepts a query, which can carry a credential. The
  upgrade refuses to run while a row has one, naming the upstream and never the URL: only
  the operator knows the endpoint to use instead. Set a clean `metrics_url` or null first.

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
    "kind <> 'webhook' OR coalesce("
    " jsonb_typeof(config) = 'object'"
    " AND config ? 'url_ref'"
    " AND jsonb_typeof(config->'url_ref') = 'string'"
    " AND (config->>'url_ref') ~ '^[A-Za-z_][A-Za-z0-9_]*$'"
    " AND octet_length(config->>'url_ref') <= 200"
    " AND (NOT (config ? 'timeout_seconds')"
    " OR (CASE WHEN jsonb_typeof(config->'timeout_seconds') <> 'number' THEN false"
    " ELSE (config->>'timeout_seconds')::numeric >= 0.001"
    " AND (config->>'timeout_seconds')::numeric <= 30 END))"
    " AND config - 'url_ref' - 'timeout_seconds' = '{}'::jsonb,"
    " false)"
)


def upgrade() -> None:
    names = op.get_bind().scalars(
        sa.text(
            "SELECT name FROM upstreams WHERE position(chr(63) in metrics_url) > 0 ORDER BY name"
        )
    )
    blocked = list(names)
    if blocked:
        raise RuntimeError(
            "upstream metrics_url must not contain a query; set a clean metrics_url or null "
            "on: " + ", ".join(blocked)
        )
    op.execute("DELETE FROM oidc_login_states")
    op.drop_column("oidc_login_states", "nonce")
    op.drop_column("oidc_login_states", "code_verifier")
    op.create_check_constraint("webhook_config_url_ref", "notifier_channels", WEBHOOK_CONFIG_CHECK)


def downgrade() -> None:
    op.drop_constraint("webhook_config_url_ref", "notifier_channels", type_="check")
    op.execute("DELETE FROM oidc_login_states")
    op.add_column("oidc_login_states", sa.Column("nonce", sa.Text(), nullable=False))
    op.add_column("oidc_login_states", sa.Column("code_verifier", sa.Text(), nullable=False))
