"""离线业务测试：不读取配置、不调用模型、不发送微信。"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import flavor, format, important, tools
from app.llm import LlmClient
from app.scheduler import Scheduler

NOW = datetime(2026, 10, 23, 20, tzinfo=tools.SHANGHAI)


def task(task_id=1, **changes):
    values = dict(id=task_id, user_id="test-user", content=f"工作{task_id}", status="open",
                  remind_at=None, remind_at_phrase=None, version=1, created_at=NOW-timedelta(days=30),
                  is_important=True)
    values.update(changes)
    return tools.Task(**values)


def row(**changes):
    values = vars(task()).copy()
    values.update(changes)
    return values


def fake_db(responses=()):
    cur = SimpleNamespace(execute=AsyncMock(), fetchone=AsyncMock(side_effect=list(responses)),
                          fetchall=AsyncMock(return_value=[]))

    @asynccontextmanager
    async def transaction():
        yield cur

    return SimpleNamespace(transaction=transaction, fetch_all=AsyncMock(return_value=[])), cur


def settings(**changes):
    values = dict(user_id="test-user", enabled=True, weekday=4, hour=20, minute=0,
                  updated_at=NOW-timedelta(days=20))
    values.update(changes)
    return values


class DailyTests(unittest.TestCase):
    def test_important_future_daily_and_completed_excluded(self):
        work = task(remind_at=NOW+timedelta(days=20))
        normal = task(2, is_important=False, remind_at=NOW+timedelta(days=20))
        done = task(3, status="completed", completed_at=NOW)
        groups = tools.group_for_digest([work, normal, done], NOW)
        self.assertEqual(groups["important"], [work])
        self.assertEqual(sum(map(len, groups.values())), 1)
        text, ordered = format.digest(groups, NOW, False)
        self.assertEqual(ordered, [work])
        self.assertEqual(text.count(work.content), 1)
        self.assertIn("重要工作", text)
        self.assertIn("1 件", flavor.build_opening(groups, NOW))

    def test_daily_numbering_and_section(self):
        work, normal = task(), task(2, is_important=False)
        groups = tools.group_for_digest([normal, work], NOW)
        text, ordered = format.digest(groups, NOW, False)
        self.assertEqual([t.id for t in ordered], [1, 2])
        self.assertIn("1. ⭐", text)
        self.assertIn("2. 未定时间", text)
        scheduler = Scheduler(None, None, None, None)
        self.assertEqual(scheduler._section_of(work, NOW), "important")

    def test_confirmation_and_history(self):
        done = task(status="completed", completed_at=NOW, review_note="成果：方案评审通过")
        self.assertIn("08:00", format.created(task()))
        self.assertIn("停止每日提醒", format.completed(done))
        text = important.review_text([task(2), done], NOW-timedelta(days=7), NOW)
        for fact in ("仍在推进（全部） 1 件", "已完成 1 件", "完成于 2026-10-23 20:00", "方案评审通过"):
            self.assertIn(fact, text)
        self.assertNotIn("完成 N", text)

    def test_weekday_timezone_and_before_due(self):
        self.assertEqual(important.latest_review_moment(NOW, 4, 20, 0), NOW)
        self.assertEqual(important.latest_review_moment(NOW-timedelta(seconds=1), 4, 20, 0),
                         NOW-timedelta(days=7))
        self.assertEqual(important.latest_review_moment(NOW.astimezone(timezone.utc), 4, 20, 0), NOW)
        sunday = NOW+timedelta(days=2, hours=1)
        self.assertEqual(important.latest_review_moment(sunday, 6, 21, 0), sunday)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_important_without_explicit_time(self):
        client = LlmClient(SimpleNamespace(), 1000, 30)
        with patch("app.llm.tools.create_task", AsyncMock(return_value=task())) as create, \
             patch("app.llm.tools.list_tasks", AsyncMock(return_value=[task()])):
            _, confirmations, _ = await client._run_tool(
                None, "test-user", "create_reminder", {"content": "工作1", "is_important": True},
                inbox_id=None, pending=None)
        self.assertIsNone(create.call_args.kwargs["fire_at"])
        self.assertTrue(create.call_args.kwargs["is_important"])
        self.assertIn("08:00", confirmations[0])

    async def test_completed_note_does_not_reschedule_reminders(self):
        old = row(status="completed", completed_at=NOW)
        updated = dict(old, review_note="评审通过")
        db, cur = fake_db([old, updated])
        got = await important.edit_work(db, "test-user", 1, note="评审通过")
        self.assertEqual(got.review_note, "评审通过")
        self.assertEqual(got.status, "completed")
        sql = "\n".join(c.args[0] for c in cur.execute.call_args_list)
        self.assertNotIn("bot.deliveries", sql)
        self.assertNotIn("version = version + 1", sql)

    async def test_same_inbox_note_not_duplicated(self):
        old = row(review_note="已记录")
        db, cur = fake_db([old, {"exists": 1}, old])
        got = await important.edit_work(db, "test-user", 1, note="已记录", source_inbox_id=7)
        self.assertEqual(got.review_note, "已记录")

    async def test_mark_existing_preserves_reminder(self):
        old = row(is_important=False, remind_at=NOW)
        db, cur = fake_db([old, dict(old, is_important=True)])
        got = await important.edit_work(db, "test-user", 1, is_important=True)
        self.assertEqual(got.remind_at, NOW)
        self.assertEqual(got.version, 1)
        self.assertTrue(got.is_important)

    async def test_no_cross_user_or_invalid_note(self):
        db, _ = fake_db([None])
        with self.assertRaises(tools.ToolError):
            await important.edit_work(db, "other-user", 1, note="备注")
        with self.assertRaises(tools.ToolError):
            await important.edit_work(db, "test-user", 1, note=" ")

    async def test_weekly_due_and_duplicate(self):
        db, cur = fake_db([settings(), {"id": 9}])
        with patch("app.important.enqueue_text", AsyncMock(return_value=[22])) as enqueue:
            self.assertEqual(await important.ensure_weekly_review(db, "test-user", NOW), 1)
        self.assertEqual(enqueue.call_args.kwargs["business_key"], "work-review:9")
        self.assertEqual(enqueue.call_args.kwargs["kind"], "notice")
        self.assertIn("2026-10-16 20:00", enqueue.call_args.kwargs["text"])
        db, cur = fake_db([settings(), None])
        with patch("app.important.enqueue_text", AsyncMock()) as enqueue:
            self.assertEqual(await important.ensure_weekly_review(db, "test-user", NOW), 0)
            enqueue.assert_not_called()

    async def test_disabled_future_new_settings_and_old_missed_week(self):
        for cfg, now in ((None, NOW), (settings(enabled=False), NOW),
                         (settings(), NOW-timedelta(seconds=1)),
                         (settings(updated_at=NOW+timedelta(seconds=1)), NOW),
                         (settings(), NOW+timedelta(hours=25))):
            db, _ = fake_db([cfg])
            with patch("app.important.enqueue_text", AsyncMock()) as enqueue:
                self.assertEqual(await important.ensure_weekly_review(db, "test-user", now), 0)
                enqueue.assert_not_called()

    async def test_late_only_latest_review(self):
        db, _ = fake_db([settings(), {"id": 9}])
        with patch("app.important.enqueue_text", AsyncMock(return_value=[22])) as enqueue:
            await important.ensure_weekly_review(db, "test-user", NOW+timedelta(hours=10))
        self.assertIn("延迟补发", enqueue.call_args.kwargs["text"])
        self.assertEqual(enqueue.call_count, 1)

    async def test_setting_change_cancels_queued_old_reviews(self):
        old = settings()
        new = settings(weekday=6, hour=21)
        db, cur = fake_db([old, new])
        got = await important.review_settings(db, "test-user", weekday=6, hour=21)
        self.assertIn("每周日 21:00", important.settings_text(got))
        cancel_sql, params = cur.execute.call_args.args
        self.assertIn("work_review_runs", cancel_sql)
        self.assertIn("blocked_context", params[1])
        self.assertIn("blocked_auth", params[1])

    async def test_setting_validation(self):
        for args in (dict(hour=24), dict(weekday=-1), dict(minute=60), dict(hour=True), dict(enabled="false")):
            with self.assertRaises(tools.ToolError):
                await important.review_settings(None, "test-user", **args)

    async def test_review_date_inclusive_and_completed_status(self):
        client = LlmClient(SimpleNamespace(), 1000, 30)
        done = task(status="completed", completed_at=NOW)
        with patch("app.important.review_tasks", AsyncMock(return_value=[task(2), done])) as query:
            _, confirmations, ids = await client._run_tool(None, "test-user", "review_important_work",
                {"start_date": "2026-10-01", "end_date": "2026-10-23", "status": "completed"},
                inbox_id=None, pending=None)
        self.assertEqual(ids, [1])
        self.assertEqual(query.call_args.args[3], datetime(2026, 10, 24, tzinfo=tools.SHANGHAI))
        self.assertIn("已完成 1 件", confirmations[0])


if __name__ == "__main__":
    unittest.main()
