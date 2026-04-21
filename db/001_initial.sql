-- Website Risk Investigator — initial schema
-- Loaded automatically by the postgres:16 entrypoint on first boot.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =====================================================================
-- investigations
-- =====================================================================
CREATE TABLE investigations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    input_url         TEXT NOT NULL,
    normalized_origin TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued',
        -- queued | planning | crawling | analyzing | reporting | done | failed
    risk_band         TEXT,                    -- low | medium | high | critical
    confidence        NUMERIC(4,3),            -- 0.000 .. 1.000
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX ix_investigations_status ON investigations(status);
CREATE INDEX ix_investigations_origin ON investigations(normalized_origin);

-- =====================================================================
-- pages
-- =====================================================================
CREATE TABLE pages (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id UUID NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    url              TEXT NOT NULL,
    final_url        TEXT,
    http_status      INT,
    mime             TEXT,
    title            TEXT,
    lang             TEXT,
    content_hash     TEXT,            -- sha256 hex of normalized visible text
    simhash          BIGINT,          -- 64-bit simhash of visible text
    word_count       INT,
    render_mode      TEXT NOT NULL,   -- http | playwright
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    screenshot_key   TEXT,
    ato_screenshot_key TEXT,          -- above-the-fold
    html_key         TEXT,
    har_key          TEXT,
    extracted        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_pages_inv ON pages(investigation_id);
CREATE INDEX ix_pages_simhash ON pages(simhash);

-- =====================================================================
-- forms
-- =====================================================================
CREATE TABLE forms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id     UUID NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    action      TEXT,
    method      TEXT,
    fields      JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_login    BOOLEAN NOT NULL DEFAULT FALSE,
    is_payment  BOOLEAN NOT NULL DEFAULT FALSE,
    posts_cross_origin BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ix_forms_page ON forms(page_id);

-- =====================================================================
-- outlinks
-- =====================================================================
CREATE TABLE outlinks (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id            UUID NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    href               TEXT NOT NULL,
    rel                TEXT,
    anchor_text        TEXT,
    same_origin        BOOLEAN NOT NULL DEFAULT FALSE,
    registered_domain  TEXT
);
CREATE INDEX ix_outlinks_page ON outlinks(page_id);

-- =====================================================================
-- evidence (controlled vocabulary lives in app/crawler/vocabulary.py)
-- =====================================================================
CREATE TABLE evidence (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id UUID NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    analyzer         TEXT NOT NULL,   -- crawl | insite_search | template | phishing | reputation | infra
    kind             TEXT NOT NULL,   -- controlled vocabulary, see vocabulary.py
    severity         TEXT NOT NULL,   -- info | low | medium | high | critical
    confidence       NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    summary          TEXT NOT NULL,
    details          JSONB NOT NULL DEFAULT '{}'::jsonb,
    screenshot_key   TEXT,
    page_id          UUID REFERENCES pages(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_evidence_inv ON evidence(investigation_id);
CREATE INDEX ix_evidence_kind ON evidence(kind);
CREATE INDEX ix_evidence_severity ON evidence(severity);

-- =====================================================================
-- updated_at trigger for investigations
-- =====================================================================
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER investigations_touch
    BEFORE UPDATE ON investigations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
