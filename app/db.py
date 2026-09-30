"""数据库访问层。

用 psycopg3 的异步接口 + 官方连接池，保证数据库调用不阻塞事件循环。
所有业务写入都通过 transaction() 包裹，由数据库承担一致性。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import DbConfig

log = logging.getLogger(__name__)


class Database:
    def __init__(self, cfg: DbConfig, min_size: int = 1, max_size: int = 4) -> None:
        self._cfg = cfg
        self._pool = AsyncConnectionPool(
            conninfo=psycopg.conninfo.make_conninfo(**cfg.kwargs()),
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            # 小内存机器上不做额外预热连接，按需建立
            open=False,
        )
        self._opened = False

    async def open(self) -> None:
        if not self._opened:
            await self._pool.open(wait=True, timeout=self._cfg.connect_timeout)
            self._opened = True

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._pool.connection(timeout=self._cfg.connect_timeout) as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncCursor]:
        """一个显式事务。异常时回滚，正常退出时提交。"""
        async with self._pool.connection(timeout=self._cfg.connect_timeout) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    yield cur

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchone()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return list(await cur.fetchall())

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount

    async def healthy(self) -> bool:
        try:
            row = await self.fetch_one("SELECT 1 AS ok")
            return bool(row and row["ok"] == 1)
        except Exception:
            log.warning("数据库健康检查失败", exc_info=True)
            return False
