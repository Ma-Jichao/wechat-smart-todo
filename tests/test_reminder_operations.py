"""离线场景回归：使用伪造事项和模型响应，不读取配置、不请求外部服务。"""
import json
import sys
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import format, tools
from app.llm import AmbiguousMatch, LlmClient, LlmError
from app.llm import ACTION_NUM_RE

NOW = datetime(2026, 10, 28, 10, tzinfo=tools.SHANGHAI)


def task(task_id=801, **changes):
    values = dict(id=task_id, user_id="test-user", content="项目方案", status="open",
                  remind_at=None, remind_at_phrase=None, version=1, created_at=NOW)
    values.update(changes)
    return tools.Task(**values)


def response(*calls, text=""):
    return {"choices": [{"message": {"content": text, "tool_calls": [
        {"id": f"call-{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)]}}]}


def client():
    return LlmClient(SimpleNamespace(model="fake-model"), 2000, 30)


def context_db():
    state = {"value": None}

    async def fetch_one(sql, params=()):
        if "bot.conversations" in sql and state["value"] is not None:
            return {"state": state["value"], "expires_at": NOW+timedelta(minutes=30)}
        return None

    async def execute(sql, params=()):
        if "DELETE FROM bot.conversations" in sql:
            state["value"] = None
        elif "INSERT INTO bot.conversations" in sql:
            state["value"] = json.loads(json.dumps(params[1].obj))

    return SimpleNamespace(fetch_one=AsyncMock(side_effect=fetch_one),
                           execute=AsyncMock(side_effect=execute)), state


def update_db(row):
    cur = SimpleNamespace(execute=AsyncMock(), fetchone=AsyncMock(return_value=row))

    @asynccontextmanager
    async def transaction():
        yield cur

    return SimpleNamespace(transaction=transaction), cur


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_type_filter_keeps_same_named_important_work(self):
        daily, work = task(repeat_times=["08:00"]), task(802, is_important=True)
        with patch("app.llm.tools.find_tasks_by_content", AsyncMock(return_value=[daily, work])):
            found = await client()._resolve_task_id(None, "test-user", {
                "match_text": "项目", "task_type": "recurring"}, None)
        self.assertEqual(found, daily.id)

    async def test_explicit_id_cannot_bypass_requested_type(self):
        with patch("app.llm.tools.get_task", AsyncMock(return_value=task(is_important=True))):
            with self.assertRaises(tools.ToolError):
                await client()._resolve_task_id(None, "test-user", {
                    "task_id": 801, "task_type": "recurring"}, None)

    async def test_fixed_heading_is_not_used_as_task_name(self):
        with patch("app.llm.tools.find_tasks_by_content", AsyncMock(return_value=[])), \
             patch("app.llm.tools.list_tasks", AsyncMock(return_value=[task(repeat_times=["08:00"])])):
            self.assertEqual(await client()._resolve_task_id(None, "test-user", {
                "match_text": "⏰ 每日提醒", "task_type": "recurring"}, None), 801)

    async def test_ambiguous_choices_include_type_and_time(self):
        daily, work = task(repeat_times=["08:00"]), task(802, is_important=True)
        with patch("app.llm.tools.find_tasks_by_content", AsyncMock(return_value=[daily, work])):
            with self.assertRaises(AmbiguousMatch) as caught:
                await client()._resolve_task_id(None, "test-user", {"match_text": "项目"}, None)
        text = format.choose_task(caught.exception.candidates)
        for expected in ("每日重复 08:00", "重要工作", "尚未修改", "第2个"):
            self.assertIn(expected, text)
        self.assertNotIn("801", text)
        self.assertNotIn("802", text)


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_preserves_important_and_cancels_old_delivery(self):
        old = task(is_important=True, remind_at=NOW+timedelta(days=1))
        new = replace(old, remind_at=None, version=2)
        db, cur = update_db(vars(new))
        with patch("app.tools.get_task", AsyncMock(return_value=old)), \
             patch("app.tools._supersede_deliveries", AsyncMock()) as supersede, \
             patch("app.tools._schedule_reminder", AsyncMock()) as schedule:
            got = await tools.update_task(db, "test-user", old.id, stop_reminders=True)
        self.assertEqual(got.status, "open")
        self.assertTrue(got.is_important)
        self.assertIsNone(got.remind_at)
        supersede.assert_awaited_once()
        self.assertIsNone(schedule.call_args.args[1].remind_at)
        text = format.updated(got, before=old, stopped=True)
        self.assertIn("2026年10月29日", text)
        self.assertIn("未标记完成", text)
        self.assertIn("每日早报保留", text)

    async def test_stop_recurring_clears_date_range_and_keeps_open(self):
        old = task(repeat_times=["08:00"], repeat_start=NOW.date(),
                   repeat_end=(NOW+timedelta(days=3)).date(), skip_holidays=True)
        new = replace(old, repeat_times=[], repeat_start=None, repeat_end=None,
                      skip_holidays=False, version=2)
        db, cur = update_db(vars(new))
        with patch("app.tools.get_task", AsyncMock(return_value=old)), \
             patch("app.tools._supersede_deliveries", AsyncMock()):
            got = await tools.update_task(db, "test-user", old.id, stop_reminders=True)
        params = cur.execute.call_args_list[0].args[1]
        self.assertEqual(params[3:7], ([], None, None, False))
        self.assertEqual(got.status, "open")

    async def test_recurring_reschedule_keeps_range(self):
        old = task(repeat_times=["08:00"], repeat_start=NOW.date(), skip_holidays=True)
        new = replace(old, repeat_times=["09:30"], version=2)
        db, cur = update_db(vars(new))
        with patch("app.tools.get_task", AsyncMock(return_value=old)), \
             patch("app.tools._supersede_deliveries", AsyncMock()):
            got = await tools.update_task(db, "test-user", old.id, repeat_times=["09:30"])
        self.assertEqual(cur.execute.call_args_list[0].args[1][3:7], (["09:30"], NOW.date(), None, True))
        self.assertIn("09:30", format.updated(got, before=old))

    async def test_recurring_does_not_silently_accept_single_time(self):
        old = task(repeat_times=["08:00"])
        db, cur = update_db(None)
        with patch("app.tools.get_task", AsyncMock(return_value=old)):
            with self.assertRaises(tools.ToolError):
                await tools.update_task(db, "test-user", old.id, fire_at=NOW)
        cur.execute.assert_not_awaited()

    async def test_concurrent_version_change_does_not_reschedule(self):
        db, cur = update_db(None)
        with patch("app.tools.get_task", AsyncMock(return_value=task())), \
             patch("app.tools._supersede_deliveries", AsyncMock()) as supersede:
            with self.assertRaisesRegex(tools.ToolError, "状态刚刚发生变化"):
                await tools.update_task(db, "test-user", 801, content="新方案")
        supersede.assert_not_awaited()


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch("app.tools.now_shanghai", return_value=NOW)
        self.history = patch("app.llm.tools.recent_seen_messages", AsyncMock(return_value=[]))
        self.clock.start()
        self.history.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.history.stop)
        self.db, self.state = context_db()

    async def test_selection_survives_new_client_and_cancels_only_chosen_type(self):
        daily, work = task(repeat_times=["08:00"]), task(802, is_important=True)
        first = client()
        first._post = AsyncMock(return_value=response(("cancel_reminder", {"match_text": "项目"})))
        with patch("app.llm.tools.find_tasks_by_content", AsyncMock(return_value=[daily, work])):
            result = await first.handle(self.db, "test-user", "取消项目方案", inbox_id=None)
        self.assertIn("尚未修改", result.reply)
        second = client()  # 模拟进程重启后，只有持久化会话可用。
        second._post = AsyncMock()
        with patch("app.llm.tools.get_task", AsyncMock(return_value=daily)), \
             patch("app.llm.tools.find_tasks_by_content", AsyncMock(return_value=[daily, work])), \
             patch("app.llm.tools.cancel_task", AsyncMock(return_value=replace(daily, status="canceled"))) as cancel:
            result = await second.handle(self.db, "test-user", "第一个", inbox_id=None)
        cancel.assert_awaited_once_with(self.db, "test-user", daily.id)
        second._post.assert_not_awaited()
        self.assertIn("仍保留同名事项：重要工作", result.reply)
        self.assertIsNone(self.state['value'])

    async def test_latest_list_numbers_override_digest_and_model_id(self):
        self.state['value'] = {"listing": [802, 801]}
        c = client()
        c._post = AsyncMock(side_effect=[response(("complete_reminder", {"task_id": 999})), response()])
        c._resolve_digest_reference = AsyncMock(return_value=("完成 2", 999))
        with patch("app.llm.tools.get_task", AsyncMock(return_value=task())), \
             patch("app.llm.tools.complete_task", AsyncMock(return_value=replace(task(), status="completed"))) as complete:
            result = await c.handle(self.db, "test-user", "完成 2", inbox_id=None)
        complete.assert_awaited_once_with(self.db, "test-user", 801)
        c._resolve_digest_reference.assert_not_awaited()
        self.assertEqual(result.task_ids, [801])

    async def test_out_of_range_does_not_fall_back_to_another_list(self):
        self.state['value'] = {"listing": [801]}
        c = client()
        c._post = AsyncMock()
        result = await c.handle(self.db, "test-user", "完成 2", inbox_id=None)
        self.assertIn("本次没有修改", result.reply)
        c._post.assert_not_awaited()

    async def test_empty_list_never_uses_an_older_digest_number(self):
        self.state['value'] = {"listing": []}
        c = client()
        c._post = AsyncMock()
        c._resolve_digest_reference = AsyncMock(return_value=("完成 1", 801))
        result = await c.handle(self.db, "test-user", "完成 1", inbox_id=None)
        self.assertIn("本次没有修改", result.reply)
        c._resolve_digest_reference.assert_not_awaited()
        c._post.assert_not_awaited()

    async def test_expired_context_requires_a_new_query(self):
        c = client()
        c._load_pending = AsyncMock(return_value={"expired": True, "listed_at": NOW.isoformat()})
        c._post = AsyncMock()
        result = await c.handle(self.db, "test-user", "完成 1", inbox_id=None)
        self.assertIn("已过期", result.reply)
        c._post.assert_not_awaited()

    async def test_newer_digest_takes_precedence_over_old_listing(self):
        self.state['value'] = {"listing": [802], "listed_at": (NOW-timedelta(minutes=10)).isoformat()}
        original = self.db.fetch_one.side_effect

        async def fetch_one(sql, params=()):
            if "SELECT generated_at FROM bot.digests" in sql:
                return {"generated_at": NOW-timedelta(minutes=5)}
            return await original(sql, params)

        self.db.fetch_one.side_effect = fetch_one
        c = client()
        c._resolve_digest_reference = AsyncMock(return_value=("完成 1", 801))
        c._post = AsyncMock(side_effect=[response(("complete_reminder", {})), response()])
        with patch("app.llm.tools.get_task", AsyncMock(return_value=task())), \
             patch("app.llm.tools.complete_task", AsyncMock(return_value=replace(task(), status="completed"))) as complete:
            await c.handle(self.db, "test-user", "完成 1", inbox_id=None)
        complete.assert_awaited_once_with(self.db, "test-user", 801)

    async def test_count_statement_is_not_an_ordinal(self):
        self.assertIsNone(ACTION_NUM_RE.search("完成 12件事"))
        self.assertIsNone(ACTION_NUM_RE.search("完成 12 件事"))
        self.assertEqual(ACTION_NUM_RE.search("完成 12").group(1), "12")

    async def test_overview_reads_settings_without_enabling_weekly_review(self):
        c = LlmClient(SimpleNamespace(), 2000, 30, digest_hour=9, digest_minute=15)
        items = [task(repeat_times=["08:00"]), task(802, is_important=True), task(803)]
        with patch("app.llm.tools.list_tasks", AsyncMock(return_value=items)):
            _, cs, ids = await c._run_tool(self.db, "test-user", "list_reminders", {"scope": "overview"}, inbox_id=None, pending=None)
        self.assertEqual(ids, [802, 803, 801])
        for expected in ("09:15", "每日重复提醒 1 件", "重要工作 1 件", "普通待办 1 件", "尚未启用"):
            self.assertIn(expected, cs[0])
        self.db.execute.assert_not_awaited()

    async def test_overview_saves_display_order_for_next_message(self):
        c = client()
        c._post = AsyncMock(side_effect=[response(("list_reminders", {"scope": "overview"})), response()])
        with patch("app.llm.tools.list_tasks", AsyncMock(return_value=[task(), task(802, is_important=True)])):
            await c.handle(self.db, "test-user", "现在有哪些提醒", inbox_id=None)
        self.assertEqual(self.state['value']['listing'], [802, 801])

    async def test_all_query_also_includes_recurring_and_weekly_settings(self):
        c = client()
        with patch("app.llm.tools.list_tasks", AsyncMock(return_value=[task(repeat_times=["08:00"])])) as query:
            _, confirmations, ids = await c._run_tool(self.db, "test-user", "list_reminders",
                {"scope": "all"}, inbox_id=None, pending=None)
        self.assertEqual(query.call_args.args[2], tools.SCOPE_OVERVIEW)
        self.assertIn("每周汇总", confirmations[0])
        self.assertEqual(ids, [801])

    async def test_tool_failure_cannot_be_hidden_by_success_prose(self):
        c = client()
        c._post = AsyncMock(return_value=response(("cancel_reminder", {"match_text": "项目"}), text="已经全部取消"))
        c._run_tool = AsyncMock(side_effect=tools.ToolError("事项已完成，本次没有取消。"))
        result = await c.handle(self.db, "test-user", "取消项目", inbox_id=None)
        self.assertIn("本次没有取消", result.reply)
        self.assertNotIn("已经全部取消", result.reply)
        self.assertEqual(c._post.await_count, 1)

    async def test_model_failure_after_commit_preserves_success_feedback(self):
        c = client()
        c._post = AsyncMock(side_effect=[response(("create_reminder", {"content": "方案"})), LlmError("fake timeout")])
        c._run_tool = AsyncMock(return_value=("创建成功", ["✅ 方案已保存"], [801]))
        result = await c.handle(self.db, "test-user", "记方案", inbox_id=None)
        self.assertIn("方案已保存", result.reply)
        self.assertIn("后续理解中断", result.reply)
        self.assertNotIn("没有保存任何内容", result.reply)

    async def test_partial_success_and_failure_are_both_visible(self):
        c = client()
        c._post = AsyncMock(return_value=response(("create_reminder", {"content": "甲"}), ("create_reminder", {"content": "乙"})))
        c._run_tool = AsyncMock(side_effect=[("成功", ["✅ 甲已保存"], [801]), tools.ToolError("乙的时间已过，请确认日期。")])
        result = await c.handle(self.db, "test-user", "记甲和乙", inbox_id=None)
        self.assertIn("甲已保存", result.reply)
        self.assertIn("乙的时间已过", result.reply)
        self.assertEqual(result.task_ids, [801])

    async def test_identical_tool_calls_execute_once(self):
        c = client()
        args = {"task_id": 801}
        c._post = AsyncMock(side_effect=[response(("complete_reminder", args), ("complete_reminder", args)), response()])
        c._run_tool = AsyncMock(return_value=("完成", ["✅ 已完成项目"], [801]))
        result = await c.handle(self.db, "test-user", "项目完成", inbox_id=None)
        c._run_tool.assert_awaited_once()
        self.assertEqual(result.reply.count("已完成项目"), 1)

    async def test_success_without_a_tool_is_not_confirmed(self):
        for prose in ("已经取消项目方案", "取消了项目方案", "改好了", "记下了", "好嘞"):
            with self.subTest(prose=prose):
                c = client()
                c._post = AsyncMock(return_value=response(text=prose))
                result = await c.handle(self.db, "test-user", "取消项目方案", inbox_id=None)
                self.assertIn("尚未执行任何变更", result.reply)

    async def test_model_question_cannot_add_unverified_success_claims(self):
        c = client()
        c._post = AsyncMock(side_effect=[response(("create_reminder", {"content": "甲"})),
                                        response(text="已经全部取消，还有别的吗？")])
        c._run_tool = AsyncMock(return_value=("成功", ["✅ 甲已保存"], [801]))
        result = await c.handle(self.db, "test-user", "记下甲", inbox_id=None)
        self.assertIn("甲已保存", result.reply)
        self.assertNotIn("全部取消", result.reply)

    async def test_context_write_failure_does_not_turn_committed_work_into_failure(self):
        c = client()
        c._post = AsyncMock(side_effect=[response(("create_reminder", {"content": "甲"})), response()])
        c._run_tool = AsyncMock(return_value=("成功", ["✅ 甲已保存"], [801]))
        c._save_pending = AsyncMock(side_effect=RuntimeError("fake storage error"))
        result = await c.handle(self.db, "test-user", "记甲", inbox_id=None)
        self.assertIn("甲已保存", result.reply)
        self.assertIn("序号未能保存", result.reply)


if __name__ == '__main__':
    unittest.main()
