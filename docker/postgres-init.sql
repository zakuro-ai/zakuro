-- docker/postgres-init.sql
--
-- Minimal bootstrap script for the local-development postgres container used
-- in docker/docker-compose.yml. Loaded by the postgres image from
-- /docker-entrypoint-initdb.d/ on first boot only (i.e. when pgdata is empty).
--
-- Real schema management lives with the broker: on first start the broker
-- service is expected to run its own migrations against the `zakuro` database
-- created by the postgres image's environment variables. This file therefore
-- only seeds the operational extensions the broker assumes are available and
-- creates a logical schema namespace so migrations don't have to.
--
-- If you need to pre-seed data, drop your own .sql file into the
-- ./docker/ directory and mount it into /docker-entrypoint-initdb.d/ via
-- docker-compose; postgres runs entries in lexicographic order.

\connect zakuro

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE SCHEMA IF NOT EXISTS ledger;
CREATE SCHEMA IF NOT EXISTS workers;

COMMENT ON SCHEMA ledger  IS 'Credit ledger tables, managed by broker migrations.';
COMMENT ON SCHEMA workers IS 'Worker registry tables, managed by broker migrations.';
