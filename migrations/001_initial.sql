-- Vesper core schema (PLAN.md §6). Additive and idempotent: safe to re-run.
-- pgvector-dependent tables live in 002_research_corpus.sql.

CREATE TABLE IF NOT EXISTS garmin_daily (
    day             date PRIMARY KEY,
    hrv             real,
    sleep_hours     real,
    body_battery    int,
    readiness       int,
    resting_hr      int,
    raw             jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS garmin_activities (
    activity_id     text PRIMARY KEY,
    day             date NOT NULL,
    type            text NOT NULL,
    duration_min    real NOT NULL DEFAULT 0,
    training_load   real,
    summary         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_garmin_activities_day ON garmin_activities (day);

CREATE TABLE IF NOT EXISTS notion_daily_log (
    day             date PRIMARY KEY,
    pain_level      int,
    pain_location   text NOT NULL DEFAULT '',
    pt_done         boolean NOT NULL DEFAULT false,
    habits          jsonb NOT NULL DEFAULT '{}'::jsonb,
    day_score       int
);

CREATE TABLE IF NOT EXISTS features_daily (
    day                  date PRIMARY KEY,
    weekly_volume_min    real NOT NULL DEFAULT 0,
    muscle_group_balance jsonb NOT NULL DEFAULT '{}'::jsonb,
    days_since_legs      int,
    pain_trend           real NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suggestions (
    id              serial PRIMARY KEY,
    run_ts          timestamptz NOT NULL DEFAULT now(),
    for_date        date NOT NULL,
    plan            jsonb NOT NULL,
    rationale       text NOT NULL DEFAULT '',
    research_used   boolean NOT NULL DEFAULT false,
    model_tier      text NOT NULL DEFAULT 'fast'
);
CREATE INDEX IF NOT EXISTS idx_suggestions_for_date ON suggestions (for_date);

CREATE TABLE IF NOT EXISTS outcomes (
    id                   serial PRIMARY KEY,
    suggestion_id        int NOT NULL REFERENCES suggestions (id),
    actual_activity_id   text,
    adhered              boolean,
    notes                text NOT NULL DEFAULT '',
    reconciled_ts        timestamptz NOT NULL DEFAULT now()
);
