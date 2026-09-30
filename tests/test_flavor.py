"""早报「温度层」的防编造测试。

这是这个功能的要害：模型写的点评里一旦出现早报里没有的数字，就说明它在编事实
（「还剩几天」「拖了多久」「金额」这类），必须整句丢弃、退回确定性版本。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import flavor, format

SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeTask:
    def __init__(self, task_id, content, remind_at=None, status="open"):
        self.id = task_id
        self.content = content
        self.remind_at = remind_at
        self.status = status


def groups_fixture():
    generated_at = datetime(2026, 9, 29, 8, 0, tzinfo=SHANGHAI)
    groups = {
        "today": [FakeTask(12, "整理项目材料", datetime(2026, 9, 29, 15, 0, tzinfo=SHANGHAI))],
        "overdue": [FakeTask(9, "交季度报表", datetime(2026, 9, 27, 15, 0, tzinfo=SHANGHAI))],
        "unscheduled": [FakeTask(15, "买牛奶", None)],
    }
    return groups, generated_at


def test_opening_is_computed_by_code() -> None:
    groups, at = groups_fixture()
    text = flavor.build_opening(groups, at)
    assert "3 件事" in text, text
    assert "1 件已经逾期" in text, text

    empty = flavor.build_opening({"today": [], "overdue": [], "unscheduled": []}, at)
    assert "没有待办" in empty, empty
    print("[通过] 开场白里的件数与逾期件数由代码算，不交给模型")


def test_overdue_days_computed_by_code() -> None:
    groups, at = groups_fixture()
    assert flavor.overdue_days(groups["overdue"][0], at) == 2
    assert flavor.overdue_days(groups["today"][0], at) is None, "未到期不算逾期"
    # 逾期天数由代码算，并直接体现在清单的行尾括注里
    text, _ = format.digest(groups, at, False)
    assert "⚠️ 逾期 2 天" in text, text
    assert "↳ 原定 9月27日 15:00" in text, text
    print("[通过] 逾期天数由代码算，以 ⚠️ 行内标记 + 原定时间副行呈现")


def test_guard_rejects_any_numeral() -> None:
    """核心规则：模型写的话里一个数字都不许有，阿拉伯和中文都不行。"""
    groups, at = groups_fixture()
    ids = [t.id for key in ("today", "overdue", "unscheduled") for t in groups[key]]

    payload = {
        "opening": "先把最碍眼的处理掉，别让小事拖成心事。",
        "notes": {
            "12": "还剩 5 天就要交了，抓紧",          # 阿拉伯数字 → 丢
            "9": "已经拖了三天了，今天夺回来",          # 中文数字 → 丢
            "15": "顺手就能办掉，出门顺手。",           # 干净 → 留
            "999": "这条编号不存在",                  # 编号不存在 → 丢
        },
        "closing": "一件一件收掉，今天你就赢了。",       # 「一件」是数量 → 丢
    }
    got = flavor.sanitize(payload, ids)

    assert got.opening == "先把最碍眼的处理掉，别让小事拖成心事。", got.opening
    assert 12 not in got.notes, "含阿拉伯数字（报数量）的点评必须被丢弃"
    assert 9 not in got.notes, "含中文数字+量词（报数量）的点评必须被丢弃"
    assert got.notes.get(15) == "顺手就能办掉，出门顺手。", got.notes
    assert 999 not in got.notes, "编号不存在的事项必须被丢弃"
    assert got.closing is None, "含数量的结尾必须被丢弃"
    assert len(got.rejected) == 4, got.rejected
    print(f"[通过] 防编造：报数量的写法一律丢弃（阿拉伯数字 / 中文数字+量词），共丢 {len(got.rejected)} 处")

    # 中文数字的各类写法都要挡住
    for bad in ("还剩五天", "拖了三天", "有两件", "第一件", "半个方案", "三十分钟",
                "还剩10天", "用了２天", "三千", "十二个", "两半"):
        assert flavor.check_flourish(bad, limit=40) is None, f"应被拒绝：{bad}"
    for good in ("先把它清掉", "别再压着", "材料记得带上", "顺手办掉就好",
                 "一起把它做掉", "一眼就看出来", "一路顺风"):
        assert flavor.check_flourish(good, limit=40) == good, f"应被接受：{good}"
    print("[通过] 数量写法（中文数字+量词/叠写、全角数字）全挡住，含「一」的习语正常通过")


def test_guard_rejects_markdown_and_oversize() -> None:
    groups, at = groups_fixture()
    ids = [t.id for key in ("today", "overdue", "unscheduled") for t in groups[key]]

    payload = {
        "opening": "**加粗**不应该出现",
        "notes": {"9": "x" * 100, "15": "换\n行也不允许"},
        "closing": "# 井号开头",
    }
    got = flavor.sanitize(payload, ids)
    assert "*" not in (got.opening or ""), got.opening
    assert got.opening == "加粗不应该出现", got.opening
    assert 9 not in got.notes, "超长点评应丢弃"
    assert 15 not in got.notes, "换行的点评应丢弃"
    assert got.closing == "井号开头", got.closing
    assert len(got.rejected) == 2, got.rejected
    print("[通过] 超长、含换行、含 Markdown 的模型输出都会被处理或丢弃")


def test_parse_payload_tolerates_noise() -> None:
    assert flavor.parse_payload('{"opening": "早"}') == {"opening": "早"}
    assert flavor.parse_payload('```json\n{"opening": "早"}\n```') == {"opening": "早"}
    assert flavor.parse_payload('好的，这是结果：{"closing": "加油"}') == {"closing": "加油"}
    assert flavor.parse_payload("完全不是 JSON") == {}
    assert flavor.parse_payload("") == {}
    assert flavor.parse_payload('["不是对象"]') == {}
    print("[通过] 模型回复带噪声（代码块/前后缀/非对象）也能安全解析，失败返回空")


def test_composer_failure_never_breaks_digest() -> None:
    """模型报错或超时，必须退回无温度版本，而不是让早报发不出去。"""
    import asyncio

    class BoomLlm:
        async def complete_json(self, **kwargs):
            raise RuntimeError("模型挂了")

    class SlowLlm:
        async def complete_json(self, **kwargs):
            return {"opening": "", "notes": {}, "closing": ""}

    groups, at = groups_fixture()
    ids = [t.id for key in ("today", "overdue", "unscheduled") for t in groups[key]]

    async def run():
        boom = flavor.FlavorComposer(BoomLlm(), enabled=True)
        got = await boom.compose(groups, at, ids)
        assert got.opening is None and got.notes == {} and got.closing is None, got

        empty = flavor.FlavorComposer(SlowLlm(), enabled=True)
        got2 = await empty.compose(groups, at, ids)
        assert got2.opening is None and got2.closing is None, got2

        off = flavor.FlavorComposer(BoomLlm(), enabled=False)
        got3 = await off.compose(groups, at, ids)
        assert got3.opening is None, "关掉开关时不应调用模型"

    asyncio.run(run())
    print("[通过] 模型失败/返回空/开关关闭都不影响早报，退回确定性版本")


def main() -> int:
    test_opening_is_computed_by_code()
    test_overdue_days_computed_by_code()
    test_guard_rejects_any_numeral()
    test_guard_rejects_markdown_and_oversize()
    test_parse_payload_tolerates_noise()
    test_composer_failure_never_breaks_digest()
    print("\n温度层全部用例通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
