"""v1 fixed schema."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260709_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "users",
    "messages",
    "dialog_summaries",
    "memory_facts",
    "transactions",
    "budgets",
    "scheduled_tasks",
    "documents",
    "doc_chunks",
    "oauth_tokens",
    "outbox_actions",
    "tool_calls_log",
    "usage",
)

USER_SCOPED_TABLES = (
    "messages",
    "dialog_summaries",
    "memory_facts",
    "transactions",
    "budgets",
    "scheduled_tasks",
    "documents",
    "doc_chunks",
    "oauth_tokens",
    "outbox_actions",
    "tool_calls_log",
    "usage",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_partman")

    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            tg_user_id bigint UNIQUE NOT NULL,
            tg_chat_id bigint NOT NULL,
            username text,
            first_name text,
            status text CHECK (
                status IN ('pending', 'active', 'rejected', 'banned')
            ) DEFAULT 'pending',
            plan text DEFAULT 'trial',
            paid_until timestamptz,
            timezone text NOT NULL,
            created_at timestamptz,
            updated_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE messages (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role text CHECK (role IN ('user', 'assistant', 'tool')),
            content text,
            tool_calls jsonb,
            tool_call_id text,
            meta jsonb,
            tokens int,
            trace_id uuid,
            created_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX messages_user_id_id_idx ON messages (user_id, id)")

    op.execute(
        """
        CREATE TABLE dialog_summaries (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            summary text NOT NULL,
            upto_message_id uuid NOT NULL,
            tokens int,
            created_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX dialog_summaries_user_id_created_at_idx "
        "ON dialog_summaries (user_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE memory_facts (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text text NOT NULL,
            last_used_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz,
            updated_at timestamptz,
            embedding vector(1024)
        )
        """
    )
    op.execute(
        "CREATE INDEX memory_facts_user_id_last_used_at_idx ON memory_facts (user_id, last_used_at)"
    )
    op.execute(
        "CREATE INDEX memory_facts_embedding_hnsw_idx "
        "ON memory_facts USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE transactions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount numeric(12,2) NOT NULL CHECK (amount > 0),
            direction text CHECK (direction IN ('expense', 'income')),
            category text CHECK (
                category IN (
                    'food',
                    'transport',
                    'housing',
                    'health',
                    'entertainment',
                    'shopping',
                    'subscriptions',
                    'salary',
                    'other'
                )
            ),
            currency text NOT NULL DEFAULT 'RUB',
            description text NOT NULL DEFAULT '',
            ts timestamptz NOT NULL,
            created_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX transactions_user_id_ts_idx ON transactions (user_id, ts)")

    op.execute(
        """
        CREATE TABLE budgets (
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category text CHECK (
                category IN (
                    'food',
                    'transport',
                    'housing',
                    'health',
                    'entertainment',
                    'shopping',
                    'subscriptions',
                    'salary',
                    'other'
                )
            ),
            monthly_limit numeric(12,2) NOT NULL CHECK (monthly_limit > 0),
            created_at timestamptz,
            updated_at timestamptz,
            PRIMARY KEY (user_id, category)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE scheduled_tasks (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind text CHECK (kind IN ('reminder', 'agent_task')),
            title text,
            payload text NOT NULL,
            cron_expr text,
            next_run_at timestamptz NOT NULL,
            status text CHECK (status IN ('active', 'done', 'cancelled')),
            last_run_at timestamptz,
            created_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX scheduled_tasks_status_next_run_at_idx "
        "ON scheduled_tasks (status, next_run_at)"
    )
    op.execute(
        "CREATE INDEX scheduled_tasks_user_id_status_idx ON scheduled_tasks (user_id, status)"
    )

    op.execute(
        """
        CREATE TABLE documents (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename text,
            pages int,
            status text CHECK (status IN ('processing', 'ready', 'failed')),
            tg_file_id text,
            s3_key text,
            size_bytes bigint,
            created_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX documents_user_id_idx ON documents (user_id)")

    op.execute(
        """
        CREATE TABLE doc_chunks (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_id bigint REFERENCES documents(id) ON DELETE CASCADE,
            page int,
            chunk_index int,
            text text NOT NULL,
            tokens int,
            embedding vector(1024)
        )
        """
    )
    op.execute("CREATE INDEX doc_chunks_doc_id_idx ON doc_chunks (doc_id)")
    op.execute("CREATE INDEX doc_chunks_user_id_idx ON doc_chunks (user_id)")
    op.execute(
        "CREATE INDEX doc_chunks_embedding_hnsw_idx "
        "ON doc_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE oauth_tokens (
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider text DEFAULT 'google',
            access_token_enc bytea NOT NULL,
            refresh_token_enc bytea,
            key_version int NOT NULL DEFAULT 1,
            expires_at timestamptz,
            scopes text[],
            status text CHECK (status IN ('active', 'reconnect_required', 'revoked')),
            created_at timestamptz,
            updated_at timestamptz,
            PRIMARY KEY (user_id, provider)
        )
        """
    )
    op.execute(
        "CREATE INDEX oauth_tokens_expires_at_active_idx "
        "ON oauth_tokens (expires_at) WHERE status = 'active'"
    )

    op.execute(
        """
        CREATE TABLE outbox_actions (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action jsonb NOT NULL,
            status text CHECK (
                status IN ('pending', 'executing', 'done', 'failed', 'cancelled')
            ),
            attempts int NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz,
            executed_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX outbox_actions_status_created_at_idx ON outbox_actions (status, created_at)"
    )

    op.execute(
        """
        CREATE TABLE tool_calls_log (
            id bigint GENERATED ALWAYS AS IDENTITY,
            user_id uuid NOT NULL,
            trace_id uuid,
            tool_name text NOT NULL,
            args jsonb,
            result_status text,
            error text,
            latency_ms int,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    op.execute(
        "SELECT create_parent("
        "p_parent_table := 'public.tool_calls_log', "
        "p_control := 'created_at', "
        "p_interval := '1 month', "
        "p_type := 'range', "
        "p_premake := 4"
        ")"
    )
    op.execute(
        "CREATE INDEX tool_calls_log_user_id_created_at_idx ON tool_calls_log (user_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE usage (
            id bigint GENERATED ALWAYS AS IDENTITY,
            user_id uuid NOT NULL,
            trace_id uuid,
            model text NOT NULL,
            prompt_tokens int,
            completion_tokens int,
            cached_tokens int,
            cost_usd numeric(10,6) NOT NULL,
            queue text CHECK (queue IN ('interactive', 'background')),
            created_at timestamptz NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    op.execute(
        "SELECT create_parent("
        "p_parent_table := 'public.usage', "
        "p_control := 'created_at', "
        "p_interval := '1 month', "
        "p_type := 'range', "
        "p_premake := 4"
        ")"
    )
    op.execute("CREATE INDEX usage_user_id_created_at_idx ON usage (user_id, created_at)")

    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app, service"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public "
        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app, service"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app, service"
    )
    op.execute("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app, service")

    _enable_rls()


def downgrade() -> None:
    op.execute(
        "DELETE FROM part_config WHERE parent_table IN ('public.tool_calls_log', 'public.usage')"
    )
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _enable_rls() -> None:
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY users_user_isolation
            ON users
            USING (id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        """
    )

    for table in USER_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_user_isolation
                ON {table}
                USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
            """
        )
