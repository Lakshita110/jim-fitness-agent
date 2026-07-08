-- Per-set strength performance pulled from Garmin activities (ACTIVE sets
-- only): what was actually lifted, so Jim can progress weights/reps from
-- real history rather than guesses.
CREATE TABLE IF NOT EXISTS exercise_sets (
    activity_id   text NOT NULL,
    set_index     int NOT NULL,
    day           date NOT NULL,
    category      text,
    exercise_name text,
    reps          int,
    weight_kg     real,
    duration_sec  real,
    PRIMARY KEY (activity_id, set_index)
);
CREATE INDEX IF NOT EXISTS idx_exercise_sets_lookup
    ON exercise_sets (category, exercise_name, day);
