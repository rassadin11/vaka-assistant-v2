FROM pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb

RUN apt-get update \
    && apt-get install -y --no-install-recommends pgbackrest postgresql-16-partman \
    && mkdir -p /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest \
    && chown -R postgres:postgres /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest \
    && rm -rf /var/lib/apt/lists/*
