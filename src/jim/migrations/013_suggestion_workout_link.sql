-- Plan-vs-actual for the MCP path: every write that puts a workout on the
-- Garmin calendar records a `suggestions` row (source 'mcp'), linked to the
-- Garmin workout it scheduled so later edits/unschedules/deletes can find
-- it. `cancelled` marks a plan the athlete took off the calendar, so the
-- week overview doesn't count it as missed.

ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS workout_id text;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS cancelled boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_suggestions_user_workout ON suggestions (user_id, workout_id);
