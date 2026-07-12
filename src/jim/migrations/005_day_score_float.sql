-- Notion's `day score` is a FORMULA that returns a fraction (e.g. 0.5), but the
-- column was int and the parser coerced with int() — silently truncating every
-- partial day to 0. Widen the column so fractional scores survive.
--
-- Idempotent: ALTER ... TYPE double precision is a no-op if it already is one.
ALTER TABLE notion_daily_log
    ALTER COLUMN day_score TYPE double precision USING day_score::double precision;
