-- Website Risk Investigator — add verdict fields to investigations.
--
-- Idempotent: safe to run on a fresh volume (alongside 001_initial.sql) or on an
-- existing database. The docker-entrypoint-initdb.d mechanism only runs on FIRST
-- boot of the pgdata volume, so for an existing dev DB apply manually:
--
--     docker compose exec -T postgres psql -U wri -d wri \
--         -f /docker-entrypoint-initdb.d/002_verdict_fields.sql
--
-- Or just recreate the volume: `docker compose down -v && docker compose up -d`.

ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS summary   TEXT,
    ADD COLUMN IF NOT EXISTS findings  JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- LLM-written plain-English narrative. NULL when the LLM wasn't called
    -- (no API key) or when its output was rejected by validation; consumers
    -- should then fall back to `summary` (the deterministic template).
    ADD COLUMN IF NOT EXISTS narrative JSONB,
    -- LLM-produced deep review — exploratory, evidence-grounded analysis
    -- that sees raw page text + screenshots. Populated on-demand by the
    -- POST /investigations/{id}/deep-review endpoint, cached here so repeat
    -- requests are free. NULL until requested (and may stay NULL if the
    -- LLM is unavailable or the output failed validation).
    ADD COLUMN IF NOT EXISTS deep_review JSONB;
