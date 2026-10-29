"""早报的「温度」层。

分工是刻意的：

  * **事实全部由代码算**：编号、时间、星期日历、件数、逾期件数、逾期天数。
    这些是用户会照着行动的，绝不能由模型自由生成。
  * **只有措辞交给模型**：开场补充、每条待办下面一句人话点评、结尾一句鼓励。
  * **模型写的话里一个数字都不许有**——阿拉伯数字和中文数字都不行。
    这是本模块最硬的一条规则，理由是：模型一旦提到数量或天数，就可能算错
    （「还剩 5 天」「拖了五天」），而用户会照着行动。
    数量与天数一律由代码写，代码算的不会错。

  这条规则比「只允许清单里出现过的数字」更严密：后者挡不住中文数字，
  而且会把我们自己推导出来的数字（逾期天数、逾期件数）也误判成编造。
  收紧到「完全不许有数字」之后，规则好解释、实现好验证、也不会误伤。

另外这条路径**绝不阻塞早报投递**：模型超时、报错、输出不合规，一律退回无温度版本。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .format import format_date_cn, format_short_datetime, overdue_days, strip_markdown

log = logging.getLogger(__name__)

# 阿拉伯数字（含全角）：模型报数量、天数几乎都用这种写法，一律不采用
ARABIC_NUMERAL_RE = re.compile(r"[0-9０-９]")

# 中文数字后面**紧跟量词或另一个数字**才算在报数量
# （「五天」「两件」「第一件」「三小时」「三千」「十二个」）。
# 只禁这个组合、而不是禁所有中文数字，是因为「一」大量出现在习语里
# （一起、一眼、一路），一刀切会把好文案整片误杀。
# 已知代价：「一点」「一路」这类习语里带量词字的说法会被误杀，
# 因为「三点」和「一点」在中文里无法靠字面区分。点评是可选项，丢一句无害。
_CN_NUM = "〇零一二三四五六七八九十百千万亿两半壹贰叁肆伍陆柒捌玖拾佰仟"
_CN_UNIT = "天日周月年时秒点号件个次元块条项位人遍份张页折趟番"
# 第二个位置也放进数字字符，覆盖「三千」「一百」「十二个」这类叠写
CN_QUANTITY_RE = re.compile(
    rf"[{_CN_NUM}](?:小时|分钟|星期|个月|[{_CN_UNIT}{_CN_NUM}])"
)

MAX_NOTE_CHARS = 40
MAX_OPENING_CHARS = 60
MAX_CLOSING_CHARS = 40

SYSTEM_PROMPT = """你在给一个中文微信提醒机器人写消息里的「人话」部分。

你只会拿到一份【事实清单】。你要写三样东西：
1. opening：紧跟在系统开场白后面的一句补充，别重复件数与逾期件数（系统已经说了）。
2. notes：给其中几件（不必全部）事项各写一句点评或提醒，口语、简短、像朋友说话。
   如果这件事实在没什么好说的，就不要给它写 notes，留空比硬凑好。
3. closing：一句结尾鼓励，简短，有劲儿。

绝对不能违反的规则，按重要性排序：
- **不要报任何数量、天数或时间**，那些由系统负责写，你只写感受、建议和提醒。
  不许写「还剩 5 天」「拖了三天」「有两件」「第一件」「半小时」这类话；
  可以写「先把它清掉，别再压着」「材料记得带上」。
  （系统会自动拦截带阿拉伯数字或「中文数字+量词」的句子，被拦下就整句作废。）
- **不许引入清单之外的任何事实**：不许提清单里没有的日期、金额、人名、项目名、文件名；
  不许推断进度或替用户做安排、承诺、转达。
- 不要写「已记录」「已提醒」这类机器人自我描述。
- 不要用 Markdown 标记（星号、井号、反引号、短横线列表）——微信会原样显示符号。
- 不要重复编号、时间、事项名称，那些系统已经排好版了。
- 每条 notes 不超过 30 字，opening 不超过 40 字，closing 不超过 25 字。只写一句，不要换行。

只输出一个 JSON 对象，不要任何额外文字：
{"opening": "...", "notes": {"12": "..."}, "closing": "..."}
notes 的键是事项编号（字符串）。不想写的部分就省略或给空字符串。"""


@dataclass
class Flavor:
    opening: str | None = None
    notes: dict[int, str] = field(default_factory=dict)
    closing: str | None = None
    rejected: list[str] = field(default_factory=list)


def check_flourish(text: str | None, *, limit: int) -> str | None:
    """校验模型写的一句「温度」。不合规返回 None，由调用方丢弃。"""
    if not text:
        return None
    cleaned = strip_markdown(str(text)).strip().strip("「」\"' ")
    if not cleaned:
        return None
    if len(cleaned) > limit:
        return None
    if "\n" in cleaned:
        return None
    if ARABIC_NUMERAL_RE.search(cleaned):
        # 出现阿拉伯数字 —— 模型可能在报数量或天数，算错会误导用户，不采用
        return None
    if CN_QUANTITY_RE.search(cleaned):
        # 中文数字带量词（五天、两件、第一件）同样是在报数量，不采用
        return None
    return cleaned


def build_opening(groups: dict[str, list[Any]], generated_at: datetime) -> str:
    """开场白由代码算，因为里面全是事实（件数、逾期件数）。"""
    today = groups.get("today") or []
    overdue = groups.get("overdue") or []
    unscheduled = groups.get("unscheduled") or []
    total = len(today) + len(overdue) + len(unscheduled) + len(groups.get("important") or [])
    if total == 0:
        return "早上好，今天没有待办，安排自己的节奏就行。"

    parts = [f"早上好。今天有 {total} 件事要盯"]
    if overdue:
        parts.append(f"，其中 {len(overdue)} 件已经逾期")
    parts.append("。")
    if unscheduled:
        parts.append(f"另有 {len(unscheduled)} 件没定时间。")
    return "".join(parts)


def _fact_sheet(groups: dict[str, list[Any]], generated_at: datetime) -> str:
    lines = [f"现在是 {format_date_cn(generated_at.date())} {generated_at:%H:%M}。", "事实清单："]
    sections = (("important", "重要工作"), ("today", "今天要做的"), ("overdue", "逾期未完成"), ("unscheduled", "没定时间的"))
    index = 0
    for key, title in sections:
        items = groups.get(key) or []
        if not items:
            continue
        lines.append(f"[{title}]")
        for task in items:
            index += 1
            if task.remind_at is not None:
                when = format_short_datetime(task.remind_at)
                days = overdue_days(task, generated_at) if key == "overdue" else None
                extra = f"（已逾期 {days} 天）" if days else ""
                lines.append(f"  编号{task.id}（显示第{index}件）：{when} {task.content}{extra}")
            else:
                lines.append(f"  编号{task.id}（显示第{index}件）：未设时间 {task.content}")
    lines.append(f"合计 {index} 件。")
    return "\n".join(lines)


def parse_payload(raw: str) -> dict[str, Any]:
    """从模型回复里取出 JSON。取不出来就返回空 dict（等于无温度）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def sanitize(payload: dict[str, Any], valid_task_ids: Sequence[int]) -> Flavor:
    """把模型输出按规则过滤成可用的一层温度。任何不合规的部分单独丢弃。"""
    flavor = Flavor()
    valid = set(valid_task_ids)

    opening = check_flourish(payload.get("opening"), limit=MAX_OPENING_CHARS)
    if opening:
        flavor.opening = opening
    elif payload.get("opening"):
        flavor.rejected.append("开场补充")

    raw_notes = payload.get("notes")
    if isinstance(raw_notes, dict):
        for key, value in raw_notes.items():
            try:
                task_id = int(str(key).strip())
            except (TypeError, ValueError):
                continue
            if task_id not in valid:
                flavor.rejected.append(f"编号{key}不存在")
                continue
            note = check_flourish(value, limit=MAX_NOTE_CHARS)
            if note:
                flavor.notes[task_id] = note
            elif value:
                flavor.rejected.append(f"事项#{task_id}的点评")
    elif raw_notes:
        flavor.rejected.append("notes 结构不对")

    closing = check_flourish(payload.get("closing"), limit=MAX_CLOSING_CHARS)
    if closing:
        flavor.closing = closing
    elif payload.get("closing"):
        flavor.rejected.append("结尾鼓励")

    return flavor


class FlavorComposer:
    """通过注入的 llm 客户端向模型要一层温度。失败一律退回空 Flavor。"""

    def __init__(
        self,
        llm_client: Any,
        enabled: bool = True,
        model: str | None = None,
        max_tokens: int = 2000,
    ) -> None:
        self._llm = llm_client
        self._enabled = enabled
        self._model = model
        self._max_tokens = max_tokens

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def compose(
        self,
        groups: dict[str, list[Any]],
        generated_at: datetime,
        valid_task_ids: Sequence[int],
    ) -> Flavor:
        if not self._enabled:
            return Flavor()
        try:
            payload = await self._llm.complete_json(
                system=SYSTEM_PROMPT,
                user=_fact_sheet(groups, generated_at),
                model=self._model,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            # 这条路径失败是允许的，不该拖垮早报投递，所以只记一行
            log.warning("写温度失败，退回确定性版本：%s: %s", type(exc).__name__, exc)
            return Flavor()
        flavor = sanitize(payload, valid_task_ids)
        if flavor.rejected:
            log.info("丢弃了不合规的模型输出：%s", "、".join(flavor.rejected))
        return flavor
