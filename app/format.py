"""微信消息排版。

**微信是纯文本渲染，不认 Markdown**：`**粗体**` 会原样显示出星号，`# 标题`
会看到井号。所以排版只用：emoji、换行、可见符号标记。

版式参考（本次按用户要求调研后采用的成熟做法）：

  * 主参考：开源提醒机器人 `qq-reminder`（全程纯文本、不依赖任何渲染）——
    标题行 + 空行 + 顶格连续编号 + 稳定编号 + 点评独立成行 + 结尾回复指令。
    见 https://github.com/Spectrum-o/qq-reminder
  * 「时间放行首、逾期写明天数」参考成熟待办产品的通知结构
    （Apple 提醒事项 / OmniFocus / TickTick / Things 的通知都是相对日 + 时间在前；
    轻流、石墨的产品文档明确要求逾期必须写清「逾期了多少天」，否则会被忽略）。
  * 企业微信 `markdown` / 钉钉与飞书卡片**依赖客户端渲染**，在纯文本通道会把
    `**`、`#`、`<font>`、`---` 原样露出来，因此**不采用**。

几条硬规矩（都写过测试守着）：

  * 清单**顶格、连续编号**，中间不插标题或空段，不让别的内容把它往下挤。
  * 序号用 `1.` 而不是键帽 emoji `1⃣`——部分客户端会把键帽渲染成「数字+方框」
    或宽窄不一。
  * 点评独立成行、行首用 `↳`；**不用空格缩进**，因为微信会吃掉行首空白。
  * **不用任何横线类字符**（`━ ▬ ═ ─ —` 等），它们会渲染成一条很重的黑线。
  * **不用 Markdown 标记**；模型输出里的标记由 strip_markdown 统一剥掉。
  * 时间一律可核验：能看到具体时刻与日期，逾期项额外写明逾期天数与原定时间。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

WEEKDAY_CN = "一二三四五六日"

SECTION_TODAY = "today"
SECTION_OVERDUE = "overdue"
SECTION_UNSCHEDULED = "unscheduled"

# 点评行的行首标记。用非空白字符，避免被微信吃掉行首空白。
NOTE_MARK = "↳"

MARKDOWN_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
)


def strip_markdown(text: str) -> str:
    """去掉 Markdown 标记，保留文字。星号在微信里会原样显示，所以必须清掉。"""
    result = text or ""
    for pattern, repl in MARKDOWN_PATTERNS:
        result = pattern.sub(repl, result)
    result = result.replace("*", "")
    return result


# ---------------------------------------------------------------------------
# 时间与日期
# ---------------------------------------------------------------------------
def weekday_cn(moment: datetime) -> str:
    return WEEKDAY_CN[moment.weekday()]


def format_datetime(moment: datetime) -> str:
    """确认语里用的完整写法：年月日 + 星期 + 时刻 + 时区。"""
    return (f"{moment.year}年{moment.month}月{moment.day}日"
            f"（周{weekday_cn(moment)}）{moment.hour:02d}:{moment.minute:02d}")


def format_short_datetime(moment: datetime) -> str:
    return f"{moment.month}月{moment.day}日 {moment.hour:02d}:{moment.minute:02d}"


def format_time(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def format_date_cn(day: date) -> str:
    return f"{day.month}月{day.day}日 周{WEEKDAY_CN[day.weekday()]}"


def day_label(moment: datetime, reference: datetime | None = None) -> str:
    """相对日写法：今天 / 明天 / 后天 / 昨天 / 具体日期。

    成熟待办产品的通知都用相对口径，比让人去算绝对日期更不易看错。
    """
    base = (reference or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()
    target = moment.astimezone(SHANGHAI).date()
    delta = (target - base).days
    return {
        0: "今天", 1: "明天", 2: "后天", -1: "昨天", -2: "前天",
    }.get(delta, f"{target.month}月{target.day}日")


def overdue_days(task: Any, reference: datetime) -> int | None:
    """逾期的天数。由代码算，不让模型猜——这是用户会照着行动的事实。"""
    if getattr(task, "remind_at", None) is None:
        return None
    moment = task.remind_at.astimezone(reference.tzinfo or SHANGHAI)
    days = (reference.date() - moment.date()).days
    return days if days > 0 else None


# ---------------------------------------------------------------------------
# 单条清单行
# ---------------------------------------------------------------------------
def item_line(index: int, task: Any, reference: datetime) -> str:
    """一行一条：`序号. 标记 时刻 内容`。

    标记只在需要时出现：逾期写 `⚠️ 逾期 N 天`，未设时间写 `未定时间`。
    今天的事只写时刻——标题里已经有日期了，重复写会变吵。
    内部自增编号不进用户界面；要指代某条时用显示序号或关键词。
    """
    if task.remind_at is None:
        return f"{index}. 未定时间 {task.content}"
    days = overdue_days(task, reference)
    if days:
        return f"{index}. ⚠️ 逾期 {days} 天 {task.content}"
    moment = task.remind_at.astimezone(SHANGHAI)
    prefix = format_time(moment) if moment.date() == reference.astimezone(SHANGHAI).date() \
        else f"{day_label(moment, reference)} {format_time(moment)}"
    return f"{index}. {prefix} {task.content}"


def item_fact_note(task: Any, reference: datetime) -> str | None:
    """由代码写的事实性副行（目前只有逾期项的原定时间）。"""
    days = overdue_days(task, reference)
    if not days or task.remind_at is None:
        return None
    return f"原定 {format_short_datetime(task.remind_at.astimezone(SHANGHAI))}"


def _item_block(index: int, task: Any, reference: datetime,
                notes: dict[int, str] | None) -> list[str]:
    lines = [item_line(index, task, reference)]
    parts = [item_fact_note(task, reference), (notes or {}).get(task.id)]
    detail = " · ".join(p for p in parts if p)
    if detail:
        lines.append(f"{NOTE_MARK} {detail}")
    return lines


# ---------------------------------------------------------------------------
# 确认类
# ---------------------------------------------------------------------------
def created(task: Any, *, remaining: int | None = None) -> str:
    lines = ["✅ 记好了", f"📌 {task.content}"]
    if task.remind_at is None:
        lines.append("📝 没设提醒时间，我会放进每天早报里")
        lines.append("")
        lines.append("想加个时间随时说，比如「明天下午三点提醒我」")
    else:
        lines.append(f"⏰ {format_datetime(task.remind_at)}（北京时间）")
        lines.append("")
        lines.append("到时候我喊你。要改时间就说「改到后天下午三点」。")
    if remaining is not None and remaining > 0:
        lines.append(f"现在手上还有 {remaining} 件。")
    return "\n".join(lines)


def _cn_day(day: date) -> str:
    return f"{day.month}月{day.day}日"


def created_recurring(
    task: Any,
    times: Iterable[str],
    skip_holidays: bool,
    *,
    repeat_start: date | None = None,
    repeat_end: date | None = None,
) -> str:
    lines = ["✅ 记好了", f"📌 {task.content}"]
    lines.append(f"🔁 每天 {'、'.join(times)} 提醒你")
    if repeat_start is not None and repeat_end is not None:
        lines.append(f"🗓 从 {_cn_day(repeat_start)} 到 {_cn_day(repeat_end)}")
    elif repeat_end is not None:
        lines.append(f"🗓 到 {_cn_day(repeat_end)} 为止")
    if skip_holidays:
        lines.append("🗓 法定节假日自动跳过")
    lines.append("")
    lines.append("想改时间或停掉，随时说。")
    return "\n".join(lines)


def updated(task: Any) -> str:
    lines = ["✏️ 改好了", f"📌 {task.content}"]
    if task.remind_at is None:
        lines.append("📝 提醒时间去掉了，改为只在早报里列出")
    else:
        lines.append(f"⏰ {format_datetime(task.remind_at)}（北京时间）")
    lines.append("")
    lines.append("新时间记下了，旧的那次不会再提醒你。")
    return "\n".join(lines)


def completed(task: Any, *, remaining: int | None = None) -> str:
    lines = ["✅ 完成", f"📌 {task.content}", ""]
    if remaining is None:
        lines.append("这件结了 👍")
    elif remaining == 0:
        lines.append("这件结了，手上清空了 🎉")
    else:
        lines.append(f"这件结了。还有 {remaining} 件在盯着。")
    return "\n".join(lines)


def canceled(task: Any) -> str:
    return (f"🚫 取消了\n📌 {task.content}\n\n"
            "这条不再提醒你。记录我给你留着，想恢复随时说。")


# ---------------------------------------------------------------------------
# 列表与早报
# ---------------------------------------------------------------------------
def complete_hint(count: int) -> str:
    """按清单条数给「完成 N」提示。单条就指向第 1 条，避免只有一条却提示「完成第 2 条」。"""
    if count <= 1:
        return "回复「完成 1」标记完成"
    return "回复「完成 N」标记第 N 条"


def task_list(tasks: Iterable[Any], title: str = "手上的待办",
              reference: datetime | None = None) -> str:
    now = reference or datetime.now(SHANGHAI)
    items = list(tasks)
    if not items:
        return "🎉 手上没有未完成的事，清清爽爽"
    lines = [f"📋 {title} {len(items)} 件", ""]
    for index, task in enumerate(items, start=1):
        lines.extend(_item_block(index, task, now, None))
    lines.append("")
    lines.append(complete_hint(len(items)))
    return "\n".join(lines)


def empty_scope(scope: str) -> str:
    return {
        "today": "🎉 今天没有待办，轻松一点",
        "overdue": "🎉 没有逾期的事，保持住",
        "unscheduled": "🎉 没有挂着的事",
    }.get(scope, "🎉 手上没有未完成的事")


def digest(
    groups: dict[str, list[Any]],
    generated_at: datetime,
    late: bool,
    *,
    commentary: str | None = None,
    notes: dict[int, str] | None = None,
    closing: str | None = None,
) -> tuple[str, list[Any]]:
    """返回 (正文, 按显示顺序排列的事项)。显示序号就是正文里的序号。

    结构（照抄成熟纯文本待办机器人的做法）::

        标题行（日期 + 件数）
        <空行>
        1. 标记 时刻 内容 #稳定编号
        ↳ 事实副行 · 点评
        ...
        <空行>
        共 N 件 · HH:MM 生成
        回复「完成 2」可标记第 2 条
        <空行>
        （温度层的问候与鼓励）

    要点：清单顶格连续编号、中间不插任何标题；分组信息退到行内标记；
    点评独立成行用 ↳ 而不是空格缩进；感叹/问候一律放在清单之后。
    """
    ordered: list[Any] = []
    for key in (SECTION_TODAY, SECTION_OVERDUE, SECTION_UNSCHEDULED):
        ordered.extend(groups.get(key) or [])

    header = f"☀️ {format_date_cn(generated_at.date())} · 今日待办 {len(ordered)} 件"

    lines: list[str] = []
    for index, task in enumerate(ordered, start=1):
        lines.extend(_item_block(index, task, generated_at, notes))

    footer: list[str] = []
    if ordered:
        footer.append(f"共 {len(ordered)} 件 · {format_time(generated_at)} 生成")
        footer.append(complete_hint(len(ordered)))
    else:
        footer.append(f"{format_time(generated_at)} 生成")

    parts = [header, ""]
    if ordered:
        parts.extend(lines)
        parts.append("")
    else:
        parts.append("🎉 今天没有待办，轻松一点")
        parts.append("")
    parts.extend(footer)

    tail = [t for t in (commentary, closing) if t]
    if tail:
        parts.append("")
        parts.extend(tail)

    text = "\n".join(parts).rstrip() + ("（延迟补发）" if late else "")
    return strip_markdown(text), ordered


# ---------------------------------------------------------------------------
# 提醒与通知
# ---------------------------------------------------------------------------
def reminder(content: str, scheduled_at: datetime, *, late: bool) -> str:
    lines = ["⏰ 到点提醒（补发）" if late else "⏰ 到点提醒"]
    lines.append(content)
    if late:
        lines.append(f"🕐 原定 {format_short_datetime(scheduled_at)}")
    lines.append("")
    lines.append("回复「完成」标记完成")
    return "\n".join(lines)


def recurring(content: str) -> str:
    """重复提醒的到点文案。不套用「完成」指令——它不是一次性待办。"""
    return f"⏰ 每日提醒\n到点啦，记得{content}。"


def needs_clarification(text: str) -> str:
    return "🤔 " + strip_markdown(text)


def assembled_reply(confirmations: list[str], model_text: str | None) -> str:
    """把工具确认语与模型自由文本拼成最终回复。

    有确认语时，模型文本只在带问号（真正在追问）时附在其后；没有确认语时，
    带问号的模型文本加 🤔 澄清前缀，陈述类文本（如寒暄「好嘞」）原样输出、
    不再误加 🤔。都没有时回兜底澄清句。
    """
    if confirmations:
        reply = "\n".join(confirmations)
        if model_text and ("？" in model_text or "?" in model_text):
            reply = f"{reply}\n\n{needs_clarification(model_text)}"
        return reply
    if model_text:
        if "？" in model_text or "?" in model_text:
            return needs_clarification(model_text)
        cleaned = strip_markdown(model_text).strip()
        if cleaned:
            return cleaned
    return "🤔 我没理解你的意思，能再说一次吗？"


def failure_notice(text: str) -> str:
    return "😥 " + strip_markdown(text)


def system_notice(text: str) -> str:
    return "🔔 " + strip_markdown(text)


def voice_unreadable() -> str:
    return ("🎙 这条语音没能转成文字\n"
            "没有记录任何内容。请改用文字发一次，或重发一条更清晰的语音。")


def no_text_content() -> str:
    return "🤷 这条消息里没有可识别的文字\n没有记录任何内容。"


def llm_failed() -> str:
    return ("😥 这条消息没有处理成功\n"
            "没有保存任何内容，请重新发一次。")


def auth_expired_notice() -> str:
    return ("🔐 iLink 会话已失效，提醒推送暂停\n"
            "需要重新扫码登录，请找管理员处理。队列里的待办不会丢失。")
