-- Notion integration removed. Drop its table and the credential columns it
-- used; leave 001/005/006/007/008 untouched (append-only, never edited).

DROP TABLE IF EXISTS notion_daily_log;

ALTER TABLE user_credentials DROP COLUMN IF EXISTS notion_token_enc;
ALTER TABLE user_credentials DROP COLUMN IF EXISTS notion_knee_log_db_id;
