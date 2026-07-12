-- Research corpus (M4) — requires the pgvector extension (Render Postgres has
-- it; local dev without it can skip this file and everything but
-- research_training still works).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS research_corpus (
    id          serial PRIMARY KEY,
    source      text NOT NULL,
    title       text NOT NULL,
    chunk_text  text NOT NULL,
    embedding   vector(1536),
    tags        text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_research_corpus_source ON research_corpus (source);
