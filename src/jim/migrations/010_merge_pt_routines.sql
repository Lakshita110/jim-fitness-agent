-- Simplify the playbook: pt_routines was a whole parallel structure next to
-- workouts+rotation for no structural reason (see playbook.py's docstring).
-- Merge its templates into workouts (both are jsonb objects keyed by
-- template key) before dropping the now-redundant column.
--
-- migrate() re-runs every file on every startup (no version table), so this
-- has to tolerate running after the column is already gone -- guard the merge
-- on the column still existing rather than letting the second run 500 on
-- UndefinedColumn.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'playbooks' AND column_name = 'pt_routines'
    ) THEN
        UPDATE playbooks SET workouts = workouts || pt_routines
        WHERE pt_routines <> '{}'::jsonb;

        ALTER TABLE playbooks DROP COLUMN pt_routines;
    END IF;
END $$;
