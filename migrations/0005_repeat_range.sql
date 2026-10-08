-- 0005_repeat_range.sql — 每天重复提醒支持起止日期
--
-- 需求：「从 10月1日 到 6日 每天 08:00 提醒」这类有起止日期的每日提醒。
-- 此前 repeat_times 只表达「无限期每天」，模型会把带区间的说法拆成多个一次性事项，
-- 未完成时逐日进入早报被误标逾期。这里增加可选起止日，让它落在一条重复事项上。

BEGIN;

ALTER TABLE bot.tasks
    ADD COLUMN IF NOT EXISTS repeat_start date;

ALTER TABLE bot.tasks
    ADD COLUMN IF NOT EXISTS repeat_end date;

COMMIT;
