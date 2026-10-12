"""定时提醒插件：按点给指定会话发一条主动消息。

设计要点（和 AstrBot 内置「未来任务」的区别）:

- **直发消息**：用 ``context.send_message`` 投递，不唤醒主 Agent。
  因此不会产生 ``send_message_to_user`` 工具调用、不会把
  ``Message sent to session <umo>`` 塞进上下文，也不会往对话历史里写
  ``[CronJob] … triggered at …`` 这种任务播报式的伪对话。
- **输出前强制清洗**：不管文案来自模型还是模板，都先过
  :func:`core.compose.sanitize`，把会话标识、时间戳、``CronJob``、
  任务/状态清单全部剔除。这是硬保证，不依赖模型是否听话。
- **模型挂了也要响**：``context.llm_generate`` 没有任何 fallback，
  这里自己按 配置的模型 → 当前会话模型 → 备用列表 依次重试，
  全失败就用固定文案。提醒不会因为上游故障而消失。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

try:  # 正常按包加载时走相对导入
    from .core.compose import (
        build_prompt,
        build_system_prompt,
        describe_spec,
        render_fallback,
        sanitize,
    )
    from .core.schedule import (
        build_trigger,
        parse_reminders,
        parse_time_spec,
        reminder_key,
    )
    from .core.store import SentLog
except ImportError:  # pragma: no cover - 少数加载方式下插件目录不在包路径上
    from core.compose import (
        build_prompt,
        build_system_prompt,
        describe_spec,
        render_fallback,
        sanitize,
    )
    from core.schedule import (
        build_trigger,
        parse_reminders,
        parse_time_spec,
        reminder_key,
    )
    from core.store import SentLog

PLUGIN_NAME = "astrbot_plugin_daily_reminder"
PLUGIN_VERSION = "1.1.0"
JOB_PREFIX = "daily_reminder:"
STATE_FILE = "sent.json"
PRUNE_KEEP = 200
"""发送记录里最多保留多少条历史时间戳，避免文件无限增长。"""

HELP_TEXT = """定时提醒

remind              查看已启用的提醒与下次触发时间
remind test <序号>   立即试发第几条（试发不写发送记录，正式那次照常触发）

提醒条目在插件配置里维护，格式：时间 | 提醒内容 | 目标会话。
时间写 HH:MM 表示每天一次，也可以写五段 cron 表达式（分 时 日 月 周）。"""


def _resolve_data_dir() -> Path:
    """拿到插件的数据目录。

    自测里会把这个函数替换掉，避免不同用例共用同一个状态文件。

    Returns:
        数据目录路径。
    """
    for args in ((PLUGIN_NAME,), ()):
        try:
            return Path(StarTools.get_data_dir(*args))
        except Exception:  # noqa: BLE001 - 换下一种取法
            continue
    return Path(__file__).resolve().parent / "data"


def _resolve_timezone(name: str) -> Any:
    """把时区名解析成 tzinfo。

    Args:
        name: 时区名，如 ``Asia/Shanghai``。

    Returns:
        tzinfo；解析不了返回 ``None``（表示跟随运行环境）。
    """
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001 - 时区数据缺失时降级
        logger.warning("[定时提醒] 时区 %s 不可用（%s），改用运行环境本地时区", text, exc)
        return None


def _format_dt(moment: Any, tz: Any = None) -> str:
    """格式化一个时间点。

    Args:
        moment: datetime。
        tz: 目标时区；``None`` 表示转成本地时区。

    Returns:
        ``YYYY-MM-DD HH:MM`` 形式的字符串；解析不了返回 ``?``。
    """
    try:
        if moment.tzinfo is not None:
            moment = moment.astimezone(tz) if tz is not None else moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001 - 展示用，失败不抛
        return "?"


def _mask_umo(umo: str) -> str:
    """把会话标识打码后再展示，避免它被复述进聊天记录。

    这里展示的是内部标识，而指令回复很可能也会进对话历史；一旦进了，
    机器人之后就有可能把它念出来 —— 那正是本插件要避免的现象。

    Args:
        umo: 会话标识。

    Returns:
        形如 ``default:FriendMessage:***146`` 的字符串。
    """
    text = str(umo or "")
    if not text:
        return "未设置"
    head, sep, tail = text.rpartition(":")
    if not sep:
        return "***"
    shown = tail[-3:] if len(tail) > 3 else tail
    return f"{head}:***{shown}"


@register(PLUGIN_NAME, "Buddy", "定时提醒：按点给指定会话发一条主动消息", PLUGIN_VERSION)
class DailyReminderPlugin(Star):
    """定时提醒插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 插件上下文。
            config: 面板配置，来自 ``_conf_schema.json``。
        """
        super().__init__(context)
        self.config: dict[str, Any] = config if isinstance(config, dict) else {}
        self.scheduler: Any = None
        self._job_lock = asyncio.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._log = SentLog(Path(_resolve_data_dir()) / STATE_FILE, logger)

    # ----------------------------------------------------------------- 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取配置项，兼容扁平与分组（object）两种 schema 写法。

        Args:
            key: 配置键名。
            default: 缺省值。

        Returns:
            配置值。
        """
        config = self.config
        if not isinstance(config, dict):
            return default
        if key in config:
            return config[key]
        for value in config.values():
            if isinstance(value, dict) and key in value:
                return value[key]
        return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        """读取字符串配置。"""
        value = self._cfg(key, default)
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _cfg_int(self, key: str, default: int = 0) -> int:
        """读取整数配置，容忍面板里被填成字符串。"""
        value = self._cfg(key, default)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        """读取布尔配置，容忍 true/false、1/0、开/关 等写法。"""
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in ("true", "1", "yes", "y", "on", "是", "开", "开启"):
            return True
        if text in ("false", "0", "no", "n", "off", "否", "关", "关闭"):
            return False
        return default

    def _cfg_list(self, key: str) -> list[str]:
        """读取列表配置，字符串会被按行切开。"""
        value = self._cfg(key, [])
        if isinstance(value, str):
            return [line for line in value.splitlines() if line.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return []

    def _settings(self) -> dict[str, Any]:
        """一次性读出全部配置，便于多点共用同一份快照。"""
        return {
            "enable": self._cfg_bool("enable", True),
            "time": self._cfg_str("time", "12:30"),
            "default_umo": self._cfg_str("default_umo", ""),
            "use_llm": self._cfg_bool("use_llm", True),
            "persona_prompt": self._cfg_str("persona_prompt", ""),
            "llm_provider_id": self._cfg_str("llm_provider_id", ""),
            "llm_fallback_provider_ids": self._cfg_list("llm_fallback_provider_ids"),
            "fallback_text": self._cfg_str("fallback_text", ""),
            "max_chars": self._cfg_int("max_chars", 200),
            "timezone": self._cfg_str("timezone", "Asia/Shanghai"),
            "misfire_grace_seconds": self._cfg_int("misfire_grace_seconds", 300),
            "verbose_log": self._cfg_bool("verbose_log", False),
        }

    def _reminders(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        """解析面板里的提醒条目（时间为单独配置项，条目里可自带时间覆盖）。"""
        return parse_reminders(
            self._cfg_list("reminders"), settings["default_umo"], settings["time"]
        )

    # ----------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        """加载提醒条目并按配置注册定时任务。"""
        # 同一个实例可能被 initialize 多次（框架热重载 / 插件重载）：
        # 先收掉上一次的调度器并清空条目表，否则旧 job 会继续跑，
        # 同一条提醒就有发两遍的风险。实测线上一次启动窗口里被调了 3 次。
        await self.terminate()
        self._jobs.clear()

        settings = self._settings()
        reminders = self._reminders(settings)
        logger.info(
            "[定时提醒] 插件已加载：启用=%s，提醒时间=%s，识别到 %d 条提醒，文案=%s",
            settings["enable"],
            settings["time"] or "未设置",
            len(reminders),
            "模型生成" if settings["use_llm"] else "固定模板",
        )
        # 时间是个单独的配置项，写坏了会让"没自带时间的条目"整批消失 ——
        # 这种静默失效必须在日志里说清楚，否则只会表现为"插件没反应"。
        if parse_time_spec(settings["time"]) is None:
            logger.warning(
                "[定时提醒] 提醒时间 %r 无法解析（要写 HH:MM 或五段 cron 表达式），"
                "没自带时间的条目会被跳过",
                settings["time"],
            )
        raw_items = [str(item) for item in self._cfg_list("reminders") if str(item).strip()]
        usable_raw = [item for item in raw_items if not item.lstrip().startswith("#")]
        if len(reminders) < len(usable_raw):
            logger.warning(
                "[定时提醒] 有 %d 条配置没能解析出来（检查时间、竖线分段与目标会话），已跳过",
                len(usable_raw) - len(reminders),
            )
        for item in reminders:
            logger.info(
                "[定时提醒]   条目 %s（→ %s）",
                describe_spec(item),
                _mask_umo(item.get("umo", "")),
            )

        usable = [item for item in reminders if item.get("umo")]
        for item in reminders:
            if item.get("umo"):
                self._jobs[reminder_key(item)] = item
            else:
                logger.warning(
                    "[定时提醒] 条目「%s」没有目标会话（default_umo 也没填），跳过",
                    item.get("content"),
                )

        if not settings["enable"]:
            logger.info("[定时提醒] 已在配置里关闭，不注册定时任务（remind test 仍可用）")
            return
        if not usable:
            logger.warning("[定时提醒] 没有可用的提醒条目，定时任务未注册")
            return

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            logger.error("[定时提醒] 缺少 APScheduler，定时任务未启用：%s", exc)
            return

        tz = _resolve_timezone(settings["timezone"])
        grace = max(1, int(settings["misfire_grace_seconds"] or 1))
        try:
            scheduler = AsyncIOScheduler(timezone=tz)
        except Exception as exc:  # noqa: BLE001 - 建调度器失败不该挡住插件加载
            logger.error("[定时提醒] 创建调度器失败：%s", exc)
            return

        registered = 0
        pending: list[tuple[dict[str, Any], Any]] = []
        for item in usable:
            key = reminder_key(item)
            try:
                trigger = build_trigger(item, tz)
                job = scheduler.add_job(
                    self._fire,
                    trigger,
                    args=[key],
                    id=JOB_PREFIX + key,
                    replace_existing=True,
                    misfire_grace_time=grace,
                )
            except Exception as exc:  # noqa: BLE001 - 单条坏掉不影响其它条目
                logger.warning(
                    "[定时提醒] 条目「%s」时间表达式不可用（%s），跳过", item.get("raw"), exc
                )
                continue
            registered += 1
            pending.append((item, job))
            logger.info("[定时提醒] 已注册 %s", describe_spec(item))

        if not registered:
            logger.warning("[定时提醒] 没有成功注册任何提醒")
            return

        try:
            scheduler.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败不能拖垮 AstrBot
            logger.error("[定时提醒] 启动调度器失败：%s", exc)
            return
        self.scheduler = scheduler
        self._log.prune(PRUNE_KEEP)
        # 下次触发时间要在 start() 之后读：在此之前 APScheduler 还没算出 next_run_time，
        # 直接读会打出一串「下次 ?」，把日志变成误导（踩过一次）。
        for item, job in pending:
            logger.info(
                "[定时提醒]   %s 下次触发：%s",
                describe_spec(item),
                _format_dt(getattr(job, "next_run_time", None), tz),
            )
        logger.info("[定时提醒] 定时任务已启动，共 %d 条", registered)

    async def terminate(self) -> None:
        """停止定时任务。

        注意 ``AsyncIOScheduler.shutdown()`` 被 ``@run_in_event_loop`` 包了一层：
        它只是把真正的关闭动作投递到事件循环就返回，**调用后立刻读 ``running``
        仍然是 True**。所以这里不去断言关闭结果，只把引用交出去。
        """
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001 - 关闭失败无需上报
                logger.debug("[定时提醒] 关闭定时任务失败：%s", exc)
            self.scheduler = None

    # --------------------------------------------------------------- 发送

    async def _provider_candidates(self, umo: str, settings: dict[str, Any]) -> list[str]:
        """列出可以尝试的模型 ID，按优先级去重排序。

        ``context.llm_generate`` 只认一个 provider、且没有任何 fallback，
        所以这里手动排一条链：配置的模型 → 当前会话模型 → 备用列表。

        ``get_current_chat_provider_id`` 在 v4.28 是协程（内部走
        ``get_using_provider_async``），但历史上存在同步写法，所以两种都兼容。

        Args:
            umo: 目标会话。
            settings: 配置快照。

        Returns:
            provider id 列表，可能为空。
        """
        ids: list[str] = []
        configured = str(settings["llm_provider_id"] or "").strip()
        if configured:
            ids.append(configured)
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                current = getter(umo)
                if inspect.isawaitable(current):
                    current = await current
            except Exception as exc:  # noqa: BLE001 - 拿不到就跳过一个候选
                logger.debug("[定时提醒] 取当前模型失败：%s", exc)
                current = ""
            if current:
                ids.append(str(current))
        ids.extend(str(item).strip() for item in settings["llm_fallback_provider_ids"])

        seen: set[str] = set()
        ordered: list[str] = []
        for pid in ids:
            if pid and pid not in seen:
                seen.add(pid)
                ordered.append(pid)
        return ordered

    async def _generate(self, item: dict[str, Any], settings: dict[str, Any]) -> str:
        """尝试用模型生成一句自然文案，失败返回空串。

        Args:
            item: 提醒条目。
            settings: 配置快照。

        Returns:
            模型输出的原始文本；一个模型都没成功时返回空串。
        """
        gen = getattr(self.context, "llm_generate", None)
        if not callable(gen):
            logger.warning("[定时提醒] 当前 AstrBot 版本没有 llm_generate，改用固定文案")
            return ""

        system_prompt = build_system_prompt(settings["persona_prompt"])
        prompt = build_prompt(item.get("content", ""))
        candidates = await self._provider_candidates(item.get("umo", ""), settings)
        if not candidates:
            logger.warning("[定时提醒] 没有任何可用的模型，改用固定文案")
            return ""

        for pid in candidates:
            try:
                resp = await gen(chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt)
            except Exception as exc:  # noqa: BLE001 - 换下一个候选
                logger.warning("[定时提醒] 模型 %s 生成失败：%s", pid, exc)
                continue
            # 只认 LLMResponse.completion_text；拿不到且整体就是字符串时才退而用
            # resp 本身 —— 否则会把对象的 repr 当文案发出去（自测抓到过一次）。
            text = getattr(resp, "completion_text", None)
            if text is None and isinstance(resp, str):
                text = resp
            if str(text or "").strip():
                return str(text)
            logger.warning("[定时提醒] 模型 %s 返回了空文本", pid)
        return ""

    async def _compose(self, item: dict[str, Any], settings: dict[str, Any]) -> str:
        """拿到最终要发出去的文案。

        Args:
            item: 提醒条目。
            settings: 配置快照。

        Returns:
            清洗过、保证非空的文案。
        """
        fallback = render_fallback(item.get("content", ""), settings["fallback_text"])
        raw = ""
        if settings["use_llm"]:
            raw = await self._generate(item, settings)
        text = sanitize(raw, fallback=fallback, max_chars=settings["max_chars"])
        if not text:
            text = str(item.get("content") or "").strip()
        if settings["verbose_log"]:
            logger.info("[定时提醒] 原始文案：%r", raw[:400])
            logger.info("[定时提醒] 清洗后：%r", text)
        return text

    async def _send(self, umo: str, text: str) -> bool:
        """直接把消息投递到目标会话。

        刻意不用主 Agent：这是"不产生机器味历史"的结构性前提。

        Args:
            umo: 目标会话。
            text: 消息正文。

        Returns:
            是否发送成功。
        """
        if not umo or not text:
            return False
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001 - 发送失败要落到日志里
            logger.warning("[定时提醒] 向 %s 发送失败：%s", _mask_umo(umo), exc)
            return False
        return True

    async def _fire(self, key: str, *, manual: bool = False) -> str:
        """一条提醒的触发入口。

        Args:
            key: 提醒 key。
            manual: 是否由 ``remind test`` 手动触发；手动触发不写发送记录，
                也跳过幂等判断，方便反复调试。

        Returns:
            实际发出的文案；跳过或失败时返回空串。
        """
        item = self._jobs.get(key)
        if item is None:
            logger.warning("[定时提醒] 找不到提醒 %s，跳过", key)
            return ""

        if self._job_lock.locked():
            logger.warning("[定时提醒] 上一轮还没结束，本轮跳过")
            return ""

        async with self._job_lock:
            settings = self._settings()
            tz = _resolve_timezone(settings["timezone"])
            # 幂等键精确到分钟：同一次触发不会因为重启或补跑而发两遍，
            # 而「每 6 小时一次」这种 cron 的两次触发也不会互相挡住。
            moment = datetime.now(tz) if tz is not None else datetime.now()
            stamp = moment.strftime("%Y-%m-%d %H:%M")
            if not manual and self._log.get(key) == stamp:
                logger.info("[定时提醒] 这一时刻已经发过「%s」，跳过", item.get("content"))
                return ""

            text = await self._compose(item, settings)
            if not text:
                logger.warning("[定时提醒] 条目「%s」没有可用文案，本轮不发", item.get("content"))
                return ""

            ok = await self._send(str(item.get("umo") or ""), text)
            if ok:
                logger.info(
                    "[定时提醒] 已发送 %s（%s）", describe_spec(item), _mask_umo(item["umo"])
                )
                if not manual:
                    self._log.mark(key, stamp)
            return text if ok else ""

    # --------------------------------------------------------------- 指令

    def _status_text(self) -> str:
        """渲染状态文本，供 ``remind`` 指令使用。"""
        settings = self._settings()
        lines = [
            f"定时提醒：{'已启用' if settings['enable'] else '已关闭'}",
            f"文案来源：{'模型生成，失败回退固定文案' if settings['use_llm'] else '固定文案'}",
            f"时区：{settings['timezone'] or '运行环境本地时区'}",
        ]
        items = list(self._jobs.items())
        if not items:
            lines.append("没有可用的提醒条目（检查 reminders 与 default_umo 配置）")
            lines.append("")
            lines.append(HELP_TEXT)
            return "\n".join(lines)

        lines.append(f"已加载 {len(items)} 条：")
        for index, (key, item) in enumerate(items, 1):
            next_text = ""
            if self.scheduler is not None:
                job = self.scheduler.get_job(JOB_PREFIX + key)
                if job is not None and getattr(job, "next_run_time", None):
                    next_text = f"，下次 {_format_dt(job.next_run_time)}"
            lines.append(f"{index}. {describe_spec(item)} → {_mask_umo(item['umo'])}{next_text}")
        if self.scheduler is None:
            lines.append("（定时任务未注册：请检查 enable 开关与日志）")
        lines.append("")
        lines.append("remind test <序号> 可立即试发一条。")
        return "\n".join(lines)

    @filter.command("remind")
    async def cmd_remind(self, event: AstrMessageEvent):
        """查看定时提醒状态，或试发一条。"""
        parts = str(event.message_str or "").strip().split()
        args = [part for part in parts[1:] if part]
        if not args:
            yield event.plain_result(self._status_text())
            return

        if args[0].lower() not in ("test", "测试", "try"):
            yield event.plain_result(f"看不懂「{args[0]}」。\n\n{HELP_TEXT}")
            return

        items = list(self._jobs.items())
        if not items:
            yield event.plain_result("没有可用的提醒条目，先在插件配置里填 reminders。")
            return

        index = 0
        target = None
        if len(args) >= 2:
            try:
                index = int(args[1])
            except (TypeError, ValueError):
                index = 0
            if 1 <= index <= len(items):
                target = items[index - 1]
        if target is None:
            listing = "\n".join(
                f"{i}. {describe_spec(item)}" for i, (_k, item) in enumerate(items, 1)
            )
            yield event.plain_result(f"请给出序号（1 到 {len(items)}）：\n{listing}")
            return

        key, item = target
        text = await self._fire(key, manual=True)
        if not text:
            yield event.plain_result(f"试发「{item.get('content')}」没有成功，请查看日志。")
            return
        yield event.plain_result(f"已试发：\n{text}\n\n（试发不写发送记录，正式那次照常触发）")
