-- 重要工作独立于完成状态；历史与复盘备注长期保留。
ALTER TABLE bot.tasks ADD COLUMN is_important boolean NOT NULL DEFAULT false;
ALTER TABLE bot.tasks ADD COLUMN review_note text NOT NULL DEFAULT '';
CREATE INDEX tasks_important_user_idx ON bot.tasks (user_id, status, completed_at)
    WHERE is_important;
ALTER TABLE bot.digest_items DROP CONSTRAINT digest_items_section_check;
ALTER TABLE bot.digest_items ADD CONSTRAINT digest_items_section_check
    CHECK (section IN ('important', 'today', 'overdue', 'unscheduled'));

-- weekday 使用 Python 口径：周一 0，周日 6。首次加入重要工作时启用。
CREATE TABLE bot.work_review_settings (
    user_id text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    weekday integer NOT NULL DEFAULT 4 CHECK (weekday BETWEEN 0 AND 6),
    hour integer NOT NULL DEFAULT 20 CHECK (hour BETWEEN 0 AND 23),
    minute integer NOT NULL DEFAULT 0 CHECK (minute BETWEEN 0 AND 59),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bot.work_review_runs (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, scheduled_at)
);
