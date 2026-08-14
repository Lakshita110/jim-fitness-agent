-- The chat/playbook path (coach.py, playbook.py, agent/validate.py) is
-- retired now that the Garmin MCP path is verified end-to-end. Named
-- workouts live in Garmin's own library and the one remaining piece of
-- Jim-side state is the constraints table (011_constraints.sql); nothing
-- reads or writes playbooks anymore. Leave 001/005/006/007/008/010
-- untouched (append-only, never edited).

DROP TABLE IF EXISTS playbooks;
