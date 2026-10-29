"""调度协程：到点提醒的兜底、每天 08:00 早报、逾期保留。

设计要点：

  * 到点提醒本身在创建事项时就已经写进 deliveries，发送协程到期自然会取。
    这里只做**兜底**：凡是「open、有提醒时间、时间已过、却没有对应投递记录」的事项，
    补一条（迁移进来的旧事项、任何历史缺口都靠这个覆盖）。
    已经发送过（accepted）的提醒**不会**被重新入队——逾期事项每天进早报，
    不每隔几分钟再催促一次。
  * 早报按 (user_id, local_date) 唯一。每条早报只生成一次，重试属于同一份的投递尝试。
  * 逾期保留不需要额外机制：早报每次都从「当前 open 的事项」现算，
    所以只要没完成/取消，就会持续出现。
  * 停机恢复：晚于当天 08:00 启动且还在补发窗口内 → 补发当天一份并标注延迟；
    超出窗口 → 当天不补发（记日志），事项仍会进入下一天早报。跨多日只生成当天那份。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from chinese_calendar import is_workday

from . import flavor, format
from .config import DigestConfig, LimitsConfig, StyleConfig, WeixinConfig
from .db import Database
from .important import ensure_weekly_review
from .outbox import digest_business_key, enqueue_text, recurring_business_key, reminder_business_key
from .tools import SHANGHAI, STATUS_OPEN, Task, group_for_digest, list_tasks, now_shanghai

log = logging.getLogger(__name__)

# 08:00 前后这个窗口内到点的事项合并进早报，不另发同内容提醒
MERGE_WINDOW = timedelta(minutes=10)
# 晚于原定时间这么久才发出去的到点提醒，要注明是补发
LATE_REMINDER_AFTER = timedelta(minutes=15)


class Scheduler:
    def __init__(
        self,
        db: Database,
        weixin_cfg: WeixinConfig,
        digest_cfg: DigestConfig,
        limits: LimitsConfig,
        style_cfg: StyleConfig | None = None,
        composer: Any = None,
    ) -> None:
        self._db = db
        self._weixin = weixin_cfg
        self._cfg = digest_cfg
        self._limits = limits
        self._style = style_cfg or StyleConfig()
        self._composer = composer
        self._stopped = False

    # ── 到点提醒兜底 ─────────────────────────────────────────────────────
    async def ensure_missing_reminders(self) -> int:
        """为已到期但没有投递记录的 open 事项补一条投递。幂等。"""
        rows = await self._db.fetch_all(
            """
            SELECT t.* FROM bot.tasks t
            WHERE t.user_id = %s
              AND t.status = 'open'
              AND t.remind_at IS NOT NULL
              AND t.remind_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM bot.deliveries d
                  WHERE d.task_id = t.id AND d.kind = 'reminder'
              )
            """,
            (self._weixin.allowed_user_id,),
        )
        if not rows:
            return 0

        created = 0
        for row in rows:
            async with self._db.transaction() as cur:
                await cur.execute(
                    """
                    INSERT INTO bot.deliveries
                        (user_id, kind, task_id, task_version, business_key,
                         scheduled_at, next_attempt_at, content_snapshot)
                    VALUES (%s, 'reminder', %s, %s, %s, %s, now(), %s)
                    ON CONFLICT (business_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        row["user_id"], row["id"], row["version"],
                        reminder_business_key(row["id"], row["version"]),
                        row["remind_at"], row["content"],
                    ),
                )
                if await cur.fetchone():
                    created += 1
        if created:
            log.info("兜底补建 %d 条到点提醒投递", created)
        return created

    # ── 每天重复的提醒（打卡类） ──────────────────────────────────────────
    def _is_workday(self, day: date) -> bool:
        """工作日（周一至五 + 调休补班，排除法定节假日与普通周末）。"""
        return is_workday(day)

    def _moment_at(self, day: date, hhmm: str) -> datetime | None:
        """把 'HH:MM' 解析成当天带时区的时间。格式不对返回 None。"""
        try:
            hour, minute = hhmm.split(":")
            return datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=SHANGHAI)
        except (ValueError, AttributeError):
            log.warning("重复提醒时间格式不对，跳过：%r", hhmm)
            return None

    async def ensure_recurring_reminders(self) -> int:
        """为「每天重复」的任务生成今天的到点投递。幂等。

        只在工作日生成（skip_holidays 时）；错过超过补发窗口的时间点不补发。
        """
        tasks = await self._db.fetch_all(
            "SELECT * FROM bot.tasks WHERE user_id = %s AND status = 'open' "
            "AND repeat_times <> '{}'",
            (self._weixin.allowed_user_id,),
        )
        if not tasks:
            return 0

        now = now_shanghai()
        today = now.date()
        created = 0
        for row in tasks:
            if row["skip_holidays"] and not self._is_workday(today):
                continue
            start, end = row.get("repeat_start"), row.get("repeat_end")
            if start is not None and today < start:
                continue
            if end is not None and today > end:
                continue
            for hhmm in (row["repeat_times"] or []):
                moment = self._moment_at(today, hhmm)
                if moment is None or moment < now - LATE_REMINDER_AFTER:
                    continue
                business_key = recurring_business_key(row["id"], today, hhmm)
                existing = await self._db.fetch_one(
                    "SELECT id, status, task_version FROM bot.deliveries WHERE business_key = %s", (business_key,))
                if existing:
                    if existing["status"] == "superseded" and existing["task_version"] != row["version"]:
                        # Keep the date/time key stable, but repair an unsent occurrence
                        # whose content changed. Already accepted occurrences stay sent.
                        created += await self._db.execute(
                            "UPDATE bot.deliveries SET task_version = %s, status = 'pending', "
                            "content_snapshot = %s, attempts = 0, last_error = NULL, "
                            "error_class = NULL, next_attempt_at = %s, updated_at = now() "
                            "WHERE id = %s AND status = 'superseded' AND task_version <> %s",
                            (row["version"], format.recurring(row["content"]), moment,
                             existing["id"], row["version"]),
                        )
                    continue
                async with self._db.transaction() as cur:
                    await cur.execute(
                        """
                        INSERT INTO bot.deliveries
                            (user_id, kind, task_id, task_version, business_key,
                             scheduled_at, next_attempt_at, content_snapshot)
                        VALUES (%s, 'recurring', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (business_key) DO NOTHING
                        RETURNING id
                        """,
                        (
                            row["user_id"], row["id"], row["version"],
                            business_key, moment, moment, format.recurring(row["content"]),
                        ),
                    )
                    if await cur.fetchone():
                        created += 1
        if created:
            log.info("为重复提醒生成 %d 条今日到点投递", created)
        return created

    # ── 早报 ─────────────────────────────────────────────────────────────
    async def _digest_row(self, local_date: date) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM bot.digests WHERE user_id = %s AND local_date = %s",
            (self._weixin.allowed_user_id, local_date),
        )

    def _scheduled_moment(self, local_date: date) -> datetime:
        return datetime(
            local_date.year, local_date.month, local_date.day,
            self._cfg.hour, self._cfg.minute, tzinfo=SHANGHAI,
        )

    def _should_generate(self, now: datetime) -> date | None:
        """判断现在该不该生成早报，返回要生成哪一天的（本地日期）。"""
        today = now.date()
        moment = self._scheduled_moment(today)
        if now < moment:
            return None
        if now > moment + timedelta(hours=self._cfg.digest_grace_hours):
            # 超出补发窗口：当天不补发，避免深夜收到「今日待办」
            return None
        return today

    def _build_digest_text(
        self,
        groups: dict[str, list[Task]],
        generated_at: datetime,
        late: bool,
        *,
        commentary: str | None = None,
        notes: dict[int, str] | None = None,
        closing: str | None = None,
    ) -> tuple[str, list[Task]]:
        return format.digest(groups, generated_at, late,
                             commentary=commentary, notes=notes, closing=closing)

    async def _compose_flavor(
        self, groups: dict[str, list[Task]], reference: datetime, generated_at: datetime
    ) -> tuple[str | None, dict[int, str], str | None]:
        """组织早报的「温度」层。失败一律退回确定性版本，绝不阻塞投递。"""
        style = self._style
        code_opening = flavor.build_opening(groups, generated_at)
        # 逾期天数由清单行尾的括注承担，这里不再叠加一条会重复的确定性点评
        notes: dict[int, str] = {}

        if not (style.greeting or style.per_item_note or style.closing):
            return code_opening, notes, None
        if self._composer is None or not self._composer.enabled:
            return code_opening, notes, None

        ordered = [t for key in ("important", "today", "overdue", "unscheduled")
                   for t in (groups.get(key) or [])]
        got = await self._composer.compose(groups, generated_at, [t.id for t in ordered])
        if style.greeting and got.opening:
            code_opening = f"{code_opening}\n{got.opening}"
        if style.per_item_note and got.notes:
            notes.update(got.notes)
        return code_opening, notes, (got.closing if style.closing else None)

    async def generate_digest(self, local_date: date, *, late: bool) -> int:
        """生成当天早报并入库。已存在则刷新未发送部分（已发送的内容不重复生成）。

        分组基准是该日 **08:00** 这个时刻，不是「现在」：
        晚起补发时若以「现在」为基准，当天已到点的事项会被误判成逾期，
        而次日早报更会把当天 08:00 的事项当成未来日期直接漏掉。
        """
        now = now_shanghai()
        moment = self._scheduled_moment(local_date)
        tasks = await list_tasks(self._db, self._weixin.allowed_user_id, "all", reference=moment)
        groups = group_for_digest(tasks, moment)
        greeting, notes, closing = await self._compose_flavor(groups, moment, now)
        text, ordered = self._build_digest_text(
            groups, now, late, commentary=greeting, notes=notes, closing=closing
        )

        existing = await self._digest_row(local_date)
        if existing:
            accepted = await self._db.fetch_one(
                """
                SELECT count(*) AS n FROM bot.deliveries
                WHERE digest_id = %s AND status = 'accepted'
                """,
                (existing["id"],),
            )
            if accepted and accepted["n"]:
                log.info("%s 的早报已有已接受部分，不重复生成", local_date)
                return 0

        async with self._db.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO bot.digests (user_id, local_date, generated_at, refreshed_at)
                VALUES (%s, %s, now(), now())
                ON CONFLICT (user_id, local_date) DO UPDATE
                  SET refreshed_at = now(), generated_at = now()
                RETURNING id
                """,
                (self._weixin.allowed_user_id, local_date),
            )
            digest_id = (await cur.fetchone())["id"]

            # 条目映射：早报里的显示序号 ↔ 稳定事项编号
            await cur.execute("DELETE FROM bot.digest_items WHERE digest_id = %s", (digest_id,))
            merged_ids: list[int] = []
            for index, task in enumerate(ordered, start=1):
                is_merged = self._in_merge_window(task, local_date, now)
                if is_merged:
                    merged_ids.append(task.id)
                await cur.execute(
                    """
                    INSERT INTO bot.digest_items
                        (digest_id, position, task_id, section, merged_from_reminder)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (digest_id, index, task.id, self._section_of(task, moment), is_merged),
                )

            # 与早报重合的到点提醒不再单独发，合并关系已落库
            if merged_ids:
                await cur.execute(
                    """
                    UPDATE bot.deliveries SET status = 'superseded', updated_at = now()
                    WHERE kind = 'reminder' AND task_id = ANY(%s)
                      AND status IN ('pending', 'retry_wait')
                    """,
                    (merged_ids,),
                )

            created = await enqueue_text(
                cur,
                user_id=self._weixin.allowed_user_id,
                kind="digest",
                business_key=digest_business_key(self._weixin.allowed_user_id, local_date),
                text=text,
                scheduled_at=now,
                digest_id=digest_id,
            )

        log.info("已生成 %s 的早报（%d 条事项，%d 段投递%s）",
                 local_date, len(ordered), len(created), "，延迟补发" if late else "")
        return len(created)

    def _in_merge_window(self, task: Task, local_date: date, now: datetime) -> bool:
        """该事项的到点提醒是否应当并入早报而不是单独发。

        窗口从当天 08:00 起：准点生成时是 [08:00, 08:10)。
        延迟补发时窗口一直延到当前时刻——这段时间内已到点的提醒如果仍单独发，
        就会和早报里的同一条内容重复，所以一并并入。
        """
        if task.remind_at is None:
            return False
        moment = self._scheduled_moment(local_date)
        end = max(moment + MERGE_WINDOW, now)
        delta = task.remind_at.astimezone(SHANGHAI) - moment
        return timedelta(0) <= delta < (end - moment)

    def _section_of(self, task: Task, reference: datetime) -> str:
        groups = group_for_digest([task], reference)
        for key in ("important", "today", "overdue", "unscheduled"):
            if groups.get(key):
                return key
        return "today"

    async def run(self) -> None:
        interval = self._cfg.due_scan_interval_secs
        last_digest_check = 0.0
        warned_for: date | None = None
        loop = asyncio.get_running_loop()
        while not self._stopped:
            try:
                await self.ensure_missing_reminders()
                await self.ensure_recurring_reminders()
                await ensure_weekly_review(self._db, self._weixin.allowed_user_id, now_shanghai())

                if loop.time() - last_digest_check > 60:
                    last_digest_check = loop.time()
                    now = now_shanghai()
                    today = now.date()
                    moment = self._scheduled_moment(today)
                    target = self._should_generate(now)

                    if target is not None:
                        if await self._digest_row(target) is None:
                            await self.generate_digest(
                                target, late=now > moment + timedelta(minutes=1)
                            )
                    elif now > moment + timedelta(hours=self._cfg.digest_grace_hours):
                        # 超出补发窗口：当天不再补发，事项仍完整保留、明天照常出现
                        if warned_for != today and await self._digest_row(today) is None:
                            log.warning(
                                "%s 的早报已超出 %s 小时补发窗口，当天不再补发（事项仍保留）",
                                today, self._cfg.digest_grace_hours,
                            )
                            warned_for = today

                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("调度协程本轮异常，%ss 后继续", interval)
                await asyncio.sleep(interval)

    def stop(self) -> None:
        self._stopped = True
