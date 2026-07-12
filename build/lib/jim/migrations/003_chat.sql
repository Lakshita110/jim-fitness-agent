-- Jim's chat state: one tiny key-value store (single user, single thread).
-- Keys in use: 'chat_history' (last ~30 messages), 'draft' (working plan),
-- 'goals' (plain-text long-term goals block), 'state' (cached day snapshot).
CREATE TABLE IF NOT EXISTS kv (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_ts  timestamptz NOT NULL DEFAULT now()
);

-- Where a suggestion came from: 'nightly' cron or 'chat' approval.
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'nightly';
