"""astrbot_plugin_mute

让机器人闭嘴一段时间的插件。

- ``/mute 10`` 闭嘴 10 秒，不带秒数时用配置里的默认时长
- ``/unmute`` 提前解除
- ``/mute_status`` 查看还剩多少秒
- 私聊或被 @ 时说「别说话」「闭嘴」也会触发闭嘴
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 兜底关键词：配置读不到时用这些
DEFAULT_MUTE_KEYWORDS = ("别说话", "闭嘴", "安静点", "别吵", "shut up", "shutup")
DEFAULT_UNMUTE_KEYWORDS = (
    "可以说话了",
    "说话吧",
    "解除闭嘴",
    "别闭嘴了",
    "开口吧",
    "unmute",
)
# 闭嘴期间仍然放行的指令前缀，方便提前解除
COMMAND_PREFIXES = ("/", "／", "!", "！", "#")
# 宿主会把 wake_prefix 从 message_str 上剥掉，所以自己的指令名也要认
OWN_COMMAND_NAMES = ("mute_status", "unmute", "mute", "闭嘴状态", "解除闭嘴")
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
SPLIT_PATTERN = re.compile(r"[,，、\s]+")


class MutePlugin(Star):
    """让我闭嘴一段时间的插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        # 会话（unified_msg_origin）-> 恢复说话的时间戳
        self._muted_until: dict[str, float] = {}

    # ------------------------------------------------------------------ 配置
    def _get_int(self, key: str, default: int, low: int, high: int) -> int:
        """读整数配置，坏值回落到默认值，并夹在 [low, high] 之间。"""
        value = self.config.get(key, default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning(
                "mute: 配置 %s 不是整数(%r)，改用默认值 %s",
                key,
                value,
                default,
            )
            number = default
        if high < low:
            high = low
        return max(low, min(high, number))

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return default

    def _get_list(self, key: str, default: tuple[str, ...]) -> list[str]:
        value = self.config.get(key, list(default))
        if isinstance(value, str):
            value = SPLIT_PATTERN.split(value)
        if not isinstance(value, (list, tuple)):
            return list(default)
        items = [str(item).strip().lower() for item in value]
        items = [item for item in items if item]
        return items or list(default)

    def _get_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if not isinstance(value, str) or not value.strip():
            return default
        return value

    def _max_seconds(self) -> int:
        return self._get_int("max_seconds", 3600, 1, 86400 * 7)

    # ------------------------------------------------------------------ 状态
    def _remaining(self, umo: str) -> int:
        """还要闭嘴多少秒，返回 0 表示没在闭嘴。"""
        until = self._muted_until.get(umo)
        if until is None:
            return 0
        left = until - time.time()
        if left <= 0:
            self._muted_until.pop(umo, None)
            return 0
        return int(math.ceil(left))

    def _start_mute(self, umo: str, seconds: int) -> int:
        seconds = max(1, min(self._max_seconds(), int(seconds)))
        self._muted_until[umo] = time.time() + seconds
        return seconds

    def _stop_mute(self, umo: str) -> None:
        self._muted_until.pop(umo, None)

    @staticmethod
    def _hit_keyword(text: str, keywords: list[str]) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _silence(event: AstrMessageEvent) -> None:
        """闭嘴期间既不发消息，也不把消息喂给模型。"""
        event.stop_event()

    def _ack(self, key: str, default: str, seconds: int | None = None) -> str:
        template = self._get_str(key, default)
        if seconds is not None:
            template = template.replace("{seconds}", str(seconds))
        return template

    def _parse_seconds(self, text: str) -> int | None:
        """从 /mute 后面的内容里抠出秒数，没有数字就返回 None。"""
        cleaned = text
        for token in ("mute_status", "mute", "闭嘴状态", "别说话", "闭嘴"):
            cleaned = cleaned.replace(token, " ")
        match = NUMBER_PATTERN.search(cleaned)
        if not match:
            return None
        try:
            return int(float(match.group(0)))
        except ValueError:
            return None

    def _keyword_allowed(self, event: AstrMessageEvent) -> bool:
        """群聊里默认只认被 @ / 喊到时说的关键词，防止被人随手关掉。"""
        if event.is_private_chat():
            return True
        if not self._get_bool("keyword_only_when_addressed", True):
            return True
        return bool(event.is_at_or_wake_command)

    def _wake_prefixes(self) -> list[str]:
        """取宿主的唤醒前缀，空串不用（空串会让所有消息都像指令）。"""
        try:
            config = self.context.get_config()
            prefixes = config.get("wake_prefix", []) or []
        except Exception:
            logger.debug("mute: 读取 wake_prefix 失败，按没有前缀处理", exc_info=True)
            return []
        if isinstance(prefixes, str):
            return [prefixes] if prefixes else []
        return [str(item) for item in prefixes if str(item)]

    def _looks_like_command(self, event: AstrMessageEvent) -> bool:
        """闭嘴期间放行指令，别把人锁死在里面。"""
        candidates = [event.get_message_str() or ""]
        raw = getattr(getattr(event, "message_obj", None), "message_str", "") or ""
        if raw:
            candidates.append(raw)
        prefixes = tuple(COMMAND_PREFIXES) + tuple(self._wake_prefixes())
        for text in candidates:
            stripped = text.strip()
            if not stripped:
                continue
            if stripped.startswith(prefixes):
                return True
            lowered = stripped.lower()
            if any(lowered.startswith(name) for name in OWN_COMMAND_NAMES):
                return True
        return False

    # ------------------------------------------------------------------ 指令
    @filter.command("mute", alias={"闭嘴"})
    async def mute_command(self, event: AstrMessageEvent):
        """让我闭嘴一段时间。用法：/mute 10"""
        umo = event.unified_msg_origin
        seconds = self._parse_seconds(event.get_message_str() or "")
        if seconds is not None and seconds <= 0:
            self._stop_mute(umo)
            yield event.plain_result(self._ack("ack_unmute_text", "行，可以说了咪~"))
            return
        if seconds is None:
            seconds = self._get_int("default_seconds", 60, 1, self._max_seconds())
        seconds = self._start_mute(umo, seconds)
        yield event.plain_result(
            self._ack("ack_mute_text", "好，闭嘴 {seconds} 秒咪~", seconds),
        )

    @filter.command("unmute", alias={"解除闭嘴"})
    async def unmute_command(self, event: AstrMessageEvent):
        """提前解除闭嘴状态。用法：/unmute"""
        umo = event.unified_msg_origin
        left = self._remaining(umo)
        self._stop_mute(umo)
        if left > 0:
            yield event.plain_result(self._ack("ack_unmute_text", "行，可以说话了咪~"))
        else:
            yield event.plain_result("本来就没闭嘴咪~")

    @filter.command("mute_status", alias={"闭嘴状态"})
    async def mute_status_command(self, event: AstrMessageEvent):
        """查看这个会话还要闭嘴多久。用法：/mute_status"""
        left = self._remaining(event.unified_msg_origin)
        if left > 0:
            yield event.plain_result(f"还要闭嘴 {left} 秒咪~")
        else:
            yield event.plain_result("现在可以说话咪~")

    # ------------------------------------------------------------------ 拦截
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def mute_guard(self, event: AstrMessageEvent):
        """闭嘴期间挡住回复；没闭嘴时识别「别说话」这类口头指令。"""
        text = (event.get_message_str() or "").strip()
        umo = event.unified_msg_origin
        left = self._remaining(umo)

        if left > 0:
            # 闭嘴期间说「可以说话了」这类词就直接解除
            if text and self._hit_keyword(
                text,
                self._get_list("unmute_keywords", DEFAULT_UNMUTE_KEYWORDS),
            ):
                self._stop_mute(umo)
                if self._get_bool("ack_on_keyword", True):
                    yield event.plain_result(
                        self._ack("ack_unmute_text", "行，可以说话了咪~"),
                    )
                self._silence(event)
                return
            # 指令照常放行，不然没法提前解除
            if self._looks_like_command(event):
                return
            self._silence(event)
            return

        if not text:
            return
        if not self._hit_keyword(
            text,
            self._get_list("mute_keywords", DEFAULT_MUTE_KEYWORDS),
        ):
            return
        if not self._keyword_allowed(event):
            return

        seconds = self._get_int("default_seconds", 60, 1, self._max_seconds())
        seconds = self._start_mute(umo, seconds)
        if self._get_bool("ack_on_keyword", True):
            yield event.plain_result(
                self._ack("ack_mute_text", "好，闭嘴 {seconds} 秒咪~", seconds),
            )
        self._silence(event)
