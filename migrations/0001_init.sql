-- 0001_init.sql — 微信提醒系统初始表结构
--
-- 设计依据：需求文档第 4.1 节「数据模型」。
-- 应用连接使用 reminder 角色（非超级用户），全部对象建在 bot schema 下。
--
-- 关键设计约束：
--   1. 事项状态与投递状态分离：发过消息不等于事情完成，发送失败也不改变事项是否待办。
--   2. scheduled_at（原定提醒时间）与 next_attempt_at（下次尝试时间）分开，重试不篡改原定时间。
--   3. 查询只命中明确的可发送状态，禁止 status <> 'sent' 这类条件。
--   4. 所有时间点用 timestamptz，业务日界在查询时按 Asia/Shanghai 换算。

BEGIN;

CREATE SCHEMA IF NOT EXISTS bot;

-- ---------------------------------------------------------------------------
-- 事项
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.tasks (
    id                bigserial PRIMARY KEY,
    user_id           text        NOT NULL,
    content           text        NOT NULL CHECK (char_length(content) BETWEEN 1 AND 2000),
    status            text        NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'completed', 'canceled')),
    -- 可空提醒时间：NULL 表示「先记着，不用定时提醒」，在早报单独分组
    remind_at         timestamptz,
    -- 解析依据：模型根据哪个原句、按哪个时区解释出的时间，便于失败重试时保持原意
    remind_at_phrase  text,
    remind_at_tz      text        NOT NULL DEFAULT 'Asia/Shanghai',
    version           integer     NOT NULL DEFAULT 1 CHECK (version > 0),
    source_inbox_id   bigint,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    canceled_at       timestamptz,
    CONSTRAINT tasks_completed_consistency CHECK (
        (status = 'completed') = (completed_at IS NOT NULL)
        AND (status = 'canceled') = (canceled_at IS NOT NULL)
    )
);

-- 到期扫描与早报查询走这里
CREATE INDEX IF NOT EXISTS tasks_due_idx
    ON bot.tasks (remind_at) WHERE status = 'open' AND remind_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_user_status_idx
    ON bot.tasks (user_id, status);

-- ---------------------------------------------------------------------------
-- 接收消息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.inbox (
    id             bigserial PRIMARY KEY,
    account_id     text        NOT NULL,
    message_id     text        NOT NULL,
    seq            bigint,
    from_user_id   text,
    raw            jsonb       NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now(),
    -- 接收去重不等于业务完成，所以状态机要能区分「待处理 / 处理中 / 已完成 / 待重试」
    status         text        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'processing', 'done', 'retry', 'manual')),
    attempts       integer     NOT NULL DEFAULT 0,
    lease_until    timestamptz,
    last_error     text,
    processed_at   timestamptz,
    CONSTRAINT inbox_account_message_key UNIQUE (account_id, message_id)
);

CREATE INDEX IF NOT EXISTS inbox_claim_idx
    ON bot.inbox (status, received_at) WHERE status IN ('pending', 'retry', 'processing');

-- ---------------------------------------------------------------------------
-- 投递（outbox）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.deliveries (
    id               bigserial PRIMARY KEY,
    user_id          text        NOT NULL,
    kind             text        NOT NULL
                     CHECK (kind IN ('reminder', 'digest', 'reply', 'notice')),
    task_id          bigint REFERENCES bot.tasks (id),
    task_version     integer,
    digest_id        bigint,
    segment_index    integer,
    segment_total    integer,
    -- 幂等业务键：同一逻辑消息重试保持同一个键，不因重复提交产生新投递
    business_key     text        NOT NULL,
    -- 原定时间，仅由调度决定，重试路径绝不修改它
    scheduled_at     timestamptz NOT NULL,
    -- 下次尝试时间，只有它随退避变化
    next_attempt_at  timestamptz NOT NULL,
    status           text        NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'sending', 'retry_wait', 'accepted',
                                       'blocked_auth', 'permanent_failed', 'canceled',
                                       'superseded')),
    attempts         integer     NOT NULL DEFAULT 0,
    lease_until      timestamptz,
    last_error       text,
    error_class      text,
    api_result       jsonb,
    content_snapshot text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    accepted_at      timestamptz,
    CONSTRAINT deliveries_business_key_uniq UNIQUE (business_key)
);

-- 只扫明确的可发送状态；sending 单独的索引用于租约回收
CREATE INDEX IF NOT EXISTS deliveries_ready_idx
    ON bot.deliveries (next_attempt_at)
    WHERE status IN ('pending', 'retry_wait');
CREATE INDEX IF NOT EXISTS deliveries_lease_idx
    ON bot.deliveries (lease_until) WHERE status = 'sending';
CREATE INDEX IF NOT EXISTS deliveries_task_idx
    ON bot.deliveries (task_id, status);

-- ---------------------------------------------------------------------------
-- 早报与条目映射
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.digests (
    id            bigserial PRIMARY KEY,
    user_id       text        NOT NULL,
    local_date    date        NOT NULL,
    generated_at  timestamptz NOT NULL DEFAULT now(),
    refreshed_at  timestamptz,
    CONSTRAINT digests_user_date_uniq UNIQUE (user_id, local_date)
);

CREATE TABLE IF NOT EXISTS bot.digest_items (
    digest_id  bigint  NOT NULL REFERENCES bot.digests (id) ON DELETE CASCADE,
    position   integer NOT NULL,
    task_id    bigint  NOT NULL REFERENCES bot.tasks (id),
    -- 分组：今天 / 逾期 / 未设提醒时间
    section    text    NOT NULL CHECK (section IN ('today', 'overdue', 'unscheduled')),
    -- 该条目是否由到点提醒合并而来（08:00 同时到点的情况）
    merged_from_reminder boolean NOT NULL DEFAULT false,
    PRIMARY KEY (digest_id, position)
);

CREATE INDEX IF NOT EXISTS digest_items_task_idx ON bot.digest_items (task_id);

-- ---------------------------------------------------------------------------
-- Bot 状态：接收游标与 context_token 的真相源
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.bot_state (
    account_id     text        PRIMARY KEY,
    user_id        text,
    cursor         text        NOT NULL DEFAULT '',
    context_token  text,
    context_updated_at timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 对话状态：待澄清内容与有限期上下文
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.conversations (
    user_id      text        PRIMARY KEY,
    state        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    expires_at   timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot.messages_seen (
    id          bigserial PRIMARY KEY,
    user_id     text        NOT NULL,
    direction   text        NOT NULL CHECK (direction IN ('in', 'out')),
    body        text        NOT NULL,
    task_ids    bigint[]    NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_seen_user_idx
    ON bot.messages_seen (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 单实例租约：防止部署误启动第二个消费者
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.runtime_lease (
    lease_name  text        PRIMARY KEY,
    owner       text        NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- 变更审计：修改前后关键字段
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot.task_changes (
    id            bigserial PRIMARY KEY,
    task_id       bigint      NOT NULL REFERENCES bot.tasks (id),
    changed_at    timestamptz NOT NULL DEFAULT now(),
    field         text        NOT NULL,
    old_value     text,
    new_value     text,
    source_inbox_id bigint,
    reason        text
);

CREATE INDEX IF NOT EXISTS task_changes_task_idx ON bot.task_changes (task_id, changed_at);

COMMIT;
