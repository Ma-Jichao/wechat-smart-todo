"""微信 iLink 适配器。

设计要点：

  * wechatbot-sdk 只作为**协议层**使用：ILinkApi 的 get_updates / send_message /
    build_text_message，以及 types / errors / crypto。**不使用 WeChatBot 类**，因为它的
    长轮询是「先推进游标、后派发消息」，且回调异常被吞掉、游标与 context 只在内存，
    批次内「游标已推进但消息未落库」的窗口无法闭合。
  * 编排权在本模块手里，收消息顺序严格是：

        读库里的 cursor
          -> get_updates(cursor)
          -> 一个事务：整批 raw 写 inbox（message_id 唯一键去重） + 更新 cursor + 更新 context_token
          -> 提交成功
          -> 才把新落库的消息交给业务处理

    提交前崩溃则游标未动，重启重拉该批，inbox 唯一键保证幂等；提交后崩溃则消息已在库，
    由 inbox 状态机续处理。批次内的漏消息窗口被事务闭合。
  * context_token 与 cursor 的唯一真相源是 bot.bot_state，不是 SDK 的内存属性，
    因此不触碰任何私有属性。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from psycopg.types.json import Jsonb
from wechatbot.errors import ApiError
from wechatbot.protocol import ILinkApi

from .config import WeixinConfig
from .db import Database

log = logging.getLogger(__name__)

# 这些接口签名是本适配层的契约，升级 SDK 后行为变化必须在这里被发现
REQUIRED_API_METHODS = ("get_updates", "send_message", "build_text_message", "notify_start")

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
MESSAGE_TYPE_USER = 1

KIND_BY_ITEM = {ITEM_IMAGE: "image", ITEM_VOICE: "voice", ITEM_FILE: "file", ITEM_VIDEO: "video"}


class AdapterContractError(SystemExit):
    """SDK 协议层接口不符合预期，拒绝启动。"""


def assert_sdk_contract() -> None:
    for name in REQUIRED_API_METHODS:
        if not hasattr(ILinkApi, name):
            raise AdapterContractError(
                f"wechatbot-sdk 缺少所需接口 ILinkApi.{name}，拒绝启动。"
                "本适配层锁定 wechatbot-sdk==0.3.0，升级前请先运行对应测试。"
            )
    signature = ILinkApi.build_text_message.__code__.co_varnames[:3]
    if tuple(signature) != ("user_id", "context_token", "text"):
        raise AdapterContractError(
            f"ILinkApi.build_text_message 参数改变：{signature}，拒绝启动。"
        )


@dataclass
class Incoming:
    user_id: str
    message_id: str
    seq: int | None
    kind: str
    text: str
    voice_text: str | None
    voice_has_media: bool
    context_token: str
    created_ms: int | None
    quoted_text: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def parse_incoming(raw: dict[str, Any]) -> Incoming | None:
    """把接口原始报文解析成结构化消息。只处理用户发来的消息。"""
    if raw.get("message_type") != MESSAGE_TYPE_USER:
        return None

    items = raw.get("item_list") or []
    texts: list[str] = []
    voice_texts: list[str] = []
    voice_has_media = False
    kinds: list[str] = []
    quoted: str | None = None

    for item in items:
        itype = item.get("type")
        if itype == ITEM_TEXT:
            value = (item.get("text_item") or {}).get("text")
            if value:
                texts.append(value)
            kinds.append("text")
        elif itype == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            if voice.get("media"):
                voice_has_media = True
            if voice.get("text"):
                voice_texts.append(voice["text"])
            kinds.append("voice")
        elif itype in KIND_BY_ITEM:
            kinds.append(KIND_BY_ITEM[itype])
        ref = item.get("ref_msg")
        if ref:
            quoted = (
                (ref.get("message_item") or {}).get("text_item", {}).get("text")
                or ref.get("title")
            )

    if "voice" in kinds and len(kinds) == 1:
        kind = "voice"
    elif "text" in kinds:
        kind = "text"
    elif kinds:
        kind = kinds[0]
    else:
        kind = "unknown"

    message_id = raw.get("message_id")
    return Incoming(
        user_id=str(raw.get("from_user_id") or ""),
        message_id="" if message_id is None else str(message_id),
        seq=raw.get("seq"),
        kind=kind,
        text="".join(texts).strip() or "".join(voice_texts).strip(),
        voice_text="".join(voice_texts).strip() or None,
        voice_has_media=voice_has_media,
        context_token=str(raw.get("context_token") or ""),
        created_ms=raw.get("create_time_ms"),
        quoted_text=quoted,
        raw=raw,
    )


@dataclass
class PollResult:
    inbox_ids: list[int] = field(default_factory=list)
    duplicates: int = 0
    context_updated: bool = False
    cursor_advanced: bool = False
    received: int = 0


async def ingest_batch(
    db: Database,
    cfg: WeixinConfig,
    raw_msgs: Sequence[dict[str, Any]],
    cursor: str,
    new_cursor: str | None,
) -> PollResult:
    """把一批原始消息与新的接收游标在**同一个事务**内落库。

    这是整套系统不丢消息的关键点：事务提交之前游标不动，提交之后消息必然已在库。
    拆成独立函数是为了让崩溃窗口可以被测试直接验证（见 tests/test_ingest_atomicity.py）。
    """
    result = PollResult(received=len(raw_msgs))

    latest_context: str | None = None
    for raw in raw_msgs:
        parsed = parse_incoming(raw)
        if parsed and parsed.user_id == cfg.allowed_user_id and parsed.context_token:
            latest_context = parsed.context_token

    async with db.transaction() as cur:
        for raw in raw_msgs:
            message_id = raw.get("message_id")
            if message_id is None:
                log.warning("消息缺少 message_id，跳过落库：seq=%r", raw.get("seq"))
                continue
            await cur.execute(
                """
                INSERT INTO bot.inbox (account_id, message_id, seq, from_user_id, raw)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_id, message_id) DO NOTHING
                RETURNING id
                """,
                (
                    cfg.account_id,
                    str(message_id),
                    raw.get("seq"),
                    raw.get("from_user_id"),
                    Jsonb(raw),
                ),
            )
            row = await cur.fetchone()
            if row is None:
                result.duplicates += 1
            else:
                result.inbox_ids.append(row["id"])

        assignments = ["updated_at = now()"]
        params: list[Any] = []
        if new_cursor and new_cursor != cursor:
            assignments.append("cursor = %s")
            params.append(new_cursor)
            result.cursor_advanced = True
        if latest_context:
            assignments.append("context_token = %s")
            assignments.append("context_updated_at = now()")
            params.append(latest_context)
            result.context_updated = True
        if len(assignments) == 1 and not result.duplicates:
            # 既没有新消息也没有新游标，不必写库
            return result
        params.append(cfg.account_id)
        await cur.execute(
            f"UPDATE bot.bot_state SET {', '.join(assignments)} WHERE account_id = %s",
            tuple(params),
        )

    return result


class SessionExpired(Exception):
    """iLink 会话失效（errcode -14），需要重新扫码，不能无人值守恢复。"""


class Sender:
    """发送接口的薄封装。context_token 显式入参，不依赖任何 SDK 内部状态。"""

    def __init__(self, cfg: WeixinConfig) -> None:
        self._cfg = cfg
        self._api = ILinkApi()

    async def send_text(self, user_id: str, context_token: str, text: str) -> dict[str, Any]:
        message = ILinkApi.build_text_message(user_id, context_token, text)
        return await self._api.send_message(self._cfg.base_url, self._cfg.token, message)

    async def announce_start(self) -> dict[str, Any]:
        return await self._api.notify_start(self._cfg.base_url, self._cfg.token)

    async def announce_stop(self) -> dict[str, Any]:
        return await self._api.notify_stop(self._cfg.base_url, self._cfg.token)


class WeixinAdapter:
    def __init__(self, cfg: WeixinConfig, db: Database) -> None:
        self._cfg = cfg
        self._db = db
        self._api = ILinkApi()
        self._stopped = False
        self.session_expired = False

    async def ensure_state_row(self) -> None:
        await self._db.execute(
            """
            INSERT INTO bot.bot_state (account_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (account_id) DO NOTHING
            """,
            (self._cfg.account_id, self._cfg.allowed_user_id),
        )

    async def load_state(self) -> dict[str, Any]:
        row = await self._db.fetch_one(
            "SELECT cursor, context_token FROM bot.bot_state WHERE account_id = %s",
            (self._cfg.account_id,),
        )
        return row or {"cursor": "", "context_token": None}

    async def poll_once(self) -> PollResult:
        """取一批消息并原子落库。游标推进与消息落库在同一事务内。"""
        state = await self.load_state()
        cursor = state["cursor"] or ""

        try:
            response = await self._api.get_updates(self._cfg.base_url, self._cfg.token, cursor)
        except ApiError as exc:
            if exc.errcode == -14:
                self.session_expired = True
                raise SessionExpired(str(exc)) from exc
            raise

        raw_msgs: Sequence[dict[str, Any]] = response.get("msgs") or []
        new_cursor = response.get("get_updates_buf")

        rejected = sum(
            1 for m in (parse_incoming(r) for r in raw_msgs)
            if m is not None and m.user_id != self._cfg.allowed_user_id
        )
        if rejected:
            log.warning("本批有 %d 条非授权用户消息，仅记录不处理", rejected)

        result = await ingest_batch(self._db, self._cfg, raw_msgs, cursor, new_cursor)
        if result.inbox_ids:
            log.info(
                "本批落库 %d 条（重复 %d 条），游标%s，context%s",
                len(result.inbox_ids),
                result.duplicates,
                "已推进" if result.cursor_advanced else "未变",
                "已刷新" if result.context_updated else "未变",
            )
        return result

    async def run(self, on_batch: Callable[[PollResult], Awaitable[None]]) -> None:
        """长轮询主循环。on_batch 只在事务提交成功之后被调用。"""
        await self.ensure_state_row()
        backoff = self._cfg.reconnect_backoff_base_secs
        try:
            await self._api.notify_start(self._cfg.base_url, self._cfg.token)
            log.info("已发送 notifystart 上线通告")
        except ApiError as exc:
            log.warning("notifystart 失败（不致命）：errcode=%s %s", exc.errcode, exc)

        while not self._stopped:
            try:
                result = await self.poll_once()
                backoff = self._cfg.reconnect_backoff_base_secs
                if result.inbox_ids:
                    await on_batch(result)
            except SessionExpired:
                log.error("iLink 会话失效（-14），需要重新扫码；已停止长轮询")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("长轮询本轮失败，%ss 后重连", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._cfg.reconnect_backoff_cap_secs)

        try:
            await self._api.notify_stop(self._cfg.base_url, self._cfg.token)
        except Exception:
            log.warning("notifystop 失败（忽略）", exc_info=True)

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# 错误分类：决定投递记录进入哪个状态
# ---------------------------------------------------------------------------
AUTH_ERRORS = {-14}
PERMANENT_MESSAGE = ("invalid", "bad request", "forbidden", "not allowed", "illegal")


def classify_error(exc: BaseException) -> tuple[str, str]:
    """返回 (error_class, human_reason)。

    error_class 取值：
      auth       凭据/会话失效，暂停不可能成功的投递，保留队列
      rate_limit 限流，账号维度共享冷却
      context    缺少可用的 context_token，需等用户先发消息
      transient  网络或服务端暂时故障，指数退避重试
      permanent  请求格式/权限等永久错误，停止无意义重试
    """
    if isinstance(exc, ApiError):
        detail = f"{exc}"
        lowered = detail.lower()
        if exc.errcode in AUTH_ERRORS:
            return "auth", detail
        if exc.http_status == 429 or "rate limited" in lowered or "rate limit" in lowered:
            return "rate_limit", detail
        if "prepare failed" in lowered or "context" in lowered:
            return "context", detail
        if any(token in lowered for token in PERMANENT_MESSAGE):
            return "permanent", detail
        return "transient", detail
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "transient", "请求超时，结果不确定"
    return "transient", f"{type(exc).__name__}: {exc}"
