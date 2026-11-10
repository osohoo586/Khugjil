-- Run once as the PostgreSQL superuser, before the first migration.
-- Passwords are supplied via psql variables so they never live in this file:
--   psql -v owner_pw="..." -v app_pw="..." -f init-roles.sql
--
-- khugjil_owner: runs migrations and owns the tables and the SECURITY DEFINER lookup functions.
--                BYPASSRLS is required so those narrow functions can resolve a secret's HMAC to
--                a tenant id before any tenant context exists. Never used by the app at runtime.
-- khugjil_app:   the runtime role. Not the table owner, no BYPASSRLS, so FORCE ROW LEVEL
--                SECURITY applies to every query it runs.

CREATE ROLE khugjil_owner LOGIN PASSWORD :'owner_pw' NOSUPERUSER BYPASSRLS NOCREATEROLE;
CREATE ROLE khugjil_app   LOGIN PASSWORD :'app_pw'   NOSUPERUSER NOBYPASSRLS NOCREATEROLE;

CREATE DATABASE khugjil OWNER khugjil_owner;
\connect khugjil
ALTER SCHEMA public OWNER TO khugjil_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO khugjil_app;
