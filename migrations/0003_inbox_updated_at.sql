-- 0003_inbox_updated_at.sql — inbox 增加改为时间

BEGIN;

ALTER TABLE bot.inbox
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

COMMIT;
