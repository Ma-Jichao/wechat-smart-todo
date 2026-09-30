# 原生部署基线

目标是复现原生 IvorySQL + Python 虚拟环境 + systemd 的部署。脚本保持现有部署步骤，并对发行版、架构和路径做检测。

## 固定组件

| 项目 | 基线 |
|---|---|
| 操作系统 | Ubuntu 22.04 |
| 数据库 | 原生 IvorySQL 5.6，PostgreSQL 协议端口 127.0.0.1:5432 |
| Python | 系统 Python 3.10，项目内 venv |
| 应用目录 | /opt/reminder-bot |
| 系统用户 | reminderbot，nologin |
| 数据库角色与库 | 使用者选择新的库及专用角色；默认 wechat_todo / wechat_todo |
| 业务 schema | bot，4 个版本迁移，11 张表 |
| 通道 | ILinkApi 直接连接微信 iLink，自研长轮询 |
| 服务 | reminder-bot.service，4 个协程的单进程 |
| 备份 | reminder-backup.timer，每日 03:37，保留 14 天 |

## 步骤对照

1. 按 [IvorySQL 官方原生安装说明](https://docs.ivorysql.org/en/ivorysql-doc/v5.6/3.1) 安装 IvorySQL；初始化数据目录，建立 ivorysql.service，只监听本机。
2. 创建低权限数据库角色 reminder 与 reminder 库；应用不使用超级用户。
3. 创建 reminderbot 系统用户，将源码安装到 /opt/reminder-bot，建立 venv 并安装 requirements.txt。
4. 用户填写自己的模型地址、模型名和 API key；使用 SDK 的扫码登录获取自己的微信账号信息和 token。
5. 将模板填为 etc/config.ini；凭据只保存到独立文件。etc 目录 750、配置及凭据 640，root:reminderbot。
6. 用服务用户运行 scripts/migrate.py，应用迁移；运行 app.main --check 校验配置与 SDK 契约。
7. 安装 deploy/ 中的现有 systemd 主服务与备份定时器，启动服务。
8. 用户在微信主动发送消息建立会话，创建一条真实提醒，并自行确认到点实收。

安装脚本不能替用户输入密钥或完成手机授权。已有数据库、配置或服务必须先识别，不自动覆盖或重置。

## 登录接口

项目锁定 wechatbot-sdk==0.3.0。SDK 提供 `wechatbot.auth.login(ILinkApi(), cred_path=..., force=True)`，显示扫码链接并在需要时要求输入手机配对码。登录结果包含 token、base_url、account_id、user_id，应写入受保护的本地配置，不打印或上传。

登录凭据保存在调用方指定位置，不能依赖旧用户备份，也不能把历史 credentials.json 当作公开模板。

## 安装脚本行为

- `--dry-run`：仅检测系统、CPU、数据库二进制路径并显示计划；不连接数据库、不创建文件。
- `--pg-bin /自定义路径/bin`：选择已有原生 IvorySQL；发现多个安装时要求明确选择。
- `--install-dir /opt/自选目录`：调整应用路径。已有应用服务或非空目标目录仍会停止，不能用于覆盖升级。
- 已有实例：管理员密码仅用于本次连接，不保存。核实 `SELECT version()` 是 IvorySQL 后，用户选择新的数据库和角色，输入 `CREATE 数据库名` 才执行创建。
- 没有实例：输入 `INSTALL IVORYSQL` 才下载、校验并安装官方 5.6 deb/rpm 包，创建原生数据目录和 systemd 服务。不会自动替换已存在的数据目录或监听端口。
- 应用安装会显示步骤；缺少的 Python 和 venv 系统包会显示清单并征求同意。
- 新实例超级用户口令在 `/root/.ivorysql/superuser.pw`；新应用凭据在安装目录的 `etc/`。已有实例管理员密码不会写盘。
- 拒绝同意即停止；不删除已有数据库或角色，不自动回滚删除已经创建的新资产。

## Linux 差异适配与验证边界

| 环境 | 处理 |
|---|---|
| Ubuntu / Debian | 自动识别 apt/dpkg；优先使用系统 Python，缺 venv 时安装对应包 |
| RHEL / Rocky / AlmaLinux / Fedora 等 rpm 系统 | 自动识别 dnf/yum；缺受支持 Python 时尝试 python3.11；发行版完整安装仍待实测 |
| amd64 / arm64 | 选择锁定的官方对应 deb/rpm 文件并核对 SHA256 |
| 自定义已有实例 | 探测常见 bin 目录及进程路径；多实例或特殊路径用 --pg-bin |
| 无 systemd、其他包管理器、未支持架构 | 明确停止，不擅自更换数据库或引入容器 |

已有实例不升级、不改数据库服务配置。脚本读取现有数据库服务名作为应用启动依赖；备份时间明确为 Asia/Shanghai，避免宿主机时区差异。

安装包 URL 和 SHA256 锁定自 [IvorySQL 5.6 官方发布](https://github.com/IvorySQL/IvorySQL/releases/tag/IvorySQL_5.6)。软件版本升级时需同步复核包名、校验值与安装路径，不使用任意第三方镜像。

## 中断与升级

若在建库后因扫码或依赖失败而中断，新库和凭据会保留。脚本不会在第二次执行时覆盖非空目录，也不会自动删库。检查终端提示及 etc/ 中已生成的文件，再按源码方式完成剩余步骤；需要自动续装的行为尚未实现。

已有应用升级先备份、审阅代码差异和新增迁移，人工执行；本脚本负责新安装。
