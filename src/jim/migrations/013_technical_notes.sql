-- A shared, cross-user log of operational/tool-usage mistakes Claude catches
-- while coaching (a Garmin API quirk, an exercise-matching miss, a confusing
-- tool response) — deliberately separate from research_corpus (scientific
-- training literature) and constraints (one athlete's own limits). Written
-- by the MCP tool report_technical_issue, read by get_technical_notes; see
-- mcp_server.py and skills/jim-coach/SKILL.md.
CREATE TABLE IF NOT EXISTS technical_notes (
    id                   serial PRIMARY KEY,
    title                text NOT NULL,
    note                 text NOT NULL,
    tags                 text[] NOT NULL DEFAULT '{}',
    reported_by_user_id  integer REFERENCES users(id) ON DELETE SET NULL,
    created_ts           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_technical_notes_tags ON technical_notes USING GIN (tags);
