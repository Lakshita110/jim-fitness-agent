-- Replaces the playbook's template/rotation machinery for the MCP-based
-- coach: one small per-user record of standing knee/ankle limits, safety
-- rules, and goals, read by Claude at the start of reasoning and edited via
-- the get_constraints/set_constraints MCP tools. No workouts/rotation here —
-- those live in Garmin's own workout library now.

CREATE TABLE IF NOT EXISTS constraints (
    user_id     int PRIMARY KEY REFERENCES users(id),
    content     text NOT NULL DEFAULT '',
    updated_ts  timestamptz NOT NULL DEFAULT now()
);
