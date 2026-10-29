"""进程入口。

单个 Python 进程里跑四个协程：

  接收（长轮询）→ 处理（DeepSeek + 业务工具）→ 投递（统一限速发送）→ 调度（到点兜底 + 08:00 早报）

守护规则：任何一个关键协程退出，整个进程就进入故障恢复并退出，
交给 systemd 重启，绝不让其余协程继续跑而形成「假健康」。

启动前先抢单实例租约，防止误启动第二个消费者。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import socket
import sys
from pathlib import Path
from typing import Sequence

from psycopg.types.json import Jsonb

from . import flavor, format, outbox, tools
from .config import DEFAULT_CONFIG_PATH, Config, load
from .db import Database
from .llm import LlmClient, LlmError
from .scheduler import Scheduler
from .sender import DeliverySender, pending_summary
from .weixin import Incoming, SessionExpired, WeixinAdapter, assert_sdk_contract, parse_incoming

log = logging.getLogger("reminder")

LEASE_NAME = "reminder-bot"
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30


def setup_logging(cfg: Config) -> None:
    Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(stream)
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))


# ---------------------------------------------------------------------------
# 单实例租约
# ---------------------------------------------------------------------------
async def acquire_lease(db: Database, owner: str, wait_seconds: float = LEASE_SECONDS + 30) -> bool:
    """抢占单实例租约。

    上一次进程崩溃时租约不会被主动释放，所以这里**等待**它过期而不是立刻拒绝启动：
    否则 systemd 的重启会被自己的租约挡回去，反复失败后触发 StartLimit 彻底放弃。
    """
    deadline = asyncio.get_running_loop().time() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        row = await db.fetch_one(
            """
            INSERT INTO bot.runtime_lease (lease_name, owner, heartbeat_at, expires_at)
            VALUES (%s, %s, now(), now() + make_interval(secs => %s))
            ON CONFLICT (lease_name) DO UPDATE
              SET owner = EXCLUDED.owner, heartbeat_at = now(), expires_at = EXCLUDED.expires_at
              WHERE bot.runtime_lease.owner = EXCLUDED.owner
                 OR bot.runtime_lease.expires_at < now()
            RETURNING owner
            """,
            (LEASE_NAME, owner, LEASE_SECONDS),
        )
        if row and row["owner"] == owner:
            if attempt > 1:
                log.info("等待 %d 次后取得租约", attempt)
            return True
        if asyncio.get_running_loop().time() >= deadline:
            holder = await db.fetch_one(
                "SELECT owner, expires_at FROM bot.runtime_lease WHERE lease_name = %s",
                (LEASE_NAME,),
            )
            log.error("租约仍被占用，放弃启动：%s", holder)
            return False
        log.warning("租约仍被占用，等待其过期后重试启动（单消费者保护）")
        await asyncio.sleep(5)


async def release_lease(db: Database, owner: str) -> None:
    """正常停机时释放租约，让重启可以立刻拿到。"""
    try:
        await db.execute(
            "DELETE FROM bot.runtime_lease WHERE lease_name = %s AND owner = %s",
            (LEASE_NAME, owner),
        )
    except Exception:
        log.warning("释放租约失败（不影响停机）", exc_info=True)


async def heartbeat(db: Database, owner: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await db.execute(
                """
                UPDATE bot.runtime_lease
                SET heartbeat_at = now(), expires_at = now() + make_interval(secs => %s)
                WHERE lease_name = %s AND owner = %s
                """,
                (LEASE_SECONDS, LEASE_NAME, owner),
            )
        except Exception:
            log.warning("租约心跳失败", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# 处理协程
# ---------------------------------------------------------------------------
RECOVER_INBOX_SQL = """
UPDATE bot.inbox
SET status = 'retry', lease_until = NULL, updated_at = now()
WHERE status = 'processing' AND (lease_until IS NULL OR lease_until < now())
"""

CLAIM_NEXT_INBOX_SQL = """
UPDATE bot.inbox
SET status = 'processing',
    lease_until = now() + make_interval(secs => 120),
    updated_at = now()
WHERE id = (
    SELECT id FROM bot.inbox
    WHERE status IN ('pending', 'retry')
      AND (lease_until IS NULL OR lease_until < now())
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *
"""


class Processor:
    """处理协程。

    **待处理消息的唯一来源是数据库**，内存里只放一个唤醒信号。
    只靠内存队列的话，「消息已落库但还没处理」的 inbox 记录在崩溃后就再也没人管了
    （需求文档 4.2 第 8 条：进程崩溃后可恢复处理中消息）。
    """

    def __init__(self, cfg: Config, db: Database, llm: LlmClient, sender: DeliverySender) -> None:
        self._cfg = cfg
        self._db = db
        self._llm = llm
        self._sender = sender
        self._wakeup = asyncio.Event()
        self._stopped = False

    def submit(self, inbox_ids: list[int]) -> None:
        """收消息侧只负责叫醒，具体处理哪条由数据库决定。"""
        if inbox_ids:
            self._wakeup.set()

    async def _reply(
        self, user_id: str, inbox_id: int | None, text: str, task_ids: Sequence[int] = (),
    ) -> None:
        if inbox_id is not None:
            key = outbox.reply_business_key(inbox_id)
        else:
            key = f"reply:adhoc:{outbox.short_hash(text)}"
        now = tools.now_shanghai()
        # 入队回复、记录机器人输出、标记 inbox 完成，三件事同一事务提交，
        # 避免出现「回复已入队但 inbox 还是处理中」或反过来的一半完成状态。
        async with self._db.transaction() as cur:
            await outbox.enqueue_text(
                cur, user_id=user_id, kind="reply", business_key=key,
                text=text, scheduled_at=now,
            )
            await cur.execute(
                "INSERT INTO bot.messages_seen (user_id, direction, body, task_ids) "
                "VALUES (%s, 'out', %s, %s)",
                (user_id, text, list(task_ids)),
            )
            if inbox_id is not None:
                await cur.execute(
                    "UPDATE bot.inbox SET status = 'done', processed_at = now(), "
                    "lease_until = NULL, updated_at = now() WHERE id = %s",
                    (inbox_id,),
                )

    async def _claim_next(self) -> dict | None:
        return await self._db.fetch_one(CLAIM_NEXT_INBOX_SQL)

    async def _handle_one(self, row: dict) -> None:
        inbox_id = row["id"]
        raw = row["raw"]
        message = parse_incoming(raw)
        user_id = self._cfg.weixin.allowed_user_id

        if message is None or message.user_id != user_id:
            log.info("inbox #%s 不是白名单用户的消息，标记完成不处理", inbox_id)
            await self._db.execute(
                "UPDATE bot.inbox SET status = 'done', processed_at = now(), "
                "lease_until = NULL, updated_at = now() WHERE id = %s", (inbox_id,))
            return

        text = message.text or ""
        source = "voice" if message.kind == "voice" else "text"
        if not text:
            log.info("inbox #%s 无可识别文本（kind=%s），已告知用户", inbox_id, message.kind)
            await self._reply(
                user_id, inbox_id,
                format.voice_unreadable() if message.voice_has_media else format.no_text_content(),
            )
            return

        await tools.record_seen_message(self._db, user_id, "in", text)
        try:
            result = await self._llm.handle(self._db, user_id, text,
                                            inbox_id=inbox_id, source=source)
        except LlmError as exc:
            # 明确告知尚未保存，不让后台自动重试，避免与用户重发造成双重创建
            log.error("inbox #%s 模型调用失败：%s", inbox_id, exc)
            await self._db.execute(
                "UPDATE bot.inbox SET status = 'manual', last_error = %s, lease_until = NULL, "
                "updated_at = now() WHERE id = %s", (str(exc)[:500], inbox_id))
            await self._reply(user_id, None, format.llm_failed())
            return

        await self._reply(user_id, inbox_id, result.reply, result.task_ids)
        log.info("inbox #%s 处理完成，涉及事项 %s", inbox_id, result.task_ids)

    async def run(self) -> None:
        await self._db.execute(RECOVER_INBOX_SQL)
        while not self._stopped:
            try:
                row = await self._claim_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("领取待处理消息失败，2s 后重试")
                await asyncio.sleep(2.0)
                continue

            if row is None:
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                self._wakeup.clear()
                continue

            try:
                await self._handle_one(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("inbox #%s 处理异常", row["id"])
                await self._db.execute(
                    "UPDATE bot.inbox SET status = 'retry', attempts = attempts + 1, "
                    "last_error = '处理协程异常', lease_until = NULL, updated_at = now() "
                    "WHERE id = %s",
                    (row["id"],),
                )

    def stop(self) -> None:
        self._stopped = True
        self._wakeup.set()


# ---------------------------------------------------------------------------
# 组装与守护
# ---------------------------------------------------------------------------
async def run_all(cfg: Config) -> int:
    owner = f"{socket.gethostname()}:{os.getpid()}"
    db = Database(
        cfg.db,
        min_size=1,
        # 1.6G 内存的机器上限制连接数，shared_buffers 已按此调过
        max_size=6,
    )
    await db.open()

    if not await acquire_lease(db, owner):
        log.error("已有另一个实例持有租约，拒绝启动（单消费者保护）")
        await db.close()
        return 3

    adapter = WeixinAdapter(cfg.weixin, db)
    sender = DeliverySender(db, cfg.weixin, cfg.delivery, cfg.limits)
    llm = LlmClient(cfg.deepseek, cfg.limits.max_text_chars, cfg.limits.clarify_ttl_minutes,
                    digest_hour=cfg.digest.hour, digest_minute=cfg.digest.minute)
    style_enabled = cfg.style.greeting or cfg.style.per_item_note or cfg.style.closing
    composer = flavor.FlavorComposer(
        llm, enabled=style_enabled, model=cfg.style.model, max_tokens=cfg.style.max_tokens
    )
    scheduler = Scheduler(db, cfg.weixin, cfg.digest, cfg.limits, cfg.style, composer)
    processor = Processor(cfg, db, llm, sender)

    stop_event = asyncio.Event()
    failures: list[str] = []
    # 正常停机时各协程会**正常返回**（stop() 让循环退出），那不是故障。
    # 没有这个标记的话，每次 systemctl restart 都会被记成「协程意外退出」并返回退出码 2，
    # systemd 会把干净停机标记为 failed，制造假故障。
    lifecycle = {"shutting_down": False}

    async def guarded(name: str, coro) -> None:
        try:
            await coro
            if not lifecycle["shutting_down"]:
                failures.append(f"{name} 意外正常退出")
        except asyncio.CancelledError:
            raise
        except SessionExpired as exc:
            failures.append(f"{name} 因会话失效退出：{exc}")
        except Exception as exc:
            log.exception("%s 协程异常退出", name)
            failures.append(f"{name} 异常退出：{type(exc).__name__}: {exc}")

    async def on_batch(result) -> None:
        # 落库提交之后才做这两件事：入队处理、解封因缺 context 而暂停的投递
        if result.context_updated:
            unblocked = await sender.requeue_blocked_context()
            if unblocked:
                log.info("收到入站消息，解封 %d 条此前因通道未就绪而暂停的投递", unblocked)
        processor.submit(result.inbox_ids)

    async def receiver() -> None:
        await adapter.run(on_batch)

    heartbeat_task = asyncio.create_task(heartbeat(db, owner, stop_event))
    workers = [
        asyncio.create_task(guarded("接收协程", receiver()), name="接收协程"),
        asyncio.create_task(guarded("处理协程", processor.run()), name="处理协程"),
        asyncio.create_task(guarded("发送协程", sender.run()), name="发送协程"),
        asyncio.create_task(guarded("调度协程", scheduler.run()), name="调度协程"),
    ]

    shutdown = asyncio.Event()

    def begin_shutdown(reason: str) -> None:
        lifecycle["shutting_down"] = True
        log.info("%s，开始优雅停止", reason)
        shutdown.set()

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        import signal as signal_module

        try:
            loop.add_signal_handler(
                getattr(signal_module, signal_name),
                lambda name=signal_name: begin_shutdown(f"收到 {name}"),
            )
        except (NotImplementedError, ValueError):
            log.warning("无法注册 %s 处理函数", signal_name)

    log.info("启动完成，owner=%s；%s", owner, json.dumps(await pending_summary(db), default=str))

    shutdown_task = asyncio.create_task(shutdown.wait())
    try:
        done, _pending = await asyncio.wait(
            workers + [shutdown_task], return_when=asyncio.FIRST_COMPLETED
        )
        signalled = shutdown_task in done
        for task in done:
            if task is shutdown_task:
                continue
            exc = task.exception()
            if exc is not None:
                failures.append(f"协程异常：{type(exc).__name__}: {exc}")
            elif not signalled:
                # 没有任何停机信号，却有协程自己返回了 —— 这才是真的异常退出
                failures.append(f"{task.get_name()} 未经停机信号就自行结束")
        # 从这里开始是主动收尾，其余协程的正常返回不算故障
        lifecycle["shutting_down"] = True
    finally:
        lifecycle["shutting_down"] = True
        stop_event.set()
        adapter.stop()
        processor.stop()
        sender.stop()
        scheduler.stop()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await release_lease(db, owner)
        await llm.close()
        await db.close()

    if failures:
        log.error("关键协程已退出，进程进入故障恢复：%s", "；".join(failures))
        return 2
    log.warning("所有协程都结束了，正常退出")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="微信提醒系统后端")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--check", action="store_true",
                        help="只做配置与 SDK 契约检查，不连数据库、不发消息")
    parser.add_argument("--status", action="store_true", help="打印投递队列状态后退出")
    args = parser.parse_args()

    assert_sdk_contract()
    cfg = load(args.config)
    setup_logging(cfg)

    if args.check:
        print("配置读取正常；wechatbot-sdk 协议层接口契约检查通过。")
        print(f"  数据库 {cfg.db.user}@{cfg.db.host}:{cfg.db.port}/{cfg.db.dbname}")
        print(f"  模型   {cfg.deepseek.model}  时区 {cfg.digest.timezone}")
        print(f"  早报   {cfg.digest.hour:02d}:{cfg.digest.minute:02d}  "
              f"到点扫描 {cfg.digest.due_scan_interval_secs}s  "
              f"发送间隔 {cfg.delivery.min_interval_secs}s")
        return 0

    if args.status:
        async def show() -> int:
            db = Database(cfg.db, min_size=1, max_size=2)
            await db.open()
            try:
                print(json.dumps(await pending_summary(db), ensure_ascii=False,
                                 indent=2, default=str))
            finally:
                await db.close()
            return 0
        return asyncio.run(show())

    return asyncio.run(run_all(cfg))


if __name__ == "__main__":
    sys.exit(main())
