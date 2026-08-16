-- Create the Prefect server database on first boot of a fresh Postgres volume.
-- The postgres docker-entrypoint runs files in /docker-entrypoint-initdb.d in
-- alphabetical order as the POSTGRES_USER superuser. ECRKE uses two databases
-- on the same server: `ecrke` (created via POSTGRES_DB) and `ecrke_prefect`
-- (Prefect's Postgres-backed queue state, design doc §14 — no Redis).
CREATE DATABASE ecrke_prefect;
