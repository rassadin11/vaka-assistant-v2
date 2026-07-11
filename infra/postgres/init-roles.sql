DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN
        CREATE ROLE migrator
            LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    ELSE
        ALTER ROLE migrator
            WITH LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
        CREATE ROLE app
            LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    ELSE
        ALTER ROLE app
            WITH LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service') THEN
        CREATE ROLE service
            LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            BYPASSRLS;
    ELSE
        ALTER ROLE service
            WITH LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            BYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'metrics_ro') THEN
        CREATE ROLE metrics_ro
            LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            BYPASSRLS;
    ELSE
        ALTER ROLE metrics_ro
            WITH LOGIN
            PASSWORD 'dev-local-only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            BYPASSRLS;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE assistant TO migrator, app, service, metrics_ro;
GRANT CREATE ON DATABASE assistant TO migrator;

GRANT USAGE, CREATE ON SCHEMA public TO migrator;
GRANT USAGE ON SCHEMA public TO app, service, metrics_ro;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_partman;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO migrator;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO migrator;

ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app, service;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app, service;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO metrics_ro;

DO $$
BEGIN
    IF to_regclass('public.users') IS NOT NULL THEN
        GRANT SELECT ON TABLE users, messages, usage, tool_calls_log TO metrics_ro;
    END IF;
END
$$;
