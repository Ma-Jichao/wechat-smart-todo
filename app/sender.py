"""统一的发送协程。

职责（对应第 4.3、4.4 节）：

  * 原子领取到期投递并设租约，发送时**不持有长事务**。
  * 全局限速：所有出站消息共用同一条队列与同一个节流窗口。
  * 错误分类：网络/服务端暂时故障指数退避 + 随机扰动；限流走账号维度共享冷却；
    凭据失效暂停队列；context 未就绪进入 blocked_context 且**不消耗重试次数**；
    请求格式类永久错误停止无意义重试。
  * 到期租约可回收，不留永久卡在 sending 的记录。
  * accepted 只表示接口接受，不改事项状态，也不宣称用户已收到。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import format
from .config import DeliveryConfig, LimitsConfig, WeixinConfig
from .db import Database
from .tools import SHANGHAI, now_shanghai
from .weixin import Sender, SessionExpired, classify_error

log = logging.getLogger(__name__)

# 晚于原定时间这么久才发出去的到点提醒，要注明是补发
LATE_REMINDER_AFTER = timedelta(minutes=15)

CLAIM_SQL = """
UPDATE bot.deliveries AS d
SET status = 'sending',
    lease_until = now() + make_interval(secs => %s),
    updated_at = now()
WHERE d.id = (
    SELECT id FROM bot.deliveries
    WHERE status IN ('pending', 'retry_wait')
      AND next_attempt_at <= now()
    ORDER BY next_attempt_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING d.id, d.user_id, d.kind, d.task_id, d.task_version, d.digest_id,
          d.segment_index, d.segment_total, d.business_key, d.attempts,
          d.content_snapshot, d.scheduled_at
"""

RECLAIM_SQL = """
UPDATE bot.deliveries
SET status = 'retry_wait',
    next_attempt_at = now(),
    last_error = COALESCE(last_error, '发送租约过期，结果不确定'),
    error_class = COALESCE(error_class, 'transient'),
    updated_at = now()
WHERE status = 'sending' AND lease_until < now()
RETURNING id
"""


class DeliverySender:
    def __init__(
        self,
        db: Database,
        weixin_cfg: WeixinConfig,
        delivery_cfg: DeliveryConfig,
        limits: LimitsConfig,
    ) -> None:
        self._db = db
        self._weixin = weixin_cfg
        self._cfg = delivery_cfg
        self._limits = limits
        self._sender = Sender(weixin_cfg)
        self._next_send_at = 0.0
        self._cooldown_until = 0.0
        self._stopped = False
        self.paused_for_auth = False

    # ── 状态 ─────────────────────────────────────────────────────────────
    async def context_token(self) -> str | None:
        row = await self._db.fetch_one(
            "SELECT context_token FROM bot.bot_state WHERE account_id = %s",
            (self._weixin.account_id,),
        )
        return (row or {}).get("context_token")

    async def requeue_blocked_context(self) -> int:
        """收到入站消息后解除 blocked_context。由收消息侧调用。"""
        return await self._db.execute(
            """
            UPDATE bot.deliveries
            SET status = 'pending', next_attempt_at = now(),
                last_error = NULL, error_class = NULL, updated_at = now()
            WHERE status = 'blocked_context'
            """
        )

    async def block_context(self, delivery_id: int, reason: str) -> None:
        await self._db.execute(
            """
            UPDATE bot.deliveries
            SET status = 'blocked_context', last_error = %s, error_class = 'context',
                updated_at = now()
            WHERE id = %s
            """,
            (reason[:500], delivery_id),
        )

    # ── 领取与回收 ───────────────────────────────────────────────────────
    async def reclaim_expired(self) -> int:
        rows = await self._db.fetch_all(RECLAIM_SQL)
        if rows:
            log.warning("回收 %d 条租约过期的投递（结果不确定，按保守规则重试）", len(rows))
        return len(rows)

    async def claim_one(self) -> dict[str, Any] | None:
        async with self._db.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(CLAIM_SQL, (self._cfg.lease_secs,))
                return await cur.fetchone()

    # ── 结果回写 ─────────────────────────────────────────────────────────
    async def _finish_accepted(self, delivery: dict[str, Any], result: dict[str, Any]) -> None:
        await self._db.execute(
            """
            UPDATE bot.deliveries
            SET status = 'accepted', attempts = attempts + 1, accepted_at = now(),
                last_attempt_at = now(), api_result = %s, last_error = NULL,
                error_class = NULL, lease_until = NULL, updated_at = now()
            WHERE id = %s
            """,
            (Jsonb(result), delivery["id"]),
        )

    async def _finish_failed(
        self, delivery: dict[str, Any], error_class: str, reason: str, status: str,
        delay_secs: float | None,
    ) -> None:
        attempts = delivery["attempts"] + 1
        if status == "retry_wait" and delay_secs is not None:
            await self._db.execute(
                """
                UPDATE bot.deliveries
                SET status = 'retry_wait', attempts = %s, last_attempt_at = now(),
                    next_attempt_at = now() + make_interval(secs => %s),
                    last_error = %s, error_class = %s, lease_until = NULL, updated_at = now()
                WHERE id = %s
                """,
                (attempts, delay_secs, reason[:500], error_class, delivery["id"]),
            )
        else:
            await self._db.execute(
                """
                UPDATE bot.deliveries
                SET status = %s, attempts = %s, last_attempt_at = now(),
                    last_error = %s, error_class = %s, lease_until = NULL, updated_at = now()
                WHERE id = %s
                """,
                (status, attempts, reason[:500], error_class, delivery["id"]),
            )

    def _backoff(self, attempts: int) -> float:
        raw = self._cfg.backoff_base_secs * (2 ** max(attempts - 1, 0))
        capped = min(raw, self._cfg.backoff_cap_secs)
        jitter = capped * self._cfg.backoff_jitter_ratio
        return max(1.0, capped + random.uniform(-jitter, jitter))

    # ── 发送一条 ─────────────────────────────────────────────────────────
    async def deliver_one(self, delivery: dict[str, Any]) -> None:
        token = await self.context_token()
        if not token:
            log.warning("没有 context_token，投递 #%s 进入 blocked_context", delivery["id"])
            await self.block_context(delivery["id"], "缺少 context_token，等待用户先发消息")
            return

        now_mono = time.monotonic()
        wait = max(self._next_send_at - now_mono, 0.0)
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_send_at = time.monotonic() + self._cfg.min_interval_secs

        # Throttle may yield to the processor. Recheck immediately before sending:
        # a task completed or edited during that wait must not emit a stale reminder.
        current = await self._db.fetch_one(
            "SELECT d.status, t.status AS task_status, t.version AS current_version "
            "FROM bot.deliveries d LEFT JOIN bot.tasks t ON t.id = d.task_id "
            "WHERE d.id = %s", (delivery["id"],),
        )
        if not current or current["status"] != "sending":
            return
        if delivery.get("task_id") is not None and (
            current["task_status"] != "open" or
            current["current_version"] != delivery["task_version"]
        ):
            await self._db.execute(
                "UPDATE bot.deliveries SET status = 'superseded', lease_until = NULL, "
                "updated_at = now() WHERE id = %s AND status = 'sending'",
                (delivery["id"],),
            )
            return

        text = delivery.get("content_snapshot") or ""
        scheduled = delivery.get("scheduled_at")
        if delivery["kind"] == "reminder":
            # 提醒类投递只存原始事项内容，排版在这里统一做，
            # 这样「是否补发」这个只在发送时才知道的信息才能正确体现。
            is_late = (
                scheduled is not None
                and now_shanghai() - scheduled.astimezone(SHANGHAI) > LATE_REMINDER_AFTER
            )
            text = format.reminder(
                text,
                scheduled.astimezone(SHANGHAI) if scheduled else now_shanghai(),
                late=is_late,
            )

        try:
            result = await asyncio.wait_for(
                self._sender.send_text(delivery["user_id"], token, text),
                timeout=self._cfg.send_timeout_secs,
            )
        except SessionExpired as exc:
            log.error("凭据失效，暂停全部投递：%s", exc)
            self.paused_for_auth = True
            await self._finish_failed(delivery, "auth", str(exc), "blocked_auth", None)
            return
        except asyncio.TimeoutError:
            # 超时不等于没发出去，按保守规则退避重试
            log.warning("投递 #%s 超时，结果不确定", delivery["id"])
            await self._finish_failed(
                delivery, "transient", "请求超时，结果不确定",
                "permanent_failed" if delivery["attempts"] + 1 >= self._cfg.max_attempts else "retry_wait",
                self._backoff(delivery["attempts"] + 1),
            )
            return
        except Exception as exc:
            error_class, reason = classify_error(exc)
            attempts = delivery["attempts"] + 1

            if error_class == "context":
                log.info("通道未就绪（%s），投递 #%s 转入 blocked_context，不消耗重试次数",
                         reason, delivery["id"])
                await self.block_context(delivery["id"], reason)
                return
            if error_class == "rate_limit":
                self._cooldown_until = time.monotonic() + self._cfg.rate_limit_cooldown_secs
                log.warning("被限流，账号维度冷却 %ss", self._cfg.rate_limit_cooldown_secs)
                await self._finish_failed(
                    delivery, "rate_limit", reason, "retry_wait",
                    max(self._backoff(attempts), self._cfg.rate_limit_cooldown_secs),
                )
                return
            if error_class == "auth":
                self.paused_for_auth = True
                await self._finish_failed(delivery, "auth", reason, "blocked_auth", None)
                return
            if error_class == "permanent":
                await self._finish_failed(delivery, "permanent", reason, "permanent_failed", None)
                return

            if attempts >= self._cfg.max_attempts:
                log.error("投递 #%s 达到最大尝试次数 %s，转入 permanent_failed（记录保留，待办不删除）",
                          delivery["id"], attempts)
                await self._finish_failed(
                    delivery, "transient", f"重试 {attempts} 次仍失败：{reason}",
                    "permanent_failed", None,
                )
                return
            await self._finish_failed(
                delivery, "transient", reason, "retry_wait", self._backoff(attempts)
            )
            return

        await self._finish_accepted(delivery, result if isinstance(result, dict) else {})
        log.info("投递 #%s 已被接口接受（kind=%s 第 %s/%s 段）",
                 delivery["id"], delivery["kind"],
                 delivery["segment_index"], delivery["segment_total"])

    # ── 主循环 ───────────────────────────────────────────────────────────
    async def run(self, poll_interval: float = 1.0) -> None:
        last_reclaim = 0.0
        while not self._stopped:
            try:
                if self.paused_for_auth:
                    await asyncio.sleep(30)
                    continue

                now_mono = time.monotonic()
                if now_mono < self._cooldown_until:
                    await asyncio.sleep(min(self._cooldown_until - now_mono, 5.0))
                    continue

                if now_mono - last_reclaim > 30:
                    await self.reclaim_expired()
                    last_reclaim = now_mono

                delivery = await self.claim_one()
                if delivery is None:
                    await asyncio.sleep(poll_interval)
                    continue
                await self.deliver_one(delivery)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("发送协程本轮异常，1s 后继续")
                await asyncio.sleep(1.0)

    def stop(self) -> None:
        self._stopped = True


async def pending_summary(db: Database) -> dict[str, Any]:
    """给运行状态查看用：区分「没生成 / 没领取 / 接口拒绝 / 接口超时 / 接口接受」。"""
    row = await db.fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE status = 'pending')      AS pending,
          count(*) FILTER (WHERE status = 'retry_wait')   AS retry_wait,
          count(*) FILTER (WHERE status = 'sending')      AS sending,
          count(*) FILTER (WHERE status = 'blocked_context') AS blocked_context,
          count(*) FILTER (WHERE status = 'blocked_auth') AS blocked_auth,
          count(*) FILTER (WHERE status = 'permanent_failed') AS permanent_failed,
          count(*) FILTER (WHERE status = 'accepted')     AS accepted,
          min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry_wait'))
              AS oldest_due
        FROM bot.deliveries
        """
    )
    return dict(row or {})
