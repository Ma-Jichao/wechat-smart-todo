-- 0002_delivery_states.sql — 补充投递状态与诊断字段
--
-- 背景：实测发现 iLink 的主动推送以「近期有过入站消息」为前提，
-- 在此之前 sendmessage 返回 ret=-2 prepare failed。这不是失败，而是通道暂时不可用，
-- 既不该消耗重试次数，也不该被当成凭据失效（blocked_auth）。
-- 因此单独增加一个 blocked_context 状态，收到下一次入站消息后由程序解封。

BEGIN;

ALTER TABLE bot.deliveries DROP CONSTRAINT IF EXISTS deliveries_status_check;
ALTER TABLE bot.deliveries ADD CONSTRAINT deliveries_status_check
    CHECK (status IN ('pending', 'sending', 'retry_wait', 'accepted',
                      'blocked_auth', 'blocked_context', 'permanent_failed',
                      'canceled', 'superseded'));

-- 记录真正发起 HTTP 请求的时刻。attempts 只在真的发出去之后才计数，
-- 这样 blocked_context 不会把重试次数耗光。
ALTER TABLE bot.deliveries
    ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz;

-- 解封 blocked_context 时按时间顺序取，建个部分索引
CREATE INDEX IF NOT EXISTS deliveries_blocked_context_idx
    ON bot.deliveries (next_attempt_at) WHERE status = 'blocked_context';

COMMIT;
