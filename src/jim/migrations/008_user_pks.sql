-- Composite PKs (soft-baking-kettle plan, Phase 3). NOT safely re-runnable
-- from empty in an environment that never ran scripts/backfill_users.py: a
-- fresh dev/CI database has zero rows in these tables (no NULLs to violate
-- the PK), but a real deploy must run the backfill first so every existing
-- row has a user_id — Postgres will refuse to promote the PK otherwise,
-- which is the intended safety net, not a bug to work around.

ALTER TABLE kv DROP CONSTRAINT IF EXISTS kv_pkey;
ALTER TABLE kv ADD PRIMARY KEY (user_id, key);

ALTER TABLE garmin_daily DROP CONSTRAINT IF EXISTS garmin_daily_pkey;
ALTER TABLE garmin_daily ADD PRIMARY KEY (user_id, day);

ALTER TABLE garmin_activities DROP CONSTRAINT IF EXISTS garmin_activities_pkey;
ALTER TABLE garmin_activities ADD PRIMARY KEY (user_id, activity_id);
DROP INDEX IF EXISTS idx_garmin_activities_day;
CREATE INDEX IF NOT EXISTS idx_garmin_activities_user_day ON garmin_activities (user_id, day);

ALTER TABLE notion_daily_log DROP CONSTRAINT IF EXISTS notion_daily_log_pkey;
ALTER TABLE notion_daily_log ADD PRIMARY KEY (user_id, day);

ALTER TABLE exercise_sets DROP CONSTRAINT IF EXISTS exercise_sets_pkey;
ALTER TABLE exercise_sets ADD PRIMARY KEY (user_id, activity_id, set_index);

-- suggestions/outcomes keep their surrogate `id` PK — just index user_id.
DROP INDEX IF EXISTS idx_suggestions_for_date;
CREATE INDEX IF NOT EXISTS idx_suggestions_user_for_date ON suggestions (user_id, for_date);
CREATE INDEX IF NOT EXISTS idx_outcomes_user ON outcomes (user_id);
