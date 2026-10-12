"""把提醒内容变成一句人话，并保证它不带机器味数据。

这一层是整个插件存在的理由。AstrBot 内置的「未来任务」会把机器上下文一路带进
模型的上下文和对话历史：system prompt 里注入 ``cron_job`` JSON（含会话标识）、
user 轮要求「总结本次行动与结果」、收尾再把
``[CronJob] 任务名: … triggered at <UTC 时间>, I finished this job, here is the result: …``
当作一条对话写进历史。机器人之后复述这件事时，就会把会话 ID、任务名、时间戳
一起念出来。

本插件有两条防线：

1. **结构上**：消息由插件直接发送，不经过主 Agent，也不写对话历史 ——
   上面那些字符串根本没有出现的机会。
2. **输出前**：无论文案来自模型还是模板，都先过 :func:`sanitize`。
   即使模型不听话、把元数据吐了出来，也会在落库前被剔除。

第 2 条是硬保证：它不依赖模型是否遵守提示词。
"""

from __future__ import annotations

import re

# 会话标识，形如 default:FriendMessage:1234567890
UMO_RE = re.compile(
    r"\b[A-Za-z0-9_.\-]{1,32}:(?:Friend|Group|Other|Guild|Private)Message:\d+",
    re.IGNORECASE,
)
# 日期或 ISO 时间戳（含 UTC 偏移、小数秒）
ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
# [CronJob] / 【CronJob】 / (cron job) / CronJob
CRON_JOB_RE = re.compile(r"[\[【（(]?\s*cron\s*[-_ ]?job\s*[\]】）)]?", re.IGNORECASE)
# 「任务：」「触发时间：」「状态：」这类汇报标签（只认行首）
LABEL_RE = re.compile(
    r"^[\s\-*•·>#`]*\**[^\n]{0,12}?"
    r"(任务名|任务|计划|触发时间|执行时间|运行时间|结果|状态|会话|目标会话|"
    r"接收方|发送方|Job|Status)\**[^\n]{0,6}?[:：]"
)
# 「已向…发送提醒」「Message sent to session …」这类自述式汇报
SELF_REPORT_RE = re.compile(
    r"(已|经|成功)?(向|给).{0,20}?(发送|推送|送达|发出)"
    r"|Message sent to session"
    r"|消息已发送"
    r"|执行完毕"
)
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
THINK_RE = re.compile(r"<(think|thinking|reasoning|analysis)>[\s\S]*?</\1>", re.IGNORECASE)
MARKUP_RE = re.compile(r"(\*\*|__|~~|`)")
HEADING_RE = re.compile(r"^\s*#{1,6}\s*")
BULLET_RE = re.compile(r"^\s*[-*•·]\s+")
SPACE_RE = re.compile(r"[ \t]{2,}")

MIN_OUTPUT_CHARS = 2

DEFAULT_FALLBACK = "该提醒你一下：{content}"

# 提示词里明确禁止输出内部标识。注意这只是第一道防线，
# 真正的保证在 sanitize() —— 提示词是"请"，清洗器是"必须"。
_OUTPUT_RULES = """输出要求（必须全部做到）：
1. 只写一到三句话，像平时私聊那样自然，不要分段成清单，不要用项目符号。
2. 绝对不要出现任何内部标识：会话 ID、任务 ID、时间戳、日期、方括号里的英文标签。
3. 不要出现「任务」「触发」「状态」「已完成」「结果」这类汇报用词。
4. 不要提你在执行定时任务或系统指令，就当是自己忽然想起来了。
5. 直接输出要发给他的那句话本身，不要加引号，不要解释你在做什么。
6. 不要说客套话，不要重复我给你的提醒内容里的原话，用你自己的说法。"""


def build_system_prompt(persona_hint: str = "") -> str:
    """组装生成文案用的 system prompt。

    Args:
        persona_hint: 人格/口吻提示（面板可配）。

    Returns:
        system prompt 文本。
    """
    hint = str(persona_hint or "").strip()
    head = "你正在给用户发一条主动消息。" if not hint else f"你正在给用户发一条主动消息。{hint}"
    return f"{head}\n\n{_OUTPUT_RULES}"


def build_prompt(content: str) -> str:
    """组装生成文案用的 user prompt。

    这条 prompt 里**不含**会话标识、任务名、时间戳 —— 只给"要提醒什么"。

    Args:
        content: 面板里配的提醒内容。

    Returns:
        user prompt 文本。
    """
    return f"要提醒他的事情是：{str(content or '').strip()}\n\n现在把这件事用你自己的话说一遍。"


def render_fallback(content: str, template: str = "") -> str:
    """用固定模板渲染降级文案。

    Args:
        content: 提醒内容。
        template: 面板里配的模板，支持 ``{content}`` 占位符。

    Returns:
        渲染后的文本；模板为空时用内置默认模板。
    """
    text = str(content or "").strip()
    tpl = str(template or "").strip() or DEFAULT_FALLBACK
    if "{content}" in tpl:
        return tpl.replace("{content}", text)
    return f"{tpl}{text}" if tpl else text


def _strip_line(line: str) -> str:
    """去掉行首的标题/项目符号与行内 markdown 强调标记。

    Args:
        line: 原始行。

    Returns:
        处理后的行。
    """
    text = HEADING_RE.sub("", line.strip())
    text = BULLET_RE.sub("", text)
    return MARKUP_RE.sub("", text).strip()


def _is_meta_line(line: str) -> bool:
    """判断一行是不是「机器播报」行。

    只要出现会话标识、日期时间戳、``CronJob`` 字样或自述式汇报句，
    就整行丢掉 —— 这些 token 不会正当地出现在一句贴心提醒里。

    Args:
        line: 待判断的行。

    Returns:
        是脏行返回 ``True``。
    """
    if UMO_RE.search(line) or CRON_JOB_RE.search(line):
        return True
    if ISO_RE.search(line) or SELF_REPORT_RE.search(line):
        return True
    return False


def sanitize(raw: str, *, fallback: str = "", max_chars: int = 200) -> str:
    """把一段模型输出洗成"可以发给人看"的文本。

    处理顺序：

    1. 去掉代码块与思考标签段
    2. 逐行丢掉含内部标识/时间戳/``CronJob``/自述汇报的行
    3. 出现两个及以上「任务：/结果：」标签行时，判定为汇报体
    4. **只要第 2、3 步丢掉过任何东西，整段输出作废**，直接回退到 ``fallback``
    5. 否则去掉标题与项目符号、markdown 强调标记
    6. 洗空或过短时同样回退到 ``fallback``
    7. 按 ``max_chars`` 截断（优先切在句末）

    Args:
        raw: 模型原始输出。
        fallback: 洗不出内容时使用的兜底文案。
        max_chars: 输出长度上限（字符数）。

    Returns:
        一定非空的成品文本。
    """
    text = str(raw or "")
    text = CODE_FENCE_RE.sub(" ", text)
    text = THINK_RE.sub(" ", text)
    text = text.replace("\r", "")

    raw_lines = [line.strip() for line in text.split("\n")]
    labeled = [line for line in raw_lines if line and LABEL_RE.match(_strip_line(line))]
    drop_labels = len(labeled) >= 2

    # 只要出现一处机器标识，就判定整段输出都不可信，整体作废。
    # 理由：一段话里混进了会话 ID 或时间戳，说明模型是在"播报任务"而不是在
    # 跟人说话；这种情况下从里面抠出剩下的半句，发出去只会更怪。
    # 作废后由调用方回退到固定文案 —— 那个模板一定是干净的。
    saw_meta = False
    kept: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        cleaned = _strip_line(line)
        if not cleaned:
            continue
        if _is_meta_line(cleaned):
            saw_meta = True
            continue
        if LABEL_RE.match(cleaned):
            # 只有一条标签行时，去掉标签保留内容；多条时整块判定为汇报体
            if drop_labels:
                saw_meta = True
                continue
            cleaned = LABEL_RE.sub("", cleaned).strip()
            if not cleaned:
                continue
            cleaned = _strip_line(cleaned)
        if cleaned and (not kept or kept[-1] != cleaned):
            kept.append(cleaned)

    if saw_meta:
        kept = []
    result = SPACE_RE.sub(" ", "\n".join(kept)).strip()
    if len(result) < MIN_OUTPUT_CHARS:
        result = SPACE_RE.sub(" ", str(fallback or "")).strip()
    if len(result) < MIN_OUTPUT_CHARS:
        return ""

    limit = int(max_chars or 0)
    if limit > 0 and len(result) > limit:
        window = result[:limit]
        for mark in ("。", "！", "？", "~", "…", ".", "!", "?"):
            cut = window.rfind(mark)
            if cut >= max(MIN_OUTPUT_CHARS, limit // 3):
                window = window[: cut + 1]
                break
        result = window.strip()

    return result


def describe_spec(spec: dict) -> str:
    """把解析结果渲染成给人看的说明（用在状态指令里）。

    Args:
        spec: ``parse_time_spec`` / ``parse_reminder_line`` 的返回值。

    Returns:
        单行说明。
    """
    if spec.get("kind") == "cron":
        when = f"cron {spec.get('expr')}"
    else:
        when = f"每天 {spec.get('label')}"
    return f"{when} → {spec.get('content')}"
