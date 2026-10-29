"""隔离 IvorySQL 集成测试。必须先迁移，显式 REMINDER_TEST_CONFIG；不发送微信。"""
import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from support import load_test_config
from app import important, tools
from app.db import Database
from app.llm import LlmClient
from app.scheduler import Scheduler


async def scenario():
    cfg = load_test_config()
    db = Database(cfg.db, min_size=1, max_size=2)
    await db.open()
    user = f"test-work-{uuid.uuid4()}"
    other = f"test-work-{uuid.uuid4()}"
    now = tools.now_shanghai()
    # 最近期的周五 20:00；固定锚点，不依赖测试运行星期或时刻。
    due = important.latest_review_moment(now, 4, 20, 0)
    client = LlmClient(cfg.deepseek, cfg.limits.max_text_chars, 30)
    try:
        async def create(content, **kwargs):
            return await tools.create_task(db, user, content, fire_at=None, phrase=None,
                                          source_inbox_id=None, max_chars=1000, **kwargs)

        open_work = await create("长期推进方案", is_important=True)
        done = await create("本周完成评审", is_important=True)
        older = await create("往期成果", is_important=True)
        await create("普通事项")
        await tools.complete_task(db, user, done.id)
        await tools.complete_task(db, user, older.id)
        await db.execute("UPDATE bot.tasks SET completed_at = %s WHERE id = %s",
                         (due-timedelta(days=1), done.id))
        await db.execute("UPDATE bot.tasks SET completed_at = %s WHERE id = %s",
                         (due-timedelta(days=8), older.id))
        await important.edit_work(db, user, done.id, note="成果已通过评审")
        await important.edit_work(db, user, done.id, note="下次提前沟通")
        saved = await tools.get_task(db, user, done.id)
        assert saved.review_note == "成果已通过评审\n下次提前沟通"
        assert saved.status == "completed"
        assert (await important.review_settings(db, user))["weekday"] == 4
        assert (await important.review_settings(db, user))["hour"] == 20

        found = await important.review_tasks(db, user, due-timedelta(days=7), due)
        assert {t.id for t in found} == {open_work.id, done.id}
        assert await important.review_tasks(db, other) == []
        all_history = await important.review_tasks(db, user)
        assert older.id in {t.id for t in all_history}

        # 实际生成早报并验证序号映射、未来日期重要事项与完成排除。
        await db.execute("UPDATE bot.tasks SET remind_at = %s WHERE id = %s",
                         (now+timedelta(days=30), open_work.id))
        from dataclasses import replace
        scheduler = Scheduler(db, replace(cfg.weixin, allowed_user_id=user), cfg.digest, cfg.limits)
        await scheduler.generate_digest(now.date(), late=False)
        mapping = await db.fetch_all("SELECT di.* FROM bot.digest_items di JOIN bot.digests d "
                                    "ON d.id = di.digest_id WHERE d.user_id = %s ORDER BY position", (user,))
        assert mapping[0]["task_id"] == open_work.id and mapping[0]["section"] == "important"
        assert done.id not in {r["task_id"] for r in mapping}

        await db.execute("UPDATE bot.work_review_settings SET updated_at = %s WHERE user_id = %s",
                         (due-timedelta(days=10), user))
        # 同时调度也只产生一期，run 和分段消息在同一事务提交。
        results = await asyncio.gather(important.ensure_weekly_review(db, user, due),
                                       important.ensure_weekly_review(db, user, due))
        assert sum(results) == 1, results
        assert await important.ensure_weekly_review(db, user, due) == 0
        deliveries = await db.fetch_all("SELECT * FROM bot.deliveries WHERE user_id = %s "
                                        "AND kind = 'notice'", (user,))
        assert len(deliveries) == 1
        text = deliveries[0]["content_snapshot"]
        assert "长期推进方案" in text and "本周完成评审" in text and "往期成果" not in text
        await db.execute("UPDATE bot.deliveries SET status = 'blocked_context' WHERE id = %s",
                         (deliveries[0]["id"],))
        changed = await important.review_settings(db, user, weekday=6, hour=21, minute=30)
        assert (changed["weekday"], changed["hour"], changed["minute"]) == (6, 21, 30)
        old_delivery = await db.fetch_one("SELECT status FROM bot.deliveries WHERE id = %s",
                                          (deliveries[0]["id"],))
        assert old_delivery["status"] == "superseded"
        await important.review_settings(db, user, enabled=False)
        assert await important.ensure_weekly_review(db, user, due+timedelta(days=7)) == 0
        assert not (await important.review_settings(db, user))["enabled"]
        # 微信工具入口按关键词定位已完成记录，不需要模型 API。
        _, replies, ids = await client._run_tool(db, user, "edit_important_work",
            {"match_text": "本周完成评审", "note": "补充验收结果"}, inbox_id=None, pending=None)
        assert ids == [done.id] and "已保存" in replies[0]
        print("重要工作 IvorySQL 集成通过：归档、历史范围、早报映射、并发去重、改期撤销、关闭、工具入口。")
    finally:
        await client.close()
        await db.execute("DELETE FROM bot.deliveries WHERE user_id = %s", (user,))
        await db.execute("DELETE FROM bot.digests WHERE user_id = %s", (user,))
        await db.execute("DELETE FROM bot.work_review_runs WHERE user_id = %s", (user,))
        await db.execute("DELETE FROM bot.work_review_settings WHERE user_id = %s", (user,))
        await db.execute("DELETE FROM bot.task_changes WHERE task_id IN "
                         "(SELECT id FROM bot.tasks WHERE user_id = %s)", (user,))
        await db.execute("DELETE FROM bot.tasks WHERE user_id = %s", (user,))
        await db.close()


if __name__ == "__main__":
    asyncio.run(scenario())
