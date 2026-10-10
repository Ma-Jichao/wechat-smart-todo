"""排版测试。

微信是纯文本渲染，所以排版必须自己保证可读。这里的断言同时守住调研得出的几条约定：
序号用 `1.` 而不是键帽 emoji、点评用 ↳ 独立成行、清单顶格连续、不出现横线或 Markdown 标记。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import format

SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeTask:
    def __init__(self, task_id, content, remind_at=None, status="open"):
        self.id = task_id
        self.content = content
        self.remind_at = remind_at
        self.status = status


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=SHANGHAI)


def test_markdown_stripping() -> None:
    cases = {
        "**粗体**": "粗体",
        "__下划线__": "下划线",
        "`代码`": "代码",
        "## 标题": "标题",
        "- 列表项": "列表项",
        "## 📋 待办\n- **重要**：1. 做A": "📋 待办\n重要：1. 做A",
    }
    for raw, expected in cases.items():
        got = format.strip_markdown(raw)
        assert got == expected, f"{raw!r} -> {got!r}，期望 {expected!r}"
    assert "*" not in format.strip_markdown("**a** *b* ***c*** a*b")
    print("[通过] Markdown 标记被剥离，星号不会漏到用户眼前")


def test_time_and_day_formats() -> None:
    monday = at(28, 15, 0)
    assert format.weekday_cn(monday) == "一"
    assert format.format_datetime(monday) == "2026年9月28日（周一）15:00"
    assert format.format_short_datetime(monday) == "9月28日 15:00"
    assert format.format_time(monday) == "15:00"
    assert format.format_date_cn(monday.date()) == "9月28日 周一"

    ref = at(28, 8, 0)
    assert format.day_label(at(28, 15), ref) == "今天"
    assert format.day_label(at(29, 9), ref) == "明天"
    assert format.day_label(at(30, 9), ref) == "后天"
    assert format.day_label(at(27, 9), ref) == "昨天"
    assert format.day_label(at(25, 9), ref) == "9月25日"
    print("[通过] 时间与相对日写法正确（含无前导零、星期、今天/明天/后天/昨天）")


def test_confirmations() -> None:
    text = format.created(FakeTask(12, "整理项目材料", at(29, 15)), remaining=3)
    assert text.startswith("✅"), text
    assert "#" not in text and "2026年9月29日（周二）15:00" in text, text
    assert "北京时间" in text, "必须明确时区"
    assert "还有 3 件" in text, text
    assert "*" not in text

    no_time = format.created(FakeTask(13, "买牛奶", None))
    assert "没设提醒时间" in no_time and "还有" not in no_time, no_time

    upd = format.updated(FakeTask(12, "整理项目材料", at(30, 9, 30)))
    assert upd.startswith("✏️") and "2026年9月30日（周三）09:30" in upd, upd
    assert "旧的那次不会再提醒" in upd, upd

    assert "手上清空了" in format.completed(FakeTask(1, "x"), remaining=0)
    assert "还有 2 件" in format.completed(FakeTask(1, "x"), remaining=2)
    assert format.canceled(FakeTask(1, "x")).startswith("🚫")
    print("[通过] 创建/修改/完成/取消都用口语化文案，且带完整日期时间、时区")


def test_item_line_conventions() -> None:
    ref = at(29, 8, 0)
    assert format.item_line(1, FakeTask(12, "整理项目材料", at(29, 9, 30)), ref) == \
        "1. 09:30 整理项目材料"
    assert format.item_line(2, FakeTask(9, "交季度报表", at(26, 15)), ref) == \
        "2. ⚠️ 逾期 3 天 交季度报表"
    assert format.item_line(3, FakeTask(15, "买牛奶", None), ref) == \
        "3. 未定时间 买牛奶"
    # 跨天的事项才写日期
    assert format.item_line(4, FakeTask(20, "评审会", at(29, 10) + timedelta(days=2)), ref) == \
        "4. 后天 10:00 评审会"
    assert format.item_line(5, FakeTask(21, "远处的事", at(29, 10) + timedelta(days=8)), ref) == \
        "5. 10月7日 10:00 远处的事"
    # 逾期项的事实副行由代码给出原定时间
    assert format.item_fact_note(FakeTask(9, "交季度报表", at(26, 15)), ref) == "原定 9月26日 15:00"
    assert format.item_fact_note(FakeTask(12, "整理项目材料", at(29, 9, 30)), ref) is None
    print("[通过] 单条清单行：序号用 `1.`、逾期/未定时间用行内标记")


def test_list_and_empty() -> None:
    ref = at(29, 8, 0)
    text = format.task_list([
        FakeTask(12, "整理项目材料", at(29, 9, 30)),
        FakeTask(20, "下周的评审", at(29, 10) + timedelta(days=7)),
    ], reference=ref)
    assert text.startswith("📋") and "2 件" in text, text
    assert "1. 09:30 整理项目材料" in text, text
    assert "2. 10月6日 10:00 下周的评审" in text, text
    assert "回复「完成 N」标记第 N 条" in text, text
    single = format.task_list([FakeTask(12, "整理项目材料", at(29, 9, 30))], reference=ref)
    assert "回复「完成 1」标记完成" in single, single
    assert "完成 2" not in single, single
    assert format.task_list([], reference=ref).startswith("🎉")
    assert format.empty_scope("today").startswith("🎉")
    print("[通过] 待办列表沿用同一套清单行约定，空列表有独立文案")


def test_digest_layout() -> None:
    now = at(29, 8, 0)
    groups = {
        "today": [FakeTask(12, "整理项目材料", at(29, 15))],
        "overdue": [FakeTask(9, "交季度报表", at(27, 15))],
        "unscheduled": [FakeTask(15, "买牛奶", None)],
    }
    text, ordered = format.digest(groups, now, late=False)
    lines = text.split("\n")

    assert lines[0] == "☀️ 9月29日 周二 · 今日待办 3 件", lines[0]
    assert lines[1] == "", lines[:3]

    # 清单紧跟标题、顶格、连续编号，中间不插任何标题或空段
    assert lines[2] == "1. 15:00 整理项目材料", lines[:6]
    assert lines[3] == "2. ⚠️ 逾期 2 天 交季度报表", lines[:6]
    assert lines[4] == "↳ 原定 9月27日 15:00", lines[:6]
    assert lines[5] == "3. 未定时间 买牛奶", lines[:6]
    assert not any(line.lstrip() != line for line in lines), "全篇不应有任何行首空白"

    assert "共 3 件 · 08:00 生成" in text, text
    assert "回复「完成 N」标记第 N 条" in text, text
    assert [t.id for t in ordered] == [12, 9, 15], [t.id for t in ordered]

    # 模型点评追加到事实副行之后，同样独立成行
    noted, ordered2 = format.digest(
        groups, now, late=False, notes={9: "这件一直压着，今天先把它清掉。"})
    assert "↳ 原定 9月27日 15:00 · 这件一直压着，今天先把它清掉。" in noted, noted
    assert "1. 15:00 整理项目材料" in noted, noted
    assert [t.id for t in ordered2] == [12, 9, 15], "加点评不能打乱序号"

    # 问候与鼓励一律放在清单之后
    full, _ = format.digest(
        groups, now, late=False,
        commentary="早上好。今天有 3 件事要盯，其中 1 件已经逾期。",
        closing="先动手，节奏就回来了。")
    assert full.split("\n")[2] == "1. 15:00 整理项目材料", "第一行正文必须是第 1 条"
    assert full.index("早上好。") > full.index("回复「完成"), "问候应在清单与指令之后"
    assert full.rstrip().endswith("先动手，节奏就回来了。"), full

    late_text, _ = format.digest(groups, now, late=True)
    assert "延迟补发" in late_text, late_text

    empty_text, empty_ordered = format.digest(
        {"today": [], "overdue": [], "unscheduled": []}, now, late=False)
    assert empty_ordered == [] and "🎉 今天没有待办" in empty_text, empty_text
    print("[通过] 早报：清单顶格连号在最前、逾期在原位标 ⚠️、点评独立成行、问候在最后")


def test_reminder_and_notices() -> None:
    on_time = format.reminder("整理项目材料", at(29, 15), late=False)
    assert on_time.startswith("⏰ 到点提醒"), on_time
    assert "整理项目材料" in on_time, on_time
    assert "#" not in on_time, on_time
    assert "回复「完成」标记完成" in on_time, on_time

    late = format.reminder("整理项目材料", at(29, 15), late=True)
    assert "补发" in late and "原定 9月29日 15:00" in late, late

    rec = format.recurring("打卡")
    assert rec.startswith("⏰ 每日提醒") and "记得打卡" in rec, rec
    assert "完成" not in rec, "重复提醒不应带「完成」指令"

    made = format.created_recurring(FakeTask(7, "打卡"), ["08:15", "18:00"], True)
    assert "每天 08:15、18:00" in made and "法定节假日" in made, made
    assert "#" not in made, made

    ranged = format.created_recurring(
        FakeTask(8, "更新汇报"), ["08:00"], False,
        repeat_start=date(2026, 10, 1), repeat_end=date(2026, 10, 6))
    assert "每天 08:00" in ranged and "从 10月1日 到 10月6日" in ranged, ranged

    assert format.needs_clarification("几点？").startswith("🤔")
    assert format.needs_clarification("**几点**？") == "🤔 几点？"
    assert format.llm_failed().startswith("😥")
    assert format.voice_unreadable().startswith("🎙")
    assert format.no_text_content().startswith("🤷")
    assert format.system_notice("注意").startswith("🔔")
    print("[通过] 提醒与各类提示语都带图标")


def test_assembled_reply() -> None:
    """无工具调用的闲聊/确认消息，不该一律加 🤔 反问。"""
    # 有确认语 + 模型追问 → 确认语在前，追问附后
    both = format.assembled_reply(["✅ 记好了"], "请问是明天吗？")
    assert both.startswith("✅ 记好了") and "请问是明天吗？" in both, both
    # 有确认语 + 模型无追问 → 只回确认语，不附冗余
    assert format.assembled_reply(["✅ 记好了"], "好的") == "✅ 记好了"
    # 无确认语 + 追问 → 🤔 澄清
    assert format.assembled_reply([], "几点呢？").startswith("🤔")
    # 无确认语 + 简短确认 → 原样输出，不加 🤔（「好滴」场景）
    assert format.assembled_reply([], "好嘞") == "好嘞"
    # 无确认语 + 无文本 → 兜底反问
    assert format.assembled_reply([], "").startswith("🤔")
    # Markdown 会被剥掉
    assert format.assembled_reply([], "**好**") == "好"
    print("[通过] 最终回复拼装：确认/寒暄原样回应，追问才带 🤔，确认语后只附追问")


def test_no_risky_characters_anywhere() -> None:
    """统一检查所有对外文案：无 Markdown 残留、无横线类字符、无键帽 emoji、无行首空白。"""
    now = at(29, 8, 0)
    groups = {"today": [FakeTask(1, "事项", at(29, 9))],
              "overdue": [FakeTask(2, "逾期事项", at(27, 9))],
              "unscheduled": [FakeTask(3, "未定事项", None)]}
    samples = [
        format.created(FakeTask(1, "事项", at(29, 9)), remaining=2),
        format.created(FakeTask(1, "事项", None)),
        format.updated(FakeTask(1, "事项", at(29, 9))),
        format.completed(FakeTask(1, "事项"), remaining=0),
        format.canceled(FakeTask(1, "事项")),
        format.task_list([FakeTask(1, "事项", at(29, 9))], reference=now),
        format.task_list([]),
        format.empty_scope("today"),
        format.digest(groups, now, False)[0],
        format.digest({"today": [], "overdue": [], "unscheduled": []}, now, False)[0],
        format.digest(groups, now, False, commentary="早上好。", notes={2: "先清掉"},
                      closing="加油")[0],
        format.reminder("事项", at(29, 9), late=True),
        format.needs_clarification("要不要？"),
        format.assembled_reply(["✅ 记好了"], "请问是明天吗？"),
        format.assembled_reply([], "好嘞"),
        format.assembled_reply([], ""),
        format.failure_notice("出错了"),
        format.system_notice("注意"),
        format.llm_failed(),
        format.voice_unreadable(),
        format.no_text_content(),
        format.auth_expired_notice(),
    ]
    # 会渲染成「一条粗线」的字符，以及用户已明确否掉的横线
    risky_lines = "━▬█▇═─┄┅┈┉╌╍▁▔￣﹏—"
    keycaps = [f"{i}\u20e3" for i in range(1, 10)] + ["\U0001f51f"]

    for text in samples:
        assert text, "文案不能为空"
        assert not text.startswith("\n"), text
        assert "*" not in text and "`" not in text, f"残留 Markdown：{text!r}"
        for ch in risky_lines:
            assert ch not in text, f"出现会被渲染成横线的字符 {ch!r}：{text!r}"
        for kc in keycaps:
            assert kc not in text, f"出现键帽 emoji（部分客户端渲染异常）{kc!r}：{text!r}"
        for line in text.split("\n"):
            assert line == line.lstrip(), f"行首出现空白（微信会吃掉）：{line!r}"
            assert len(line) <= 60, f"单行过长（{len(line)}）：{line!r}"
    print(f"[通过] {len(samples)} 种对外文案统一检查：无 Markdown、无横线、无键帽 emoji、无行首空白、单行≤60字")


def main() -> int:
    test_markdown_stripping()
    test_time_and_day_formats()
    test_confirmations()
    test_item_line_conventions()
    test_list_and_empty()
    test_digest_layout()
    test_reminder_and_notices()
    test_assembled_reply()
    test_no_risky_characters_anywhere()
    print("\n排版全部用例通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
