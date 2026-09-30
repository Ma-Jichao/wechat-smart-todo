"""投递路径测试：分段、幂等入队、到点兜底、早报生成、租约回收、冷启动阻塞。

打真实 IvorySQL。发送用桩函数替换，不发真实消息。
"""

from __future__ import annotations

import os

import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import load_test_config
from app import outbox, tools
from app.config import WeixinConfig, load
from app.db import Database
from app.outbox import digest_business_key, enqueue_text, segment_text
from app.scheduler import MERGE_WINDOW, Scheduler
from app.sender import DeliverySender



def test_segmentation() -> None:
    assert segment_text("") == []
    assert segment_text("短消息") == ["短消息"]
    assert segment_text("abc", limit=3) == ["abc"]

    long_line = "x" * 250
    parts = segment_text(long_line, limit=100)
    assert [len(p) for p in parts] == [100, 100, 50], [len(p) for p in parts]

    multiline = "\n".join(f"第{i}行的内容" for i in range(30))
    parts = segment_text(multiline, limit=40)
    assert "".join(parts).replace("\n", "") == multiline.replace("\n", ""), "不能丢字符"
    assert all(len(p) <= 40 for p in parts), [len(p) for p in parts]
    assert all(not p.startswith("\n") for p in parts), parts
    print("[通过] 分段：短文本不切、超长单行硬切、多行按行边界切且不丢字符")


async def scenario() -> None:
    base = load_test_config()
    account_id = f"test-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"
    db = Database(base.db, min_size=1, max_size=3)
    await db.open()

    weixin = WeixinConfig(
        base_url="https://example.invalid", account_id=account_id, allowed_user_id=user_id,
        token="test-token", long_poll_timeout_secs=45,
        reconnect_backoff_base_secs=1.0, reconnect_backoff_cap_secs=30.0,
    )

    async def cleanup() -> None:
        await db.execute(
            "DELETE FROM bot.deliveries WHERE user_id = %s OR task_id IN "
            "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user_id, user_id))
        await db.execute(
            "DELETE FROM bot.digest_items WHERE task_id IN "
            "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user_id,))
        await db.execute("DELETE FROM bot.digests WHERE user_id = %s", (user_id,))
        await db.execute(
            "DELETE FROM bot.task_changes WHERE task_id IN "
            "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user_id,))
        await db.execute("DELETE FROM bot.tasks WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.bot_state WHERE account_id = %s", (account_id,))

    try:
        real_now = tools.now_shanghai()
        # 全部锚在「当天 08:00」，测试不依赖运行时刻（否则晚上跑会跨天）
        now = real_now.replace(hour=8, minute=0, second=0, microsecond=0)
        await db.execute(
            "INSERT INTO bot.bot_state (account_id, user_id, cursor, context_token) "
            "VALUES (%s, %s, '', 'test-ctx')", (account_id, user_id))

        # ---- 入队幂等：同一个 business_key 不重复入队 -------------------------
        async with db.transaction() as cur:
            first = await enqueue_text(
                cur, user_id=user_id, kind="reply", business_key="reply:1",
                text="短回复", scheduled_at=now)
        async with db.transaction() as cur:
            second = await enqueue_text(
                cur, user_id=user_id, kind="reply", business_key="reply:1",
                text="短回复", scheduled_at=now)
        assert len(first) == 1 and second == [], (first, second)
        print("[通过] 入队幂等：同一 business_key 第二次不入队")

        # ---- 长文本分段：每段一条投递，键带段号 -------------------------------
        long_text = "\n".join(f"第{i}条待办事项的内容占位" for i in range(200))
        async with db.transaction() as cur:
            ids = await enqueue_text(
                cur, user_id=user_id, kind="digest", business_key="digest:test:2026-09-28",
                text=long_text, scheduled_at=now)
        assert len(ids) > 1, len(ids)
        rows = await db.fetch_all(
            "SELECT business_key, segment_index, segment_total, content_snapshot "
            "FROM bot.deliveries WHERE id = ANY(%s) ORDER BY segment_index", (ids,))
        assert rows[0]["segment_total"] == len(ids), rows[0]
        assert rows[0]["business_key"].endswith(":s1"), rows[0]
        assert all(len(r["content_snapshot"]) <= outbox.MAX_SEGMENT_CHARS for r in rows), "段长超限"
        joined = "".join(r["content_snapshot"] for r in rows)
        assert joined.replace("\n", "") == long_text.replace("\n", ""), "分段后内容丢了"
        print(f"[通过] 长文本入队 {len(ids)} 段，段号/总段数正确，内容无损")

        # ---- 到点兜底：已到期但没有投递记录的事项要补上 ------------------------
        async with db.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO bot.tasks (user_id, content, remind_at)
                VALUES (%s, '历史遗留的提醒', now() - make_interval(mins => 30))
                RETURNING id, version
                """, (user_id,))
            legacy = await cur.fetchone()
        scheduler = Scheduler(db, weixin, base.digest, base.limits)
        created = await scheduler.ensure_missing_reminders()
        assert created == 1, created
        again = await scheduler.ensure_missing_reminders()
        assert again == 0, "第二次不应重复补建"
        legacy_deliveries = await db.fetch_all(
            "SELECT status FROM bot.deliveries WHERE task_id = %s", (legacy["id"],))
        assert len(legacy_deliveries) == 1, legacy_deliveries
        print("[通过] 到点兜底：为历史遗留事项补建 1 条投递，重复执行不叠加")

        # ---- 已发送过的提醒不会被重新入队 --------------------------------------
        await db.execute(
            "UPDATE bot.deliveries SET status = 'accepted', accepted_at = now() "
            "WHERE task_id = %s", (legacy["id"],))
        assert await scheduler.ensure_missing_reminders() == 0, "已发送过的提醒不应重新入队"
        print("[通过] 已发送的提醒不会因逾期被重复催促")

        # ---- 每天重复提醒：工作日生成投递，节假日跳过 --------------------------
        import app.scheduler as scheduler_module

        fixed_now = real_now.replace(year=2026, month=9, day=29, hour=8, minute=0, second=0, microsecond=0)
        orig_now = scheduler_module.now_shanghai
        scheduler_module.now_shanghai = lambda: fixed_now
        try:
            rec = await tools.create_task(
                db, user_id, "打卡", fire_at=None, phrase=None,
                source_inbox_id=None, max_chars=2000,
                repeat_times=["08:15", "18:00"], skip_holidays=True,
            )
            created = await scheduler.ensure_recurring_reminders()
            assert created == 2, created
            assert await scheduler.ensure_recurring_reminders() == 0, "同一天不应重复生成"
            rec_rows = await db.fetch_all(
                "SELECT business_key, kind, content_snapshot FROM bot.deliveries "
                "WHERE task_id = %s AND kind = 'recurring'", (rec.id,))
            assert len(rec_rows) == 2 and all("记得打卡" in r["content_snapshot"] for r in rec_rows), rec_rows

            rec2 = await tools.create_task(
                db, user_id, "节假日打卡", fire_at=None, phrase=None,
                source_inbox_id=None, max_chars=2000,
                repeat_times=["09:00"], skip_holidays=True,
            )
            orig_is_workday = scheduler._is_workday
            scheduler._is_workday = lambda day: False
            assert await scheduler.ensure_recurring_reminders() == 0, "非工作日不应生成"
            scheduler._is_workday = orig_is_workday
        finally:
            scheduler_module.now_shanghai = orig_now
        print("[通过] 每天重复提醒：工作日生成且幂等，非工作日跳过")

        # ---- 早报生成：分组、序号映射、文本内容 -------------------------------
        await tools.create_task(db, user_id, "今天下午的事",
                                fire_at=now + timedelta(hours=2), phrase=None,
                                source_inbox_id=None, max_chars=2000)
        await tools.create_task(db, user_id, "逾期的事",
                                fire_at=now - timedelta(days=1), phrase=None,
                                source_inbox_id=None, max_chars=2000)
        await tools.create_task(db, user_id, "没设时间的事", fire_at=None, phrase=None,
                                source_inbox_id=None, max_chars=2000)
        await tools.create_task(db, user_id, "三天后的事",
                                fire_at=now + timedelta(days=3), phrase=None,
                                source_inbox_id=None, max_chars=2000)

        local_date = now.date()
        sent = await scheduler.generate_digest(local_date, late=False)
        assert sent >= 1, sent
        items = await db.fetch_all(
            """
            SELECT di.position, di.section, di.merged_from_reminder, di.task_id, t.content
            FROM bot.digest_items di JOIN bot.tasks t ON t.id = di.task_id
            JOIN bot.digests d ON d.id = di.digest_id
            WHERE d.user_id = %s AND d.local_date = %s ORDER BY di.position
            """, (user_id, local_date))
        assert [i["position"] for i in items] == list(range(1, len(items) + 1)), items
        contents = [i["content"] for i in items]
        assert "今天下午的事" in contents and "逾期的事" in contents \
            and "没设时间的事" in contents, contents
        assert "三天后的事" not in contents, "未来日期不应进早报"
        sections = [i["section"] for i in items]
        assert "overdue" in sections and "today" in sections and "unscheduled" in sections, sections
        body = (await db.fetch_all(
            "SELECT content_snapshot FROM bot.deliveries WHERE digest_id = "
            "(SELECT id FROM bot.digests WHERE user_id = %s AND local_date = %s) "
            "ORDER BY segment_index", (user_id, local_date)))[0]["content_snapshot"]
        assert "今日待办" in body, body
        assert "⚠️ 逾期" in body, f"逾期项应有行内标记：{body}"
        assert "未定时间" in body, body
        assert "生成" in body, "早报要标注生成时间，避免被当成实时状态"
        assert "━" not in body, "不应出现横线"
        print(f"[通过] 早报生成：{len(items)} 条、序号连续、三分组齐全、未来日期不入早报")
        print("       正文预览：\n" + "\n".join("         " + l for l in body.splitlines()[:8]))

        # ---- 早报序号引用：显示序号 → 稳定编号（走 digest_items，不调 LLM）---
        from app.llm import LlmClient

        llm = LlmClient(base.deepseek, base.limits.max_text_chars, base.limits.clarify_ttl_minutes)
        assert len(items) >= 2, items
        ref = await llm._resolve_digest_reference(db, user_id, "完成 2")
        assert ref is not None and ref[1] == items[1]["task_id"], (ref, items)
        ref2 = await llm._resolve_digest_reference(db, user_id, "第 1 项完成了")
        assert ref2 is not None and ref2[1] == items[0]["task_id"], (ref2, items)
        assert await llm._resolve_digest_reference(db, user_id, "完成 99") is None, "超范围序号不应解析"
        assert await llm._resolve_digest_reference(db, user_id, "完成了 2 件事") is None, \
            "「完成 N 件」陈述不应被当序号引用"
        await llm.close()
        print("[通过] 早报序号引用：完成 2 / 第 1 项 → 对应编号，超范围与「N 件」不解析")

        # ---- 早报已存在且已接受时不重复生成 -----------------------------------
        await db.execute(
            "UPDATE bot.deliveries SET status = 'accepted', accepted_at = now() "
            "WHERE digest_id = (SELECT id FROM bot.digests WHERE user_id = %s AND local_date = %s)",
            (user_id, local_date))
        assert await scheduler.generate_digest(local_date, late=False) == 0
        print("[通过] 早报已有已接受部分，不重复生成")

        # ---- 08:00 重合的到点提醒被合并进早报，不再单独发 ----------------------
        tomorrow = local_date + timedelta(days=1)
        moment = scheduler._scheduled_moment(tomorrow)
        async with db.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO bot.tasks (user_id, content, remind_at)
                VALUES (%s, '正好八点的事', %s) RETURNING id, version
                """, (user_id, moment),)
            eight = await cur.fetchone()
        async with db.transaction() as cur:
            await cur.execute(
                """
                INSERT INTO bot.deliveries
                    (user_id, kind, task_id, task_version, business_key,
                     scheduled_at, next_attempt_at, content_snapshot)
                VALUES (%s, 'reminder', %s, %s, %s, %s, %s, '正好八点的事')
                """,
                (user_id, eight["id"], eight["version"],
                 outbox.reminder_business_key(eight["id"], eight["version"]), moment, moment))
        await scheduler.generate_digest(tomorrow, late=False)
        merged = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.digest_items WHERE task_id = %s "
            "AND merged_from_reminder", (eight["id"],))
        assert merged["n"] == 1, merged
        reminder_row = await db.fetch_one(
            "SELECT status FROM bot.deliveries WHERE task_id = %s AND kind = 'reminder'",
            (eight["id"],))
        assert reminder_row["status"] == "superseded", reminder_row
        assert MERGE_WINDOW > timedelta(0)
        print("[通过] 08:00 重合：合并关系落库，原到点提醒被作废，不重复发送")

        # ---- 发送：冷启动阻塞不消耗重试次数 -----------------------------------
        class StubSender:
            def __init__(self, behaviour):
                self.behaviour = behaviour
                self.calls = 0

            async def send_text(self, user_id, token, text):
                self.calls += 1
                return await self.behaviour()

        from wechatbot.errors import ApiError

        async def raise_context():
            raise ApiError("prepare failed", http_status=200, errcode=-2, payload={})

        sender = DeliverySender(db, weixin, base.delivery, base.limits)
        stub = StubSender(raise_context)
        sender._sender = stub
        delivery = await sender.claim_one()
        assert delivery is not None, "应能领取到一条到期投递"
        await sender.deliver_one(delivery)
        row = await db.fetch_one("SELECT * FROM bot.deliveries WHERE id = %s", (delivery["id"],))
        assert row["status"] == "blocked_context", row["status"]
        assert row["attempts"] == 0, f"通道未就绪不应消耗重试次数，实际 {row['attempts']}"
        assert row["error_class"] == "context", row["error_class"]
        print("[通过] 冷启动阻塞 → blocked_context，attempts 保持 0，不被判为失败")

        # ---- 收到入站消息后解封 ----------------------------------------------
        unblocked = await sender.requeue_blocked_context()
        assert unblocked >= 1, unblocked
        row = await db.fetch_one("SELECT status, last_error FROM bot.deliveries WHERE id = %s",
                                 (delivery["id"],))
        assert row["status"] == "pending" and row["last_error"] is None, row
        print(f"[通过] 入站消息到达后解封 {unblocked} 条投递回到 pending")

        # ---- 发送成功：accepted，attempts+1 ------------------------------------
        async def succeed():
            return {"message_id": 12345}

        sender._sender = StubSender(succeed)
        delivery = await sender.claim_one()
        assert delivery is not None
        await sender.deliver_one(delivery)
        row = await db.fetch_one("SELECT * FROM bot.deliveries WHERE id = %s", (delivery["id"],))
        assert row["status"] == "accepted" and row["attempts"] == 1, row
        assert row["accepted_at"] is not None, row
        print("[通过] 发送成功 → accepted，attempts=1，accepted_at 已记")

        # ---- 租约回收：卡在 sending 的过期租约能恢复 ---------------------------
        await db.execute(
            "UPDATE bot.deliveries SET status = 'sending', "
            "lease_until = now() - make_interval(secs => 10) WHERE id = %s", (delivery["id"],))
        reclaimed = await sender.reclaim_expired()
        assert reclaimed >= 1, reclaimed
        row = await db.fetch_one("SELECT status FROM bot.deliveries WHERE id = %s", (delivery["id"],))
        assert row["status"] == "retry_wait", row
        print("[通过] 租约过期可回收，不留永久卡在 sending 的记录")

        # ---- 限流：账号维度共享冷却 -------------------------------------------
        async def rate_limited():
            raise ApiError("iLink sendmessage rate limited", http_status=200, errcode=-1, payload={})

        sender._sender = StubSender(rate_limited)
        delivery = await sender.claim_one()
        assert delivery is not None
        await sender.deliver_one(delivery)
        row = await db.fetch_one("SELECT * FROM bot.deliveries WHERE id = %s", (delivery["id"],))
        assert row["status"] == "retry_wait", row
        assert row["error_class"] == "rate_limit", row["error_class"]
        assert sender._cooldown_until > 0, "限流应触发账号维度冷却"
        print("[通过] 限流 → retry_wait + error_class=rate_limit，并触发共享冷却")

        # ---- 凭据失效：暂停队列而不是无意义重试 --------------------------------
        async def auth_failed():
            raise ApiError("session expired", http_status=200, errcode=-14, payload={})

        sender.paused_for_auth = False
        sender._sender = StubSender(auth_failed)
        delivery = await sender.claim_one()
        assert delivery is not None
        await sender.deliver_one(delivery)
        row = await db.fetch_one("SELECT * FROM bot.deliveries WHERE id = %s", (delivery["id"],))
        assert row["status"] == "blocked_auth", row
        assert sender.paused_for_auth, "凭据失效应暂停后续投递"
        print("[通过] 凭据失效 → blocked_auth 且暂停队列，不盲目重试")
    finally:
        await cleanup()
        await db.close()


def main() -> int:
    test_segmentation()
    asyncio.run(scenario())
    print("\n投递路径全部用例通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
