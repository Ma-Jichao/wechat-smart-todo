"""重要工作归档、复盘与可配置周报。发送复用持久化发送队列。"""
from datetime import datetime, time, timedelta

from . import format
from .db import Database
from .outbox import enqueue_text
from .tools import (
    LIVE_DELIVERY_STATUS, SHANGHAI, ToolError, _row_to_task,
)


async def edit_work(db: Database, user_id: str, task_id: int, *,
                    is_important: bool | None = None, note: str | None = None,
                    source_inbox_id: int | None = None):
    if is_important is None and note is None:
        raise ToolError("请说明加入或移出重要工作，或提供复盘备注。")
    if note is not None and (not note.strip() or len(note) > 4000):
        raise ToolError("复盘备注应为 1 至 4000 字。")
    async with db.transaction() as cur:
        await cur.execute("SELECT * FROM bot.tasks WHERE user_id = %s AND id = %s FOR UPDATE",
                          (user_id, task_id))
        old = await cur.fetchone()
        if old is None:
            raise ToolError("没有找到这项工作，请说出事项名称。")
        flag = old["is_important"] if is_important is None else is_important
        if flag and old["repeat_times"]:
            raise ToolError("这是每日重复提醒，请先停掉，再单独创建重要工作。")
        if note is not None and not flag:
            raise ToolError("请先把该事项加入重要工作，再补充复盘备注。")
        # 备注单独审计并累加，重复处理同一入站消息不重复追加。
        if note is not None and source_inbox_id is not None:
            await cur.execute("SELECT 1 FROM bot.task_changes WHERE task_id = %s "
                              "AND field = 'review_note' AND source_inbox_id = %s",
                              (task_id, source_inbox_id))
            if await cur.fetchone():
                note = None
        notes = old["review_note"]
        if note is not None:
            notes = (notes + "\n" + note.strip()).strip()
        await cur.execute("UPDATE bot.tasks SET is_important = %s, review_note = %s, "
                          "updated_at = now() "
                          "WHERE id = %s AND user_id = %s RETURNING *",
                          (flag, notes, task_id, user_id))
        task = _row_to_task(await cur.fetchone())
        if flag:
            await cur.execute("INSERT INTO bot.work_review_settings (user_id) VALUES (%s) "
                              "ON CONFLICT (user_id) DO NOTHING", (user_id,))
        for field, before, after in (("is_important", str(old["is_important"]), str(flag)),
                                      ("review_note", old["review_note"], notes)):
            if before != after:
                await cur.execute("INSERT INTO bot.task_changes "
                                  "(task_id, field, old_value, new_value, source_inbox_id, reason) "
                                  "VALUES (%s, %s, %s, %s, %s, 'important_work')",
                                  (task_id, field, before, after, source_inbox_id))
    return task


async def review_tasks(db: Database, user_id: str, start: datetime | None = None,
                       end: datetime | None = None, *, include_canceled: bool = False, cur=None):
    # 进行中的工作不因创建时间较早而漏掉；完成按 completed_at，取消按 canceled_at。
    sql = (
        "SELECT * FROM bot.tasks WHERE user_id = %s AND is_important "
        "AND (status = 'open' OR "
        "(status = 'completed' AND (%s::timestamptz IS NULL OR completed_at >= %s) "
        "AND (%s::timestamptz IS NULL OR completed_at < %s)) OR "
        "(%s AND status = 'canceled' AND (%s::timestamptz IS NULL OR canceled_at >= %s) "
        "AND (%s::timestamptz IS NULL OR canceled_at < %s))) "
        "ORDER BY created_at, id"
    )
    params = (user_id, start, start, end, end, include_canceled, start, start, end, end)
    if cur is None:
        rows = await db.fetch_all(sql, params)
    else:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [_row_to_task(row) for row in rows]


def review_text(tasks, start: datetime | None, end: datetime | None, *, late=False):
    lines = ["📋 重要工作复盘"]
    if start is not None and end is not None:
        lines.append(f"完成记录范围：{start.astimezone(SHANGHAI):%Y-%m-%d %H:%M} 至 "
                     f"{end.astimezone(SHANGHAI):%Y-%m-%d %H:%M}（不含结束时刻，北京时间）")
    elif start is not None:
        lines.append(f"完成记录：{start.astimezone(SHANGHAI):%Y-%m-%d %H:%M} 起（北京时间）")
    elif end is not None:
        lines.append(f"完成记录：截至 {end.astimezone(SHANGHAI):%Y-%m-%d %H:%M}（不含结束时刻，北京时间）")
    else:
        lines.append("全部历史记录")
    for status, label in (("open", "仍在推进（全部）"), ("completed", "已完成"), ("canceled", "已取消")):
        items = [t for t in tasks if t.status == status]
        lines.extend(["", f"{label} {len(items)} 件"])
        for task in items:
            lines.append(f"· {task.content}")
            lines.append(f"↳ 创建于 {task.created_at.astimezone(SHANGHAI):%Y-%m-%d}")
            if task.completed_at:
                lines.append(f"↳ 完成于 {task.completed_at.astimezone(SHANGHAI):%Y-%m-%d %H:%M}")
            if task.review_note:
                lines.append(f"↳ 复盘备注：{task.review_note}")
    lines.extend(["", "可回复工作名称补充复盘备注；查看更早记录可说「复盘上月的重要工作」。"])
    if late:
        lines.append("（延迟补发）")
    return format.strip_markdown("\n".join(lines))


def latest_review_moment(now: datetime, weekday: int, hour: int, minute: int):
    now = now.astimezone(SHANGHAI)
    day = now.date() - timedelta(days=(now.weekday() - weekday) % 7)
    moment = datetime.combine(day, time(hour, minute), SHANGHAI)
    return moment if moment <= now else moment - timedelta(days=7)


def settings_text(row):
    enabled = "开启" if row["enabled"] else "关闭"
    return (f"重要工作每周汇总：{enabled}\n"
            f"每周{format.WEEKDAY_CN[row['weekday']]} {row['hour']:02d}:{row['minute']:02d}（北京时间）\n"
            "可说「每周汇总改成周日晚上九点」或「关闭重要工作每周汇总」。")


async def review_settings(db: Database, user_id: str, **changes):
    for name, upper in (("weekday", 6), ("hour", 23), ("minute", 59)):
        if name in changes and (type(changes[name]) is not int or not 0 <= changes[name] <= upper):
            raise ToolError("请提供有效的星期和 24 小时制时间。")
    if "enabled" in changes and type(changes["enabled"]) is not bool:
        raise ToolError("开启状态必须是 true 或 false。")
    if set(changes) - {"weekday", "hour", "minute", "enabled"}:
        raise ToolError("不支持的汇总设置。")
    async with db.transaction() as cur:
        await cur.execute("INSERT INTO bot.work_review_settings (user_id) VALUES (%s) "
                          "ON CONFLICT (user_id) DO NOTHING", (user_id,))
        await cur.execute("SELECT * FROM bot.work_review_settings WHERE user_id = %s FOR UPDATE", (user_id,))
        row = await cur.fetchone()
        if changes and any(row[k] != v for k, v in changes.items()):
            values = {k: changes.get(k, row[k]) for k in ("enabled", "weekday", "hour", "minute")}
            await cur.execute("UPDATE bot.work_review_settings SET enabled = %s, weekday = %s, "
                              "hour = %s, minute = %s, updated_at = now() WHERE user_id = %s RETURNING *",
                              (values["enabled"], values["weekday"], values["hour"], values["minute"], user_id))
            row = await cur.fetchone()
            # 关闭或改期时，撤销尚未发送的旧汇总（包含分段、会话阻塞队列）。
            await cur.execute("UPDATE bot.deliveries d SET status = 'superseded', updated_at = now() "
                              "WHERE d.user_id = %s AND d.status = ANY(%s) AND EXISTS "
                              "(SELECT 1 FROM bot.work_review_runs r WHERE r.user_id = %s AND "
                              "(d.business_key = 'work-review:' || r.id::text OR "
                              "d.business_key LIKE 'work-review:' || r.id::text || ':s%%'))",
                              (user_id, list(LIVE_DELIVERY_STATUS), user_id))
    return row


async def ensure_weekly_review(db: Database, user_id: str, now: datetime):
    async with db.transaction() as cur:
        # 与改期共用行锁；不提前生成将来的周报。
        await cur.execute("SELECT * FROM bot.work_review_settings WHERE user_id = %s FOR UPDATE", (user_id,))
        cfg = await cur.fetchone()
        if not cfg or not cfg["enabled"]:
            return 0
        moment = latest_review_moment(now, cfg["weekday"], cfg["hour"], cfg["minute"])
        # 停机只在 24 小时内补发最新一期，避免旧周报积压。
        if moment < cfg["updated_at"] or now - moment > timedelta(hours=24):
            return 0
        await cur.execute("INSERT INTO bot.work_review_runs (user_id, scheduled_at) VALUES (%s, %s) "
                          "ON CONFLICT (user_id, scheduled_at) DO NOTHING RETURNING id", (user_id, moment))
        run = await cur.fetchone()
        if not run:
            return 0
        start = moment - timedelta(days=7)
        tasks = await review_tasks(db, user_id, start, moment, cur=cur)
        ids = await enqueue_text(cur, user_id=user_id, kind="notice",
                                 business_key=f"work-review:{run['id']}", scheduled_at=now,
                                 text=review_text(tasks, start, moment,
                                                  late=now - moment > timedelta(minutes=1)))
        return len(ids)
