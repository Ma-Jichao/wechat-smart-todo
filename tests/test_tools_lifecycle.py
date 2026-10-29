"""业务操作层测试：事项状态与投递状态必须分离，且版本变更要联动投递。

打真实 IvorySQL，用独立 user_id 隔离，结束时清理。
"""

from __future__ import annotations

import os

import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import load_test_config
from app import tools
from app.config import load
from app.db import Database
from app.tools import ToolError
from app.llm import LlmClient
from types import SimpleNamespace

MAX_CHARS = 2000


async def scenario() -> None:
    base = load_test_config()
    user_id = f"test-user-{uuid.uuid4()}"
    db = Database(base.db, min_size=1, max_size=2)
    await db.open()

    async def cleanup() -> None:
        await db.execute(
            "DELETE FROM bot.task_changes WHERE task_id IN "
            "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user_id,)
        )
        await db.execute(
            "DELETE FROM bot.deliveries WHERE task_id IN "
            "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user_id,)
        )
        await db.execute("DELETE FROM bot.tasks WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.messages_seen WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.conversations WHERE user_id = %s", (user_id,))
        await db.execute("DELETE FROM bot.work_review_settings WHERE user_id = %s", (user_id,))

    try:
        now = tools.now_shanghai()
        # 同一内容、同一时间的并发工具调用只能落一条；不同时间允许创建。
        async def create_same(moment):
            return await tools.create_task(
                db, user_id, "并发去重测试", fire_at=moment, phrase=None,
                source_inbox_id=None, max_chars=MAX_CHARS)

        results = await asyncio.gather(
            create_same(now + timedelta(days=1)),
            create_same(now + timedelta(days=1)), return_exceptions=True)
        assert sum(isinstance(r, tools.Task) for r in results) == 1, results
        assert sum(isinstance(r, ToolError) for r in results) == 1, results
        first = next(r for r in results if isinstance(r, tools.Task))
        second = await create_same(now + timedelta(days=2))
        assert first.id != second.id
        await tools.cancel_task(db, user_id, first.id)
        recreated = await create_same(now + timedelta(days=1))
        assert recreated.id != first.id
        await tools.cancel_task(db, user_id, second.id)
        await tools.cancel_task(db, user_id, recreated.id)
        print("[通过] 并发创建去重、不同时间保留、取消后允许重新创建")
        # 分组与建事项都锚在「当天 08:00」，避免测试依赖运行时刻
        # （晚上跑的话 now+2h 会跨到第二天，today 分组就空了）
        anchor = now.replace(hour=8, minute=0, second=0, microsecond=0)

        # ---- 用例 1：创建 → 事项落库 + 到点投递被安排 -------------------------
        task = await tools.create_task(
            db, user_id, "整理项目材料",
            fire_at=now + timedelta(hours=3), phrase="三个小时后",
            source_inbox_id=None, max_chars=MAX_CHARS,
        )
        assert task.version == 1 and task.status == "open", task
        rows = await db.fetch_all(
            "SELECT business_key, status, scheduled_at, next_attempt_at "
            "FROM bot.deliveries WHERE task_id = %s", (task.id,)
        )
        assert len(rows) == 1, rows
        assert rows[0]["business_key"] == f"reminder:{task.id}:v1", rows
        assert rows[0]["status"] == "pending", rows
        assert rows[0]["scheduled_at"] == rows[0]["next_attempt_at"], rows
        print("[通过] 用例 1：创建事项并安排到点投递")

        # ---- 用例 2：改时间 → 版本递增，旧投递作废，新投递安排 ------------------
        original_scheduled = rows[0]["scheduled_at"]
        moved = await tools.update_task(
            db, user_id, task.id, fire_at=now + timedelta(days=1), phrase="明天"
        )
        assert moved.version == 2, moved
        rows = await db.fetch_all(
            "SELECT business_key, status, scheduled_at FROM bot.deliveries "
            "WHERE task_id = %s ORDER BY id", (task.id,)
        )
        by_key = {r["business_key"]: r for r in rows}
        assert by_key[f"reminder:{task.id}:v1"]["status"] == "superseded", by_key
        assert by_key[f"reminder:{task.id}:v2"]["status"] == "pending", by_key
        # 原定时间本身没被重试篡改，只是换成了新的原定时间
        assert by_key[f"reminder:{task.id}:v2"]["scheduled_at"] != original_scheduled
        print("[通过] 用例 2：改时间后旧投递 superseded、新版本投递 pending")

        # ---- 用例 3：改内容不重复造投递 ---------------------------------------
        renamed = await tools.update_task(db, user_id, task.id, content="整理最终材料")
        assert renamed.version == 3, renamed
        rows = await db.fetch_all(
            "SELECT business_key, status FROM bot.deliveries WHERE task_id = %s", (task.id,)
        )
        by_key = {r["business_key"]: r["status"] for r in rows}
        assert by_key[f"reminder:{task.id}:v3"] == "pending", by_key
        assert by_key[f"reminder:{task.id}:v2"] == "superseded", by_key
        print("[通过] 用例 3：改内容同样递增版本并换投递，不产生重复")

        # ---- 用例 4：完成 → 事项保留历史，未发出的投递被取消 -------------------
        done = await tools.complete_task(db, user_id, task.id)
        assert done.status == "completed", done
        live = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.deliveries "
            "WHERE task_id = %s AND status = ANY(%s)",
            (task.id, list(tools.LIVE_DELIVERY_STATUS)),
        )
        assert live["n"] == 0, live
        still_there = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.tasks WHERE id = %s", (task.id,)
        )
        assert still_there["n"] == 1, "完成不应删除事项记录"
        print("[通过] 用例 4：完成后投递被取消，事项记录保留")

        # ---- 用例 5：已完成的事项不能再改 -------------------------------------
        try:
            await tools.update_task(db, user_id, task.id, content="再改一次")
            raise AssertionError("已完成事项不应允许修改")
        except ToolError as exc:
            assert "completed" in str(exc), exc
        print("[通过] 用例 5：已完成事项拒绝修改")

        # ---- 用例 6：过去时间必须被拒绝，不能悄悄改到第二天 --------------------
        try:
            tools.validate_fire_at(now - timedelta(days=1))
            raise AssertionError("过去时间不应通过校验")
        except ToolError as exc:
            assert "过去了" in str(exc), exc
        assert tools.validate_fire_at(now + timedelta(minutes=10))
        print("[通过] 用例 6：过去时间被拒绝并要求回问用户")

        # ---- 用例 7：早报分组不重复，逾期优先 ---------------------------------
        overdue = await tools.create_task(
            db, user_id, "逾期的事", fire_at=anchor - timedelta(minutes=30),
            phrase=None, source_inbox_id=None, max_chars=MAX_CHARS)
        today_later = await tools.create_task(
            db, user_id, "今天晚些的事", fire_at=anchor + timedelta(hours=2),
            phrase=None, source_inbox_id=None, max_chars=MAX_CHARS)
        someday = await tools.create_task(
            db, user_id, "后天的事", fire_at=anchor + timedelta(days=2),
            phrase=None, source_inbox_id=None, max_chars=MAX_CHARS)
        no_time = await tools.create_task(
            db, user_id, "先记着的事", fire_at=None, phrase=None,
            source_inbox_id=None, max_chars=MAX_CHARS)

        open_tasks = await tools.list_tasks(db, user_id, tools.SCOPE_ALL)
        groups = tools.group_for_digest(open_tasks, anchor)
        ids = {k: [t.id for t in v] for k, v in groups.items()}

        assert overdue.id in ids["overdue"], ids
        assert today_later.id in ids["today"], ids
        assert no_time.id in ids["unscheduled"], ids
        assert someday.id not in ids["today"] + ids["overdue"] + ids["unscheduled"], ids
        assert overdue.id not in ids["today"], "逾期和今天不能重复出现"
        print("[通过] 用例 7：早报分组正确（今天/逾期/未设时间，逾期不重复，未来日期不入早报）")

        # ---- 用例 8：未设提醒时间的事项不产生到点投递 --------------------------
        rows = await db.fetch_all(
            "SELECT count(*) AS n FROM bot.deliveries WHERE task_id = %s", (no_time.id,)
        )
        assert rows[0]["n"] == 0, rows
        print("[通过] 用例 8：未设提醒时间的事项不产生到点投递")

        # ---- 用例 9：编号不存在或不属于本人时给出明确错误 ----------------------
        try:
            await tools.complete_task(db, user_id, 999999999)
            raise AssertionError("不存在的编号应报错")
        except ToolError as exc:
            assert "没找到" in str(exc), exc
        print("[通过] 用例 9：越权/不存在的编号被拒绝")

        # ---- 用例 10：每天重复提醒 -----------------------------------------
        recurring = await tools.create_task(
            db, user_id, "打卡", fire_at=None, phrase=None,
            source_inbox_id=None, max_chars=MAX_CHARS,
            repeat_times=["08:15", "18:00"], skip_holidays=True,
        )
        assert recurring.repeat_times == ["08:15", "18:00"], recurring
        assert recurring.skip_holidays is True, recurring
        assert recurring.remind_at is None, recurring
        rows = await db.fetch_all(
            "SELECT count(*) AS n FROM bot.deliveries WHERE task_id = %s", (recurring.id,))
        assert rows[0]["n"] == 0, "重复提醒不应安排一次性到点投递"
        all_tasks = await tools.list_tasks(db, user_id, tools.SCOPE_ALL)
        assert recurring.id not in [t.id for t in all_tasks], all_tasks
        rec_tasks = await tools.list_tasks(db, user_id, tools.SCOPE_RECURRING)
        assert recurring.id in [t.id for t in rec_tasks], rec_tasks
        print("[通过] 用例 10：每天重复提醒不进待办列表，可单独按 recurring 查")

        # ---- 用例 11：带起止日的每天重复提醒 --------------------------------
        ranged = await tools.create_task(
            db, user_id, "更新汇报", fire_at=None, phrase=None,
            source_inbox_id=None, max_chars=MAX_CHARS,
            repeat_times=["08:00"], skip_holidays=False,
            repeat_start=date(2026, 10, 1), repeat_end=date(2026, 10, 6),
        )
        assert ranged.repeat_start == date(2026, 10, 1), ranged
        assert ranged.repeat_end == date(2026, 10, 6), ranged
        assert ranged.remind_at is None, ranged
        rows = await db.fetch_all(
            "SELECT count(*) AS n FROM bot.deliveries WHERE task_id = %s", (ranged.id,))
        assert rows[0]["n"] == 0, "重复提醒不应安排一次性到点投递"
        all_tasks = await tools.list_tasks(db, user_id, tools.SCOPE_ALL)
        assert ranged.id not in [t.id for t in all_tasks], all_tasks
        rec_tasks = await tools.list_tasks(db, user_id, tools.SCOPE_RECURRING)
        assert ranged.id in [t.id for t in rec_tasks], rec_tasks

        # 起止日非法时应拒绝
        try:
            await tools.create_task(
                db, user_id, "非法区间", fire_at=None, phrase=None,
                source_inbox_id=None, max_chars=MAX_CHARS,
                repeat_times=["08:00"], repeat_start=date(2026, 10, 6),
                repeat_end=date(2026, 10, 1))
            raise AssertionError("起始晚于结束应报错")
        except ToolError as exc:
            assert "起始日期" in str(exc), exc
        try:
            await tools.create_task(
                db, user_id, "只有起止没时间", fire_at=None, phrase=None,
                source_inbox_id=None, max_chars=MAX_CHARS,
                repeat_start=date(2026, 10, 1))
            raise AssertionError("只有起止日期、没有每天时间点应报错")
        except ToolError as exc:
            assert "每天重复" in str(exc), exc
        print("[通过] 用例 11：带起止日的每日提醒落库、不进待办列表、非法区间被拒")

        # 完整事务路径：重复时刻修改、停止独立提醒、同名类型定位与统一查询。
        moved_repeat = await tools.update_task(db, user_id, recurring.id, repeat_times=["09:30"])
        assert moved_repeat.repeat_times == ["09:30"] and moved_repeat.skip_holidays
        stopped = await tools.update_task(db, user_id, ranged.id, stop_reminders=True)
        assert stopped.status == "open" and not stopped.repeat_times
        assert stopped.repeat_start is None and stopped.repeat_end is None
        work = await tools.create_task(
            db, user_id, "同名类型测试", fire_at=now+timedelta(days=1), phrase=None,
            source_inbox_id=None, max_chars=MAX_CHARS, is_important=True)
        work = await tools.update_task(db, user_id, work.id, stop_reminders=True)
        assert work.is_important and work.status == "open" and work.remind_at is None
        live = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.deliveries WHERE task_id=%s AND status=ANY(%s)",
            (work.id, list(tools.LIVE_DELIVERY_STATUS)))
        assert live["n"] == 0
        daily = await tools.create_task(
            db, user_id, work.content, fire_at=None, phrase=None,
            source_inbox_id=None, max_chars=MAX_CHARS, repeat_times=["08:00"])
        client = LlmClient(SimpleNamespace(), MAX_CHARS, 30)
        _, confirmations, ids = await client._run_tool(db, user_id, "cancel_reminder",
            {"match_text": work.content, "task_type": "recurring"}, inbox_id=None, pending=None)
        assert ids == [daily.id] and "仍保留同名事项：重要工作" in confirmations[0]
        assert (await tools.get_task(db, user_id, work.id)).status == "open"
        _, confirmations, ids = await client._run_tool(db, user_id, "list_reminders",
            {"scope": "overview"}, inbox_id=None, pending=None)
        assert work.id in ids and recurring.id in ids and daily.id not in ids
        assert "每周汇总" in confirmations[0] and "待办早报" in confirmations[0]
        await client._save_pending(db, user_id, {"listing": ids})
        restored = await LlmClient(SimpleNamespace(), MAX_CHARS, 30)._load_pending(db, user_id)
        assert restored["listing"] == ids
        print("[通过] 修改重复时刻、停止提醒保留工作、同名类型取消、统一查询与序号持久化")
    finally:
        await cleanup()
        await db.close()


def main() -> int:
    asyncio.run(scenario())
    print("\n业务操作层全部用例通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
