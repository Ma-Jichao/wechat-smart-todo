"""离线回归：重复创建不得写入第二条事项，也不得新增投递。"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import tools
from app.llm import LlmClient, TOOL_SCHEMAS

NOW = datetime(2026, 10, 28, 8, tzinfo=tools.SHANGHAI)


def fake_db(responses):
    cur = SimpleNamespace(execute=AsyncMock(), fetchone=AsyncMock(side_effect=responses))

    @asynccontextmanager
    async def transaction():
        yield cur

    return SimpleNamespace(transaction=transaction), cur


class CreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_work_and_reminder_do_not_write_or_schedule(self):
        for changes in ({}, {"is_important": True}, {"repeat_times": ["08:00"]}):
            with self.subTest(changes=changes):
                db, cur = fake_db([{"id": 1}])
                with patch("app.tools._schedule_reminder", AsyncMock()) as schedule:
                    with self.assertRaisesRegex(tools.ToolError, "未重复创建"):
                        await tools.create_task(
                            db, "test-user", "项目材料", fire_at=None, phrase=None,
                            source_inbox_id=None, max_chars=2000, **changes)
                schedule.assert_not_awaited()
                self.assertFalse(any("INSERT" in c.args[0] for c in cur.execute.call_args_list))

    async def test_new_work_keeps_normal_creation_path(self):
        row = dict(id=1, user_id="test-user", content="项目材料", status="open",
                   remind_at=None, remind_at_phrase=None, version=1, created_at=NOW)
        db, cur = fake_db([None, row])
        with patch("app.tools._schedule_reminder", AsyncMock()) as schedule:
            task = await tools.create_task(
                db, "test-user", "项目材料", fire_at=None, phrase=None,
                source_inbox_id=None, max_chars=2000)
        self.assertEqual(task.id, 1)
        schedule.assert_awaited_once()
        self.assertTrue(any("INSERT INTO bot.tasks" in c.args[0] for c in cur.execute.call_args_list))

    async def test_schedule_identity_ignores_phrase_and_time_order(self):
        db, cur = fake_db([{"id": 1}])
        with self.assertRaises(tools.ToolError):
            await tools.create_task(
                db, "test-user", " 项目材料 ", fire_at=NOW, phrase="换个说法",
                source_inbox_id=None, max_chars=2000,
                repeat_times=["18:00", "08:00", "08:00"],
                skip_holidays=True, repeat_start=NOW.date(),
                repeat_end=(NOW+timedelta(days=3)).date())
        params = cur.execute.call_args_list[1].args[1]
        self.assertEqual(params, ("test-user", "项目材料", None,
                                 ["08:00", "18:00"], ["08:00", "18:00"],
                                 True, NOW.date(), (NOW+timedelta(days=3)).date(), False))

    async def test_model_can_list_recurring_reminders_separately(self):
        schema = next(s["function"] for s in TOOL_SCHEMAS
                      if s["function"]["name"] == "list_reminders")
        self.assertIn("recurring", schema["parameters"]["properties"]["scope"]["enum"])
        work = tools.Task(1, "test-user", "项目材料", "open", None, None, 1, NOW,
                          repeat_times=["08:00"])
        client = LlmClient(SimpleNamespace(), 2000, 30)
        with patch("app.llm.tools.list_tasks", AsyncMock(return_value=[work])) as listing:
            _, confirmations, ids = await client._run_tool(
                None, "test-user", "list_reminders", {"scope": "recurring"},
                inbox_id=None, pending=None)
        self.assertEqual(listing.call_args.args[2], tools.SCOPE_RECURRING)
        self.assertEqual(ids, [1])
        self.assertIn(work.content, confirmations[0])


if __name__ == "__main__":
    unittest.main()
