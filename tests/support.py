"""Require explicit isolated configuration before reading any test credentials."""
import configparser
import os
from pathlib import Path
import re
from urllib.parse import urlparse
from app.config import load


def load_test_config(*,live_llm=False):
    value=os.environ.get('REMINDER_TEST_CONFIG')
    if not value:raise SystemExit('数据库测试需要 REMINDER_TEST_CONFIG，禁止默认使用应用配置。')
    path=Path(value)
    raw=configparser.ConfigParser(interpolation=None)
    if not raw.read(path):raise SystemExit('读不到测试配置。')
    dbname=raw.get('db','dbname',fallback='')
    if not re.search(r'(?:^|_)test(?:_|$)',dbname):
        raise SystemExit('测试库名必须包含独立的 test 标记，例如 reminder_test；拒绝读取凭据或连接。')
    for section in ('weixin',) if live_llm else ('weixin','deepseek'):
        if not (urlparse(raw.get(section,'base_url',fallback='')).hostname or '').endswith('.invalid'):
            raise SystemExit('离线/集成测试的外部服务地址必须为 .invalid，禁止真实微信请求。')
    return load(path)
