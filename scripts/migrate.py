#!/usr/bin/env python3
"""迁移执行器：按文件名顺序应用 migrations/*.sql，已应用的记在 bot.schema_migrations。

用法：migrate.py [--config /opt/reminder-bot/etc/config.ini] [--dry-run]
"""

from __future__ import annotations

import os

import argparse
import configparser
import hashlib
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
DEFAULT_MIGRATIONS = HERE.parent / "migrations"
DEFAULT_CONFIG = Path(os.environ.get("REMINDER_CONFIG", Path(__file__).resolve().parent.parent / "etc/config.ini"))

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS bot;
CREATE TABLE IF NOT EXISTS bot.schema_migrations (
    filename   text        PRIMARY KEY,
    sha256     text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def connect(config_path: Path) -> psycopg.Connection:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(config_path):
        raise SystemExit(f"读不到配置文件：{config_path}")
    db = parser["db"]
    password = Path(db["password_file"]).read_text().strip()
    return psycopg.connect(
        host=db["host"],
        port=int(db["port"]),
        dbname=db["dbname"],
        user=db["user"],
        password=password,
        connect_timeout=int(db.get("connect_timeout", 10)),
        autocommit=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--migrations", type=Path, default=DEFAULT_MIGRATIONS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = sorted(args.migrations.glob("*.sql"))
    if not files:
        raise SystemExit(f"没有找到迁移文件：{args.migrations}")

    with connect(args.config) as conn, conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT filename, sha256 FROM bot.schema_migrations")
        applied = dict(cur.fetchall())

        pending = []
        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.name not in applied:
                pending.append((path, digest))
            elif applied[path.name] != digest:
                raise SystemExit(
                    f"迁移文件已应用但内容被改动：{path.name}\n"
                    f"  已记录 {applied[path.name]}\n  当前    {digest}\n"
                    "已应用的迁移不允许修改，请新增一个迁移文件。"
                )

        if not pending:
            print("没有待应用的迁移，数据库已是最新。")
            return 0

        for path, digest in pending:
            print(f"[应用] {path.name} ...", end=" ", flush=True)
            if args.dry_run:
                print("dry-run 跳过")
                continue
            cur.execute(path.read_text())
            cur.execute(
                "INSERT INTO bot.schema_migrations (filename, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
            print("完成")

        if not args.dry_run:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'bot'"
            )
            print(f"bot schema 现有 {cur.fetchone()[0]} 张表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
