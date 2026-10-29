# 开发与 AI 修改指南

## 模块地图

| 文件 | 职责 |
|---|---|
| app/config.py | INI、凭据文件权限校验；REMINDER_CONFIG 或项目 etc/config.ini |
| app/weixin.py | iLink 长轮询、白名单、消息与游标原子写入 |
| app/main.py | 单实例租约、接收/处理/发送/调度协程守护 |
| app/llm.py | Chat Completions、受控工具调用、歧义澄清 |
| app/tools.py | 事项 CRUD、版本、审计、序号映射 |
| app/format.py / flavor.py | 确定性排版 / 可选模型措辞 |
| app/outbox.py / sender.py | 入队、分段、限流、失败分类与恢复 |
| app/scheduler.py | 一次性、重复提醒、早报和每周复盘调度 |
| app/important.py | 重要工作标记、历史查询、备注、周报设置与幂等入队 |
| migrations/ | 顺序应用的 schema 迁移，不改已应用文件 |

给 AI 的示例任务：

> 先读 AGENTS.md。为每天重复提醒增加一个新的可配置行为，保持现有默认语义。先列影响的模块，再修改源码、增加隔离测试和文档。不要读取真实配置、不要发送微信消息。

## 验证

`tests/test_format.py`、`test_flavor.py`、`test_install.py` 为离线测试。其他集成测试需要真实 IvorySQL 测试库，但发送接口使用桩。`test_llm_behaviour.py` 默认跳过，显式 `--llm-live` 才会真实调用模型并产生费用。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python tests/test_format.py
python tests/test_flavor.py
python tests/test_install.py
python tests/test_important_work.py
python tests/test_creation_dedup.py
python tests/test_reminder_operations.py
```

集成测试设置 `REMINDER_TEST_CONFIG` 指向独立测试配置（另用 `REMINDER_CONFIG` 或 --config 指定迁移配置），先迁移，再逐个执行测试脚本。不要指向生产库：部分恢复测试操作全局队列状态。测试不应并发运行于同一数据库。

开发优先保持四协程单进程结构。模型仅提议工具调用，实际写入成功与确认文本由代码决定。接收消息、语音转写、用户粘贴文本与模型输出都视为不可信输入。

创建事项在同一事务内取得用户级 advisory lock，并检查内容、时间、重复时段、节假日规则、起止日期和重要工作标记。完全相同的未完成事项拒绝重复创建；不同时间或设置的事项仍可创建，已完成/取消记录不阻止再次创建。`test_creation_dedup.py` 是离线业务回归；真实并发与取消后重建覆盖在 `test_tools_lifecycle.py`，需独立测试库。

事项变更支持 `task_type` 限定普通待办、重要工作或独立每日重复提醒。`stop_reminder` 仅清除独立提醒设置，保留未完成状态和重要工作标记；重复时刻用 `update_reminder.repeat_times` 修改。更新按版本做乐观并发检查，并在同一事务内作废旧投递。

`list_reminders` 的 `overview` 与 `all` 均返回完整提醒总览；内部 `tools.list_tasks(all)` 继续保留不含重复提醒的既有语义。总览读取每周设置时不创建/启用设置。`bot.conversations` 保存候选、原操作参数和最近清单顺序；候选选择优先，较新早报替代较旧查询清单。失败结果不交给模型自由改写，已成功的变更不会因后续模型失败被描述为未保存。`test_reminder_operations.py` 使用伪造模型响应及数据库，真实事务验证仍见 `test_tools_lifecycle.py`。

`test_provision_backup.py` 仅用于独立、可销毁的测试实例：需要管理员权限，创建和删除自己生成的随机测试库与角色，验证最小权限、备份和恢复。`test_reliability.py` 用模拟发送接口和真实子进程退出验证重复边界，不连接微信。不要在生产实例运行这两个测试。

数据库测试不会默认读取应用配置：必须显式设置 REMINDER_TEST_CONFIG，库名包含独立的 test 标记，外部地址设为 https://example.invalid。真实模型测试仅允许模型地址例外，微信仍禁止真实请求。

重要工作数据库集成测试：先在隔离 IvorySQL 测试库应用全部迁移，再运行 `python tests/test_important_work_db.py`。该测试使用随机用户，验证历史日期范围、早报映射、并发周报去重、改期撤销和完成后备注；不调用模型、不发送微信。
