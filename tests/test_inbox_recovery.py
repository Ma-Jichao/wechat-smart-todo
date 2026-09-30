"""处理协程的崩溃恢复测试。

关键点：待处理消息的唯一来源必须是数据库，不能只靠内存队列。
否则「消息已落库但还没处理」的记录在崩溃后再也没人管，接收恢复约束。

模型用桩替换，不调用 DeepSeek；发送协程用桩替换，不发真实消息。
"""

from __future__ import annotations

import os

import asyncio
import dataclasses
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.types.json import Jsonb

from support import load_test_config
from app.config import load
from app.db import Database
from app.llm import HandlerResult
from app.main import CLAIM_NEXT_INBOX_SQL, RECOVER_INBOX_SQL, Processor


class StubLlm:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, db, user_id, text, *, inbox_id=None, source="text"):
        self.calls.append(text)
        return HandlerResult(reply=f"桩回复：已处理「{text}」", task_ids=[])


class StubSender:
    paused_for_auth = False


def make_raw(message_id: str, text: str, user_id: str) -> dict:
    return {
        "message_id": message_id,
        "seq": 1,
        "message_type": 1,
        "from_user_id": user_id,
        "to_user_id": "bot",
        "create_time_ms": 1790000000000,
        "context_token": "ctx-test",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


async def scenario() -> None:
    base = load_test_config()
    account_id = f"test-{uuid.uuid4()}"
    # 用独立的测试用户，绝不动生产 user_id 的数据
    user_id = f"test-user-{uuid.uuid4()}"
    cfg = dataclasses.replace(
        base, weixin=dataclasses.replace(base.weixin, allowed_user_id=user_id)
    )
    db = Database(base.db, min_size=1, max_size=3)
    await db.open()
    llm = StubLlm()
    processor = Processor(cfg, db, llm, StubSender())

    async def cleanup() -> None:
        await db.execute("DELETE FROM bot.deliveries WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.messages_seen WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.inbox WHERE account_id = %s", (account_id,))

    async def add_inbox(message_id: str, text: str, status: str = "pending",
                        lease_seconds: int | None = None) -> int:
        row = await db.fetch_one(
            """
            INSERT INTO bot.inbox (account_id, message_id, seq, from_user_id, raw, status,
                                   lease_until)
            VALUES (%s, %s, 1, %s, %s, %s,
                    CASE WHEN %s::int IS NULL THEN NULL
                         ELSE now() + make_interval(secs => %s::int) END)
            RETURNING id
            """,
            (account_id, message_id, user_id, Jsonb(make_raw(message_id, text, user_id)),
             status, lease_seconds, lease_seconds),
        )
        assert row is not None
        return row["id"]

    try:
        # 清理上轮可能的残留，避免干扰计数
        await cleanup()

        # ---- 用例 1：只靠数据库就能领取到待处理消息（不经过内存队列）-----------
        row = await db.fetch_one(CLAIM_NEXT_INBOX_SQL)
        assert row is None, "此时不该有可领取的消息"
        first = await add_inbox("crash-1", "第一条")
        second = await add_inbox("crash-2", "第二条")

        claimed = await db.fetch_one(CLAIM_NEXT_INBOX_SQL)
        assert claimed is not None and claimed["id"] == first, "应按 id 顺序领取（单用户保序）"
        claimed2 = await db.fetch_one(CLAIM_NEXT_INBOX_SQL)
        assert claimed2 is not None and claimed2["id"] == second, claimed2
        assert await db.fetch_one(CLAIM_NEXT_INBOX_SQL) is None, "都领取过了就不该再领到"
        print("[通过] 待处理消息来自数据库，按 id 顺序领取，保序不重领")

        # ---- 用例 2：租约未过期时不会被重复领取 -------------------------------
        await db.execute(
            "UPDATE bot.inbox SET status = 'processing', "
            "lease_until = now() + make_interval(secs => 120) WHERE id = %s", (first,))
        assert await db.fetch_one(CLAIM_NEXT_INBOX_SQL) is None, "租约未过期不应被抢走"
        print("[通过] processing 且租约未过期时不会被另一个消费者抢走")

        # ---- 用例 3：崩溃恢复——租约过期的 processing 回到可处理状态 -------------
        await db.execute(
            "UPDATE bot.inbox SET status = 'processing', "
            "lease_until = now() - make_interval(secs => 5) WHERE id = %s", (first,))
        recovered = await db.execute(RECOVER_INBOX_SQL)
        assert recovered >= 1, recovered
        row = await db.fetch_one("SELECT status FROM bot.inbox WHERE id = %s", (first,))
        assert row["status"] == "retry", row
        print("[通过] 崩溃恢复：租约过期的 processing 记录回到 retry，可被重新处理")

        # ---- 用例 4：处理协程真的把消息处理完并幂等地回一条 ---------------------
        await cleanup()
        llm.calls.clear()
        inbox_id = await add_inbox("crash-3", "提醒我明天下午三点开会")

        task = asyncio.create_task(processor.run())
        for _ in range(60):
            await asyncio.sleep(0.2)
            row = await db.fetch_one("SELECT status FROM bot.inbox WHERE id = %s", (inbox_id,))
            if row and row["status"] == "done":
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        row = await db.fetch_one("SELECT status, processed_at FROM bot.inbox WHERE id = %s",
                                 (inbox_id,))
        assert row["status"] == "done" and row["processed_at"] is not None, row
        assert llm.calls == ["提醒我明天下午三点开会"], llm.calls

        replies = await db.fetch_all(
            "SELECT business_key, status, content_snapshot FROM bot.deliveries "
            "WHERE business_key = %s", (f"reply:{inbox_id}",))
        assert len(replies) == 1, replies
        assert "桩回复" in replies[0]["content_snapshot"], replies
        print(f"[通过] 处理协程处理完成：inbox → done，回一条 {replies[0]['business_key']}")

        # 重复处理同一批不应产生第二条回复（业务键幂等）
        await db.execute("UPDATE bot.inbox SET status = 'retry', lease_until = NULL "
                         "WHERE id = %s", (inbox_id,))
        task = asyncio.create_task(processor.run())
        for _ in range(40):
            await asyncio.sleep(0.2)
            row = await db.fetch_one("SELECT status FROM bot.inbox WHERE id = %s", (inbox_id,))
            if row and row["status"] == "done":
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        replies = await db.fetch_all(
            "SELECT id FROM bot.deliveries WHERE business_key = %s", (f"reply:{inbox_id}",))
        assert len(replies) == 1, "重复处理不应产生第二条回复"
        print("[通过] 同一条消息被重新处理也不会重复回复（回复业务键幂等）")

        # ---- 用例 5：非白名单用户的消息不触发业务，但要标记完成 -----------------
        await cleanup()
        llm.calls.clear()
        intruder_raw = make_raw("crash-4", "我是别人", "someone-else@im.wechat")
        row = await db.fetch_one(
            """
            INSERT INTO bot.inbox (account_id, message_id, seq, from_user_id, raw)
            VALUES (%s, 'crash-4', 1, %s, %s) RETURNING id
            """,
            (account_id, "someone-else@im.wechat", Jsonb(intruder_raw)),
        )
        intruder_id = row["id"]
        task = asyncio.create_task(processor.run())
        for _ in range(40):
            await asyncio.sleep(0.2)
            row = await db.fetch_one("SELECT status FROM bot.inbox WHERE id = %s", (intruder_id,))
            if row and row["status"] == "done":
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert llm.calls == [], "非授权消息不应进入模型"
        row = await db.fetch_one("SELECT status FROM bot.inbox WHERE id = %s", (intruder_id,))
        assert row["status"] == "done", row
        print("[通过] 非白名单用户：未进入模型，记录标记完成，不会反复重试")
    finally:
        await cleanup()
        await db.close()


def main() -> int:
    asyncio.run(scenario())
    print("\n处理协程崩溃恢复用例全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
