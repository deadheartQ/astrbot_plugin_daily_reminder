"""提醒条目与时间表达式的解析。

**时间走单独的配置项**（面板里的「提醒时间」），``reminders`` 里只写提醒内容::

    time:      12:30
    reminders: ["提醒我给微软签到", "该睡觉了"]

也兼容在条目里自带时间的写法（老配置、以及"多条各有各的时间"都用它）——
一行按竖线切开后，第一段能解析成时间就按自带时间处理::

    reminders: ["12:30 | 提醒我给微软签到", "21:00 | 该睡了"]

两种格式下，第二段都可以写目标会话，省略时用配置里的默认会话。

时间表达式支持两种写法：

- ``HH:MM`` —— 每天固定时刻（``HHMM`` 紧凑写法也认，全角冒号也认）
- 五段 cron 表达式（``分 时 日 月 周``）

所有解析函数都以「返回 ``None`` 表示看不懂」为约定，绝不抛异常 ——
配置写错不该让插件起不来，也不该让同一份列表里的其它条目一起失效。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# 分隔符同时接受半角与全角竖线。刻意不支持 `/`：cron 表达式里它是有意义的字符。
_SEP_RE = re.compile(r"\s*[|｜]\s*")
_CRON_FIELD_RE = re.compile(r"^[\d*,\-/]+$")
_HHMM_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")
_COMPACT_RE = re.compile(r"^(\d{3,4})$")

MIN_CONTENT_CHARS = 1


def _daily(hour: int, minute: int) -> dict[str, Any] | None:
    """组装一个「每天固定时刻」的时间描述。

    Args:
        hour: 小时，0-23。
        minute: 分钟，0-59。

    Returns:
        时间描述字典；超出取值范围时返回 ``None``。
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    label = f"{hour:02d}:{minute:02d}"
    return {"kind": "daily", "hour": hour, "minute": minute, "label": label, "expr": ""}


def parse_time_spec(raw: Any) -> dict[str, Any] | None:
    """解析时间表达式。

    支持 ``HH:MM``、``HHMM`` 与五段 cron 表达式（含 ``*``、``,``、``-``、``/``）。

    Args:
        raw: 配置里的时间字符串。

    Returns:
        形如 ``{"kind": "daily", "hour": 12, "minute": 30, "label": "12:30"}``
        或 ``{"kind": "cron", "expr": "30 12 * * *", "label": "30 12 * * *"}``；
        解析不出来返回 ``None``。
    """
    text = str(raw or "").strip().replace("：", ":")
    if not text:
        return None

    parts = text.split()
    if len(parts) == 5:
        if all(_CRON_FIELD_RE.match(part) for part in parts):
            expr = " ".join(parts)
            return {"kind": "cron", "expr": expr, "label": expr}
        return None
    if len(parts) != 1:
        return None

    match = _HHMM_RE.match(text)
    if match:
        return _daily(int(match.group(1)), int(match.group(2)))

    match = _COMPACT_RE.match(text)
    if match:
        return _daily(int(text[:-2]), int(text[-2:]))

    return None


def parse_reminder_line(
    raw: Any, default_umo: str = "", preset_time: str = ""
) -> dict[str, Any] | None:
    """解析一行提醒。

    **时间默认走单独配置项**（``preset_time``），条目里只写提醒内容::

        reminders: ["提醒我给微软签到"]        # 面板里单独填 time: 12:30

    同时**兼容**在条目里自带时间的写法（老配置和"多条各有各的时间"都用它）::

        reminders: ["12:30 | 提醒我给微软签到", "21:00 | 该睡了"]

    判定规则：一行按竖线切开后，**第一段能解析成时间**就按"自带时间"处理，
    否则整行当内容、时间取 ``preset_time``。
    第二段的位置在两种格式下都是"目标会话"，省略时用 ``default_umo``。

    Args:
        raw: 一行配置。
        default_umo: 没写目标会话时使用的默认会话。
        preset_time: 单独配置的时间表达式。

    Returns:
        解析结果字典（在时间描述的基础上补 ``content`` / ``umo`` / ``raw``）；
        时间或内容缺失时返回 ``None``。
    """
    text = str(raw or "").strip()
    if not text or text.startswith("#"):
        return None

    fields = [field.strip() for field in _SEP_RE.split(text)]

    inline = parse_time_spec(fields[0]) if len(fields) > 1 else None
    if inline is not None:
        spec: dict[str, Any] | None = inline
        content = fields[1].strip()
        rest = fields[2:]
    else:
        spec = parse_time_spec(preset_time)
        content = fields[0].strip()
        rest = fields[1:]

    if spec is None or len(content) < MIN_CONTENT_CHARS:
        return None

    umo = str(default_umo or "").strip()
    for extra in rest:
        if extra:
            umo = extra
            break

    spec = dict(spec)
    spec.update({"content": content, "umo": umo, "raw": text})
    return spec


def parse_reminders(
    items: Any, default_umo: str = "", preset_time: str = ""
) -> list[dict[str, Any]]:
    """把面板里的列表配置解析成提醒列表。

    容忍两种填法：真正的 list，以及用户整段粘进文本框的字符串（此时按行切）。
    单行坏掉只跳过该行，其余照常生效。

    Args:
        items: 配置值。
        default_umo: 默认目标会话。
        preset_time: 单独配置的时间表达式，给不带时间的条目用。

    Returns:
        提醒列表。
    """
    if isinstance(items, str):
        candidates: list[Any] = [line for line in items.splitlines() if line.strip()]
    elif isinstance(items, (list, tuple)):
        candidates = list(items)
    else:
        return []

    parsed: list[dict[str, Any]] = []
    for item in candidates:
        one = parse_reminder_line(item, default_umo, preset_time)
        if one is not None:
            parsed.append(one)
    return parsed


def reminder_key(item: dict[str, Any]) -> str:
    """给一条提醒算一个稳定的 key，用于「今天发过没有」的幂等判断。

    Args:
        item: 解析后的提醒。

    Returns:
        稳定的字符串 key。
    """
    return f"{item.get('kind')}:{item.get('label')}:{item.get('umo')}:{item.get('content')}"


def date_stamp(now: datetime | None = None) -> str:
    """返回 ``YYYY-MM-DD`` 形式的当日标记。

    Args:
        now: 用于测试注入的时间。

    Returns:
        日期字符串。
    """
    moment = now or datetime.now()
    return moment.strftime("%Y-%m-%d")


def build_trigger(spec: dict[str, Any], tz: Any = None) -> Any:
    """按解析结果构造 APScheduler 触发器。

    APScheduler 在这里是延迟导入的：自测环境没有这个库时，与定时无关的解析逻辑
    仍然可测。

    Args:
        spec: ``parse_time_spec`` 的返回值。
        tz: tzinfo；``None`` 表示跟随运行环境。

    Returns:
        ``CronTrigger`` 实例。

    Raises:
        ValueError: ``spec`` 不合法。
        ImportError: 环境里没有 APScheduler。
    """
    from apscheduler.triggers.cron import CronTrigger

    if spec.get("kind") == "cron":
        expr = str(spec.get("expr") or "").strip()
        if not expr:
            raise ValueError("cron 表达式为空")
        # APScheduler 把数字星期当「周一=0」，而标准 crontab 是「周日=0」。
        # WebUI 与内置 cron 用的是后者，这里也不做数字星期的转换，
        # 因此文档里只推荐用名字（mon/tue/...）或不带星期的表达式。
        return CronTrigger.from_crontab(expr, timezone=tz)

    if spec.get("kind") == "daily":
        return CronTrigger(
            hour=int(spec["hour"]),
            minute=int(spec["minute"]),
            timezone=tz,
        )

    raise ValueError(f"不支持的时间描述：{spec!r}")
