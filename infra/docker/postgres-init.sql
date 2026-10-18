-- Runs once, on first initialization of the data volume.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- Separate database for the automated test suite.
CREATE DATABASE atlas_test OWNER atlas;
\connect atlas_test
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
