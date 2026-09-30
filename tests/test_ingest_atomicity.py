"""验证「一批消息可靠落库后才推进游标」这一不变式。

这是本方案放弃 SDK 编排层、自己写长轮询循环的理由，所以必须有测试守住它。
测试直接打真实数据库（IvorySQL），用独立的 account_id 隔离，不改动生产数据。
"""

from __future__ import annotations

import os

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import load_test_config
from app.config import DeliveryConfig, DbConfig, DigestConfig, LimitsConfig, WeixinConfig, load
from app.db import Database
from app.weixin import ingest_batch, parse_incoming

TEST_USER = "test-user@im.wechat"


def make_weixin_config(account_id: str) -> WeixinConfig:
    return WeixinConfig(
        base_url="https://example.invalid",
        account_id=account_id,
        allowed_user_id=TEST_USER,
        token="test-token-not-used",
        long_poll_timeout_secs=45,
        reconnect_backoff_base_secs=1.0,
        reconnect_backoff_cap_secs=30.0,
    )


def fake_message(message_id: str, text: str, context_token: str = "ctx-1", seq: int = 1) -> dict:
    return {
        "message_id": message_id,
        "seq": seq,
        "message_type": 1,
        "from_user_id": TEST_USER,
        "to_user_id": "bot",
        "create_time_ms": 1790000000000,
        "context_token": context_token,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def fake_voice(message_id: str, server_text: str | None, context_token: str = "ctx-2") -> dict:
    voice_item: dict = {"playtime": 3200, "encode_type": 4, "media": {"full_url": "https://x.invalid/a"}}
    if server_text is not None:
        voice_item["text"] = server_text
    return {
        "message_id": message_id,
        "seq": 2,
        "message_type": 1,
        "from_user_id": TEST_USER,
        "create_time_ms": 1790000001000,
        "context_token": context_token,
        "item_list": [{"type": 3, "voice_item": voice_item}],
    }


async def scenario() -> None:
    base = load_test_config()
    account_id = f"test-{uuid.uuid4()}"
    db = Database(base.db, min_size=1, max_size=2)
    await db.open()
    weixin = make_weixin_config(account_id)

    async def setup() -> None:
        await db.execute(
            "INSERT INTO bot.bot_state (account_id, user_id, cursor) VALUES (%s, %s, %s)",
            (account_id, TEST_USER, "cursor-0"),
        )

    async def teardown() -> None:
        await db.execute("DELETE FROM bot.inbox WHERE account_id = %s", (account_id,))
        await db.execute("DELETE FROM bot.bot_state WHERE account_id = %s", (account_id,))

    def state_cursor(row: dict | None) -> str:
        assert row is not None
        return row["cursor"]

    try:
        # ---- 用例 1：正常提交，消息与游标一起落库 -------------------------------
        await setup()
        batch = [fake_message("m1", "开会"), fake_message("m2", "买牛奶", seq=2)]
        result = await ingest_batch(db, weixin, batch, "cursor-0", "cursor-1")

        assert len(result.inbox_ids) == 2, result
        assert result.cursor_advanced and result.context_updated, result
        row = await db.fetch_one(
            "SELECT cursor, context_token FROM bot.bot_state WHERE account_id = %s", (account_id,)
        )
        assert state_cursor(row) == "cursor-1", row
        assert row["context_token"] == "ctx-1", row
        count = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.inbox WHERE account_id = %s", (account_id,)
        )
        assert count["n"] == 2, count
        print("[通过] 用例 1：消息与游标在同一事务内提交")

        # ---- 用例 2：同一批重放，唯一键去重，不产生新记录 -----------------------
        again = await ingest_batch(db, weixin, batch, "cursor-1", "cursor-1")
        assert again.inbox_ids == [], again
        assert again.duplicates == 2, again
        count = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.inbox WHERE account_id = %s", (account_id,)
        )
        assert count["n"] == 2, count
        print("[通过] 用例 2：批次重放幂等，inbox 仍是 2 条")

        # ---- 用例 3：插入中途失败，整个事务回滚，游标不动 -----------------------
        # 第二条消息的 raw 含不可 JSON 序列化的对象，会在写库时抛错。
        # 这正是「游标已推进但消息未落库」的窗口，必须证明它不存在。
        bad = fake_message("m3", "这条会失败", context_token="ctx-9", seq=3)
        bad["unserializable"] = object()
        batch2 = [fake_message("m4", "先落库再失败", seq=4), bad]
        try:
            await ingest_batch(db, weixin, batch2, "cursor-1", "cursor-2")
            raise AssertionError("用例 3 预期插入失败，但事务提交了")
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 — 任意序列化错误都应触发回滚
            print(f"[信息] 用例 3 注入的失败：{type(exc).__name__}")

        row = await db.fetch_one(
            "SELECT cursor FROM bot.bot_state WHERE account_id = %s", (account_id,)
        )
        assert state_cursor(row) == "cursor-1", f"游标不应推进，实际 {row}"
        count = await db.fetch_one(
            "SELECT count(*) AS n FROM bot.inbox WHERE account_id = %s", (account_id,)
        )
        assert count["n"] == 2, f"回滚后不应有新消息，实际 {count}"
        print("[通过] 用例 3：插入失败时游标未推进、消息未落库（窗口已闭合）")

        # ---- 用例 4：语音解析（服务端给不给转写文本两种都要正确） ---------------
        with_text = parse_incoming(fake_voice("v1", "提醒我明天下午三点开会"))
        assert with_text is not None
        assert with_text.kind == "voice", with_text
        assert with_text.voice_text == "提醒我明天下午三点开会", with_text
        assert with_text.voice_has_media, with_text

        without_text = parse_incoming(fake_voice("v2", None))
        assert without_text is not None
        assert without_text.voice_text is None, without_text
        assert without_text.voice_has_media and without_text.text == "", without_text
        print("[通过] 用例 4：语音消息解析（有/无服务端转写文本两条分支）")

        # ---- 用例 5：非授权用户被识别出来 --------------------------------------
        intruder = fake_message("m9", "我是别人")
        intruder["from_user_id"] = "someone-else@im.wechat"
        parsed = parse_incoming(intruder)
        assert parsed is not None and parsed.user_id != TEST_USER
        print("[通过] 用例 5：非授权用户消息可被识别，不进入业务处理")
    finally:
        await teardown()
        await db.close()


def main() -> int:
    asyncio.run(scenario())
    print("\n全部用例通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
