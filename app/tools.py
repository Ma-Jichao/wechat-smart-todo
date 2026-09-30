"""业务操作层。

所有函数都限定在单个 user_id 内，参数里的 user_id 由程序绑定，不来自模型。
所有变更都在一个事务里完成，并把「取消旧投递 + 安排新投递」一起做掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from psycopg import AsyncCursor

from .db import Database

log = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

STATUS_OPEN = "open"
STATUS_COMPLETED = "completed"
STATUS_CANCELED = "canceled"

# 投递状态里「还可能发出去」的集合。注意：绝不用 status <> 'sent' 这种写法，
# 那会把 canceled / superseded / permanent_failed 也扫进来。
LIVE_DELIVERY_STATUS = ("pending", "retry_wait", "blocked_context", "blocked_auth")

# 只允许在这两个时点之间提醒，超出范围拒绝并回问用户
MAX_HORIZON_DAYS = 365 * 3
PAST_TOLERANCE = timedelta(minutes=5)

SCOPE_TODAY = "today"
SCOPE_OVERDUE = "overdue"
SCOPE_UNSCHEDULED = "unscheduled"
SCOPE_ALL = "all"
SCOPE_RECURRING = "recurring"


class ToolError(Exception):
    """业务校验失败。message 会原样回给模型，让它据此向用户澄清。"""


@dataclass
class Task:
    id: int
    user_id: str
    content: str
    status: str
    remind_at: datetime | None
    remind_at_phrase: str | None
    version: int
    created_at: datetime
    repeat_times: list[str] = field(default_factory=list)
    skip_holidays: bool = False


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def validate_fire_at(value: datetime, *, reference: datetime | None = None) -> datetime:
    """校验并规范化提醒时间。过去时间与超范围都拒绝，交给模型回问用户。"""
    if value.tzinfo is None:
        raise ToolError("时间必须带时区，请重新说明提醒时间。")
    moment = value.astimezone(SHANGHAI)
    anchor = (reference or now_shanghai()).astimezone(SHANGHAI)
    if moment < anchor - PAST_TOLERANCE:
        raise ToolError(
            f"你给的时间是 {moment:%Y年%m月%d日 %H:%M}，已经过去了。"
            "请向用户说明并询问是不是要改成别的日期时间，不要自己改成第二天。"
        )
    if moment > anchor + timedelta(days=MAX_HORIZON_DAYS):
        raise ToolError(f"提醒时间 {moment:%Y年%m月%d日} 超过了可调度范围，请与用户确认。")
    return moment


def parse_fire_at(raw: str | None) -> datetime | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(
            f"时间格式无法识别：{raw!r}。请用带时区的 ISO8601，例如 2026-09-29T15:00:00+08:00。"
        ) from exc
    return validate_fire_at(value)


def local_date_of(moment: datetime) -> date:
    return moment.astimezone(SHANGHAI).date()


async def _supersede_deliveries(cur: AsyncCursor, task_id: int, keep_version: int) -> None:
    """事项版本变更后，作废旧版本尚未发出的投递。

    只作废尚未发送的投递（包括等待会话或凭据恢复的队列）。已经在网络中的 sending 无法撤回，
    这里不碰它，只记一条竞态日志（对应第 2.2 节「不承诺绝对撤回」）。
    """
    await cur.execute(
        """
        UPDATE bot.deliveries
        SET status = 'superseded', updated_at = now()
        WHERE task_id = %s
          AND status = ANY(%s)
          AND task_version IS DISTINCT FROM %s
        """,
        (task_id, list(LIVE_DELIVERY_STATUS), keep_version),
    )
    await cur.execute(
        """
        SELECT count(*) AS n FROM bot.deliveries
        WHERE task_id = %s AND status = 'sending' AND task_version IS DISTINCT FROM %s
        """,
        (task_id, keep_version),
    )
    row = await cur.fetchone()
    if row and row["n"]:
        log.warning(
            "事项 #%s 版本变更为 %s 时仍有 %s 条投递在发送中，无法撤回，将如实告知用户",
            task_id,
            keep_version,
            row["n"],
        )


async def _schedule_reminder(cur: AsyncCursor, task: Task) -> None:
    """为一个有提醒时间的事项安排到点投递。business_key 含版本号，保证幂等。"""
    if task.remind_at is None:
        return
    await cur.execute(
        """
        INSERT INTO bot.deliveries
            (user_id, kind, task_id, task_version, business_key,
             scheduled_at, next_attempt_at, content_snapshot)
        VALUES (%s, 'reminder', %s, %s, %s, %s, %s, %s)
        ON CONFLICT (business_key) DO NOTHING
        """,
        (
            task.user_id,
            task.id,
            task.version,
            f"reminder:{task.id}:v{task.version}",
            task.remind_at,
            task.remind_at,
            task.content,
        ),
    )


def _row_to_task(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        user_id=row["user_id"],
        content=row["content"],
        status=row["status"],
        remind_at=row["remind_at"],
        remind_at_phrase=row["remind_at_phrase"],
        version=row["version"],
        created_at=row["created_at"],
        repeat_times=list(row.get("repeat_times") or []),
        skip_holidays=bool(row.get("skip_holidays")),
    )


async def get_task(db: Database, user_id: str, task_id: int) -> Task | None:
    row = await db.fetch_one(
        "SELECT * FROM bot.tasks WHERE id = %s AND user_id = %s", (task_id, user_id)
    )
    return _row_to_task(row) if row else None


async def create_task(
    db: Database,
    user_id: str,
    content: str,
    *,
    fire_at: datetime | None,
    phrase: str | None,
    source_inbox_id: int | None,
    max_chars: int,
    repeat_times: Sequence[str] = (),
    skip_holidays: bool = False,
) -> Task:
    content = content.strip()
    if not content:
        raise ToolError("事项内容为空，请用户重新说明要记什么。")
    if len(content) > max_chars:
        raise ToolError(f"事项内容超过 {max_chars} 字上限，请用户精简后重发。")
    times = [t.strip() for t in (repeat_times or ()) if t and t.strip()]
    if times:
        fire_at = None  # 重复提醒不带一次性时间

    async with db.transaction() as cur:
        await cur.execute(
            """
            INSERT INTO bot.tasks (user_id, content, remind_at, remind_at_phrase,
                                   source_inbox_id, repeat_times, skip_holidays)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, content, fire_at, phrase, source_inbox_id, times, skip_holidays),
        )
        task = _row_to_task(await cur.fetchone())
        await _schedule_reminder(cur, task)
        await cur.execute(
            "INSERT INTO bot.task_changes (task_id, field, new_value, source_inbox_id, reason) "
            "VALUES (%s, 'created', %s, %s, 'create_reminder')",
            (task.id, task.content, source_inbox_id),
        )
    if times:
        log.info("创建重复提醒 #%s（每天 %s%s）", task.id, "、".join(times),
                 "，节假日跳过" if skip_holidays else "")
    else:
        log.info("创建事项 #%s（%s）", task.id, task.remind_at)
    return task


async def list_tasks(
    db: Database,
    user_id: str,
    scope: str = SCOPE_ALL,
    *,
    reference: datetime | None = None,
) -> list[Task]:
    anchor = (reference or now_shanghai()).astimezone(SHANGHAI)
    today = anchor.date()

    if scope == SCOPE_RECURRING:
        rows = await db.fetch_all(
            "SELECT * FROM bot.tasks WHERE user_id = %s AND status = 'open' "
            "AND repeat_times <> '{}' ORDER BY id",
            (user_id,),
        )
        return [_row_to_task(r) for r in rows]

    # 一次性事项才进下面的范围；重复提醒单独用 recurring 查
    base = ("SELECT * FROM bot.tasks WHERE user_id = %s AND status = 'open' "
            "AND repeat_times = '{}'")

    if scope == SCOPE_TODAY:
        # 业务日界按 Asia/Shanghai 在 SQL 里显式换算，不依赖连接或进程默认时区
        rows = await db.fetch_all(
            base
            + " AND remind_at IS NOT NULL"
            " AND (remind_at AT TIME ZONE 'Asia/Shanghai')::date = %s"
            " ORDER BY remind_at",
            (user_id, today),
        )
    elif scope == SCOPE_OVERDUE:
        rows = await db.fetch_all(
            base
            + " AND remind_at IS NOT NULL"
            " AND (remind_at AT TIME ZONE 'Asia/Shanghai')::date < %s"
            " ORDER BY remind_at",
            (user_id, today),
        )
    elif scope == SCOPE_UNSCHEDULED:
        rows = await db.fetch_all(
            base + " AND remind_at IS NULL ORDER BY id", (user_id,)
        )
    else:
        rows = await db.fetch_all(base + " ORDER BY remind_at NULLS LAST, id", (user_id,))
    return [_row_to_task(r) for r in rows]


async def find_tasks_by_content(db: Database, user_id: str, keyword: str) -> list[Task]:
    rows = await db.fetch_all(
        """
        SELECT * FROM bot.tasks
        WHERE user_id = %s AND status = 'open' AND content ILIKE %s
        ORDER BY created_at DESC
        """,
        (user_id, f"%{keyword}%"),
    )
    return [_row_to_task(r) for r in rows]


async def update_task(
    db: Database,
    user_id: str,
    task_id: int,
    *,
    content: str | None = None,
    fire_at: datetime | None = None,
    clear_time: bool = False,
    phrase: str | None = None,
    source_inbox_id: int | None = None,
) -> Task:
    current = await get_task(db, user_id, task_id)
    if current is None:
        raise ToolError("没找到你要改的那件事，能再说清楚一点吗（比如说出事项内容）？")
    if current.status != STATUS_OPEN:
        raise ToolError(f"这件事已经是「{current.status}」状态，不能再修改。")
    if content is None and fire_at is None and not clear_time:
        raise ToolError("没有给出要修改的内容或时间。")

    new_content = current.content if content is None else content.strip()
    if not new_content:
        raise ToolError("事项内容不能改成空。")
    if clear_time:
        new_remind = None
    elif fire_at is not None:
        new_remind = fire_at
    else:
        new_remind = current.remind_at

    async with db.transaction() as cur:
        await cur.execute(
            """
            UPDATE bot.tasks
            SET content = %s, remind_at = %s,
                remind_at_phrase = COALESCE(%s, remind_at_phrase),
                version = version + 1, updated_at = now()
            WHERE id = %s AND user_id = %s
            RETURNING *
            """,
            (new_content, new_remind, phrase, task_id, user_id),
        )
        task = _row_to_task(await cur.fetchone())
        # 先作废旧投递，再为同一事务内的新版本安排投递
        await _supersede_deliveries(cur, task.id, task.version)
        await _schedule_reminder(cur, task)
        if content is not None and new_content != current.content:
            await cur.execute(
                "INSERT INTO bot.task_changes (task_id, field, old_value, new_value, "
                "source_inbox_id, reason) VALUES (%s, 'content', %s, %s, %s, 'update_reminder')",
                (task.id, current.content, new_content, source_inbox_id),
            )
        if new_remind != current.remind_at:
            await cur.execute(
                "INSERT INTO bot.task_changes (task_id, field, old_value, new_value, "
                "source_inbox_id, reason) VALUES (%s, 'remind_at', %s, %s, %s, 'update_reminder')",
                (task.id, str(current.remind_at), str(new_remind), source_inbox_id),
            )
    log.info("修改事项 #%s 到 v%s", task.id, task.version)
    return task


# 事项状态 -> 对应的关闭时间列。列名只从这里取，杜绝把状态值拼进 SQL 标识符。
_CLOSE_AT_COLUMN = {STATUS_COMPLETED: "completed_at", STATUS_CANCELED: "canceled_at"}


async def _close_task(db: Database, user_id: str, task_id: int, status: str) -> Task:
    column = _CLOSE_AT_COLUMN.get(status)
    if column is None:
        raise ToolError(f"不支持的事项状态：{status}")
    async with db.transaction() as cur:
        await cur.execute(
            f"""
            UPDATE bot.tasks
            SET status = %s,
                {column} = now(),
                version = version + 1,
                updated_at = now()
            WHERE id = %s AND user_id = %s AND status = 'open'
            RETURNING *
            """,
            (status, task_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            await cur.execute(
                "SELECT * FROM bot.tasks WHERE id = %s AND user_id = %s", (task_id, user_id)
            )
            existing = await cur.fetchone()
            if existing is None:
                raise ToolError("没找到你要操作的那件事，能再说清楚一点吗？")
            raise ToolError(f"这件事已经是「{existing['status']}」状态。")
        task = _row_to_task(row)
        # 完成后立刻停止该事项后续到点提醒；保留审计记录，不删除
        await cur.execute(
            """
            UPDATE bot.deliveries SET status = 'canceled', updated_at = now()
            WHERE task_id = %s AND kind IN ('reminder', 'recurring') AND status = ANY(%s)
            """,
            (task.id, list(LIVE_DELIVERY_STATUS)),
        )
        await cur.execute(
            "INSERT INTO bot.task_changes (task_id, field, new_value, reason) "
            "VALUES (%s, 'status', %s, %s)",
            (task.id, status, f"{status}_task"),
        )
    log.info("事项 #%s -> %s", task.id, status)
    return task


async def complete_task(db: Database, user_id: str, task_id: int) -> Task:
    return await _close_task(db, user_id, task_id, STATUS_COMPLETED)


async def cancel_task(db: Database, user_id: str, task_id: int) -> Task:
    return await _close_task(db, user_id, task_id, STATUS_CANCELED)


async def record_seen_message(
    db: Database,
    user_id: str,
    direction: str,
    body: str,
    task_ids: Sequence[int] = (),
) -> int:
    row = await db.fetch_one(
        """
        INSERT INTO bot.messages_seen (user_id, direction, body, task_ids)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (user_id, direction, body, list(task_ids)),
    )
    assert row is not None
    return row["id"]


async def recent_seen_messages(db: Database, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
    return list(
        reversed(
            await db.fetch_all(
                """
                SELECT direction, body, task_ids, created_at FROM bot.messages_seen
                WHERE user_id = %s ORDER BY created_at DESC LIMIT %s
                """,
                (user_id, limit),
            )
        )
    )


def group_for_digest(tasks: Iterable[Task], reference: datetime) -> dict[str, list[Task]]:
    """按早报要求分组：今天待办、逾期未完成、未设提醒时间。

    逾期与今天不重复：已经过了具体时间仍未完成的，放进逾期分组。
    """
    anchor = reference.astimezone(SHANGHAI)
    today = anchor.date()
    groups: dict[str, list[Task]] = {SCOPE_TODAY: [], SCOPE_OVERDUE: [], SCOPE_UNSCHEDULED: []}
    for task in tasks:
        if task.status != STATUS_OPEN:
            continue
        if getattr(task, "repeat_times", None):
            continue  # 重复提醒不进早报，到点单独发
        if task.remind_at is None:
            groups[SCOPE_UNSCHEDULED].append(task)
            continue
        moment = task.remind_at.astimezone(SHANGHAI)
        if moment.date() < today:
            groups[SCOPE_OVERDUE].append(task)
        elif moment.date() == today:
            if moment < anchor:
                groups[SCOPE_OVERDUE].append(task)
            else:
                groups[SCOPE_TODAY].append(task)
        # 未来日期默认不占用早报
    for key in groups:
        groups[key].sort(key=lambda t: (t.remind_at or datetime.max.replace(tzinfo=timezone.utc)))
    return groups
