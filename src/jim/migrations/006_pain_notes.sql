-- Pain notes were read live from Notion but never persisted, so Jim could trend
-- pain *numbers* over the window while being blind to the words ("wrists still
-- poor" three days running). Keep the notes so pain history has content.
ALTER TABLE notion_daily_log
    ADD COLUMN IF NOT EXISTS pain_notes text NOT NULL DEFAULT '';
