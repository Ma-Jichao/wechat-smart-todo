"""配置加载。

配置文件用 INI（stdlib configparser，不引入额外依赖）。
口令与 API key 只以文件路径形式出现，读取后立即注入连接参数/请求头，不写日志。
"""

from __future__ import annotations

import os

import configparser
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("REMINDER_CONFIG", Path(__file__).resolve().parent.parent / "etc/config.ini"))


class ConfigError(SystemExit):
    pass


def _read_secret(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"凭据文件不存在：{path}")
    mode = file_path.stat().st_mode & 0o777
    # 要求：任何 other 权限都不允许，且组不可写。
    # 允许 600（服务用户独占）与 640（root 拥有、服务组只读，服务用户改不了自己的凭据）。
    if mode & 0o007:
        raise ConfigError(f"凭据文件对 others 开放（{oct(mode)}），应至少为 640：{path}")
    if mode & 0o020:
        raise ConfigError(f"凭据文件组可写（{oct(mode)}），服务用户不应能改写自己的凭据：{path}")
    value = file_path.read_text().strip()
    if not value:
        raise ConfigError(f"凭据文件为空：{path}")
    return value


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    connect_timeout: int

    def kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
        }


@dataclass(frozen=True)
class WeixinConfig:
    base_url: str
    account_id: str
    allowed_user_id: str
    token: str
    long_poll_timeout_secs: int
    reconnect_backoff_base_secs: float
    reconnect_backoff_cap_secs: float


@dataclass(frozen=True)
class DeepSeekConfig:
    base_url: str
    model: str
    api_key: str
    timeout_secs: int


@dataclass(frozen=True)
class DeliveryConfig:
    min_interval_secs: float
    max_attempts: int
    backoff_base_secs: float
    backoff_cap_secs: float
    backoff_jitter_ratio: float
    lease_secs: int
    rate_limit_cooldown_secs: int
    send_timeout_secs: int


@dataclass(frozen=True)
class DigestConfig:
    hour: int
    minute: int
    timezone: str
    due_scan_interval_secs: float
    digest_grace_hours: int


@dataclass(frozen=True)
class StyleConfig:
    """早报「温度」层的开关。关掉即纯确定性版本。"""

    model: str = "deepseek-flash"
    max_tokens: int = 2000
    greeting: bool = True
    per_item_note: bool = True
    closing: bool = True


@dataclass(frozen=True)
class LimitsConfig:
    max_text_chars: int
    context_ttl_hours: int
    clarify_ttl_minutes: int
    seen_retention_days: int
    inbox_retention_days: int


@dataclass(frozen=True)
class Config:
    db: DbConfig
    weixin: WeixinConfig
    deepseek: DeepSeekConfig
    delivery: DeliveryConfig
    digest: DigestConfig
    style: StyleConfig
    limits: LimitsConfig
    log_level: str
    log_file: str
    log_max_bytes: int
    log_backup_count: int
    path: Path


def load(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise ConfigError(f"读不到配置文件：{path}")

    def get(section: str, key: str, fallback: str | None = None) -> str:
        if parser.has_option(section, key):
            return parser.get(section, key).strip()
        if fallback is not None:
            return fallback
        raise ConfigError(f"配置缺少 [{section}] {key}")

    def get_int(section: str, key: str, fallback: int | None = None) -> int:
        raw = get(section, key, None if fallback is None else str(fallback))
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"配置 [{section}] {key} 不是整数：{raw!r}") from exc

    def get_float(section: str, key: str, fallback: float | None = None) -> float:
        raw = get(section, key, None if fallback is None else str(fallback))
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"配置 [{section}] {key} 不是数字：{raw!r}") from exc

    return Config(
        db=DbConfig(
            host=get("db", "host"),
            port=get_int("db", "port"),
            dbname=get("db", "dbname"),
            user=get("db", "user"),
            password=_read_secret(get("db", "password_file")),
            connect_timeout=get_int("db", "connect_timeout", 10),
        ),
        weixin=WeixinConfig(
            base_url=get("weixin", "base_url"),
            account_id=get("weixin", "account_id"),
            allowed_user_id=get("weixin", "allowed_user_id"),
            token=_read_secret(get("weixin", "token_file")),
            long_poll_timeout_secs=get_int("weixin", "long_poll_timeout_secs", 45),
            reconnect_backoff_base_secs=get_float("weixin", "reconnect_backoff_base_secs", 1),
            reconnect_backoff_cap_secs=get_float("weixin", "reconnect_backoff_cap_secs", 30),
        ),
        deepseek=DeepSeekConfig(
            base_url=get("deepseek", "base_url"),
            model=get("deepseek", "model"),
            api_key=_read_secret(get("deepseek", "api_key_file")),
            timeout_secs=get_int("deepseek", "timeout_secs", 30),
        ),
        delivery=DeliveryConfig(
            min_interval_secs=get_float("delivery", "min_interval_secs", 3),
            max_attempts=get_int("delivery", "max_attempts", 8),
            backoff_base_secs=get_float("delivery", "backoff_base_secs", 5),
            backoff_cap_secs=get_float("delivery", "backoff_cap_secs", 900),
            backoff_jitter_ratio=get_float("delivery", "backoff_jitter_ratio", 0.2),
            lease_secs=get_int("delivery", "lease_secs", 120),
            rate_limit_cooldown_secs=get_int("delivery", "rate_limit_cooldown_secs", 300),
            send_timeout_secs=get_int("delivery", "send_timeout_secs", 20),
        ),
        digest=DigestConfig(
            hour=get_int("digest", "hour", 8),
            minute=get_int("digest", "minute", 0),
            timezone=get("digest", "timezone", "Asia/Shanghai"),
            due_scan_interval_secs=get_float("digest", "due_scan_interval_secs", 15),
            digest_grace_hours=get_int("digest", "digest_grace_hours", 6),
        ),
        style=StyleConfig(
            model=get("style", "model", "deepseek-flash"),
            max_tokens=get_int("style", "max_tokens", 2000),
            greeting=parser.getboolean("style", "greeting", fallback=True),
            per_item_note=parser.getboolean("style", "per_item_note", fallback=True),
            closing=parser.getboolean("style", "closing", fallback=True),
        ),
        limits=LimitsConfig(
            max_text_chars=get_int("limits", "max_text_chars", 2000),
            context_ttl_hours=get_int("limits", "context_ttl_hours", 72),
            clarify_ttl_minutes=get_int("limits", "clarify_ttl_minutes", 30),
            seen_retention_days=get_int("limits", "seen_retention_days", 30),
            inbox_retention_days=get_int("limits", "inbox_retention_days", 90),
        ),
        log_level=get("logging", "level", "INFO"),
        log_file=get("logging", "file"),
        log_max_bytes=get_int("logging", "max_bytes", 10 * 1024 * 1024),
        log_backup_count=get_int("logging", "backup_count", 5),
        path=path,
    )
