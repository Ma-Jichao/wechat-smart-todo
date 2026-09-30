"""出站消息入库。

所有要发出去的东西（确认回复、到点提醒、早报、错误通知）都走这里进 deliveries 表，
由单一的发送协程统一限速发出去，任何一路都不能绕开节流。

超长内容在这里就切成多段，一段一条投递记录，各自有稳定的 business_key，
这样中途失败只需恢复必要片段（对应第 4.3 节「只恢复必要片段」）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from psycopg import AsyncCursor

log = logging.getLogger(__name__)

# 单段最大字符数。这是保守取值，**平台真实上限尚未实测**，
# 属于「不能伪造官方阈值」的那类参数，等实测后再调。
MAX_SEGMENT_CHARS = 1500


def segment_text(text: str, limit: int = MAX_SEGMENT_CHARS) -> list[str]:
    """按段落边界切分，避免在句子中间截断。切不出更小的段落时才硬切。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    segments: list[str] = []
    current = ""
    for block in text.split("\n"):
        if len(block) > limit:
            # 单行就超长，先冲掉现有缓冲，再硬切这一行
            if current:
                segments.append(current)
                current = ""
            for start in range(0, len(block), limit):
                segments.append(block[start:start + limit])
            continue
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > limit:
            segments.append(current)
            current = block
        else:
            current = candidate
    if current:
        segments.append(current)
    return segments


async def enqueue_text(
    cur: AsyncCursor,
    *,
    user_id: str,
    kind: str,
    business_key: str,
    text: str,
    scheduled_at: datetime,
    task_id: int | None = None,
    task_version: int | None = None,
    digest_id: int | None = None,
    limit: int = MAX_SEGMENT_CHARS,
) -> list[int]:
    """把一段文本切成若干段并写入 deliveries，返回新建的投递 id。

    business_key 相同则忽略（幂等）。分段时键为 `{business_key}:s{i}`。
    """
    segments = segment_text(text, limit)
    if not segments:
        return []

    total = len(segments)
    created: list[int] = []
    for index, segment in enumerate(segments, start=1):
        key = business_key if total == 1 else f"{business_key}:s{index}"
        await cur.execute(
            """
            INSERT INTO bot.deliveries
                (user_id, kind, task_id, task_version, digest_id, segment_index, segment_total,
                 business_key, scheduled_at, next_attempt_at, content_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (business_key) DO NOTHING
            RETURNING id
            """,
            (
                user_id, kind, task_id, task_version, digest_id, index, total,
                key, scheduled_at, scheduled_at, segment,
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            created.append(row["id"])
    if created:
        log.info("入队 %d 段（%s，共 %d 段）", len(created), kind, total)
    return created


def digest_business_key(user_id: str, local_date: Any, part: str = "main") -> str:
    return f"digest:{user_id}:{local_date}:{part}"


def reply_business_key(inbox_id: int) -> str:
    """一条入站消息的确认回复。同一消息重试时键稳定，不会重复回。"""
    return f"reply:{inbox_id}"


def reminder_business_key(task_id: int, version: int) -> str:
    return f"reminder:{task_id}:v{version}"


def recurring_business_key(task_id: int, local_date: Any, hhmm: str) -> str:
    """重复提醒每天每个时间点一条。键含日期与时刻，保证当天幂等。"""
    return f"recurring:{task_id}:{local_date}:{hhmm}"


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]
