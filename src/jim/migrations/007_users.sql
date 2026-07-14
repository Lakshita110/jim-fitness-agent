-- Multi-tenant foundations (soft-baking-kettle plan, Phase 1). Purely additive:
-- new tables plus nullable user_id columns everywhere else. Nothing reads or
-- writes user_id yet — that's Phase 3, once every path is scoped. Primary keys
-- stay untouched here; 008_user_pks.sql promotes them to composite keys after
-- a backfill guarantees every existing row has a user_id.

CREATE TABLE IF NOT EXISTS users (
    id              serial PRIMARY KEY,
    email           text NOT NULL UNIQUE,
    password_hash   text NOT NULL,
    created_ts      timestamptz NOT NULL DEFAULT now(),
    timezone        text NOT NULL DEFAULT 'America/New_York',
    nightly_enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id                 int PRIMARY KEY REFERENCES users(id),
    garmin_email            text,
    garmin_password_enc     bytea,   -- AES-GCM ciphertext, fallback re-auth
    garmin_tokens_enc       bytea,   -- encrypted session-token blob (primary auth path)
    notion_token_enc        bytea,
    notion_knee_log_db_id   text,
    updated_ts              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS playbooks (
    user_id      int PRIMARY KEY REFERENCES users(id),
    rotation     jsonb NOT NULL DEFAULT '[]'::jsonb,
    workouts     jsonb NOT NULL DEFAULT '{}'::jsonb,
    pt_routines  jsonb NOT NULL DEFAULT '{}'::jsonb,
    directives   text  NOT NULL DEFAULT '',
    updated_ts   timestamptz NOT NULL DEFAULT now()
);

-- Nullable for now; promoted to composite PKs in 008 after backfill (Phase 2).
ALTER TABLE kv                ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE garmin_daily      ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE garmin_activities ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE notion_daily_log  ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE features_daily    ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE suggestions       ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE outcomes          ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
ALTER TABLE exercise_sets     ADD COLUMN IF NOT EXISTS user_id int REFERENCES users(id);
