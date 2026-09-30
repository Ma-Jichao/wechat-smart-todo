# 配置

`config.example.ini` 为占位模板，安装脚本生成使用者自己的 `etc/config.ini`，也可按模板手动配置。运行路径可用 `REMINDER_CONFIG` 覆盖。

| 节 | 参数和用途 |
|---|---|
| db | host/port/dbname/user/password_file/connect_timeout：数据库连接 |
| weixin | base_url/account_id/allowed_user_id/token_file：登录接口、单用户白名单、凭据路径；部署时自动填写 |
| weixin | long_poll_timeout_secs/reconnect_backoff_base_secs/reconnect_backoff_cap_secs：长轮询与退避配置；SDK 实际请求超时以协议实现为准 |
| deepseek | base_url/model/api_key_file/timeout_secs：理解模型与请求超时 |
| style | model/max_tokens/greeting/per_item_note/closing：早报措辞模型、额度和开关 |
| delivery | min_interval_secs/max_attempts/backoff_base_secs/backoff_cap_secs/backoff_jitter_ratio：发送限速与退避 |
| delivery | lease_secs/rate_limit_cooldown_secs/send_timeout_secs：租约、账号冷却和发送超时 |
| digest | hour/minute/timezone/due_scan_interval_secs/digest_grace_hours：早报和扫描；当前业务日历固定北京时间，勿仅改 timezone 期待整体切换 |
| limits | max_text_chars/context_ttl_hours/clarify_ttl_minutes/seen_retention_days/inbox_retention_days：文本与上下文限制、保留期配置；实际清理策略需按源码确认 |
| backup | pg_bin/directory/keep_days：原生 pg_dump 工具路径、备份目录及保留天数，由安装脚本填写 |
| logging | level/file/max_bytes/backup_count：日志路径与轮转 |

关闭 style 的三个布尔开关可停用模型早报措辞。模型地址须提供兼容 Chat Completions 的工具调用接口；更换服务商后运行显式的模型验收。

凭据文件必须为 600 或 640，不允许 others 读取或 group 写入。配置中的密钥只写文件路径；真实配置、日志、数据库和备份均不可公开。
