"""Add the narrow Mini App user resolver."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260717_0002"
down_revision: str | None = "20260709_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the app-only resolver required before an RLS-bound request."""

    op.execute(
        """
        CREATE FUNCTION public.webapp_resolve_user(p_tg_user_id bigint)
        RETURNS TABLE (user_id uuid, status text)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT users.id, users.status
            FROM public.users
            WHERE users.tg_user_id = p_tg_user_id
        $$
        """
    )
    op.execute("ALTER FUNCTION public.webapp_resolve_user(bigint) OWNER TO migrator")
    op.execute("REVOKE ALL ON FUNCTION public.webapp_resolve_user(bigint) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.webapp_resolve_user(bigint) TO app")


def downgrade() -> None:
    """Remove only the backwards-compatible resolver function."""

    op.execute("DROP FUNCTION IF EXISTS public.webapp_resolve_user(bigint)")
