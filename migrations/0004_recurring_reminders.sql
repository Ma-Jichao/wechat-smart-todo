-- 0004_recurring_reminders.sql — 每天重复的打卡类提醒
--
-- 需求：每天固定几个时间点发提醒（如打卡），法定节假日（含调休）除外。
-- 建模：tasks 增加 repeat_times（每天的时间点列表，HH:MM）+ skip_holidays；
--       投递新增 recurring 类型，不套用到点提醒的「完成」文案。

BEGIN;

ALTER TABLE bot.tasks
    ADD COLUMN IF NOT EXISTS repeat_times text[] NOT NULL DEFAULT '{}';

ALTER TABLE bot.tasks
    ADD COLUMN IF NOT EXISTS skip_holidays boolean NOT NULL DEFAULT false;

ALTER TABLE bot.deliveries DROP CONSTRAINT IF EXISTS deliveries_kind_check;
ALTER TABLE bot.deliveries ADD CONSTRAINT deliveries_kind_check
    CHECK (kind IN ('reminder', 'digest', 'reply', 'notice', 'recurring'));

COMMIT;
