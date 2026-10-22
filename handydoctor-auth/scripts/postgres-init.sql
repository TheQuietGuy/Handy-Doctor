-- scripts/postgres-init.sql
-- Run automatically by PostgreSQL on first container start.
-- Creates useful extensions and applies conservative performance settings.

-- UUID generation (used as primary keys throughout the app)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- pgcrypto for any server-side hashing (belt-and-braces; app uses bcrypt)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Full-text search support (for future session search)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Performance: set statement timeout to prevent runaway queries
ALTER DATABASE handydoctor SET statement_timeout = '30s';
ALTER DATABASE handydoctor SET lock_timeout      = '10s';
ALTER DATABASE handydoctor SET idle_in_transaction_session_timeout = '60s';

-- Log slow queries (visible in docker logs or pg_stat_statements)
ALTER DATABASE handydoctor SET log_min_duration_statement = '500';
