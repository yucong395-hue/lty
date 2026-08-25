"""
天依插件事件总线 — 轻量跨插件 Pub/Sub
=======================================
简单的单例模式，让 bili_agent / emotional_echo / self_evolution 之间能互相通知。
用法：
    from event_bus import event_bus
    event_bus.on("video_discovered", handler)
    event_bus.emit("video_discovered", {"bvid": "BVxxx", "title": "..."})
"""
import asyncio
import logging
import time
from typing import Any, Callable, Coroutine

logger = logging.getLogger("EventBus")


class EventBus:
    """极简事件总线，单例，进程内广播"""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable[..., Coroutine | None]):
        """注册事件监听器（自动去重，防止热重载重复注册）"""
        if event not in self._handlers:
            self._handlers[event] = []
        if any(h is handler for h in self._handlers[event]):
            return
        self._handlers[event].append(handler)
        logger.info(f"[EventBus] 已注册监听: {event}")

    def off(self, event: str, handler: Callable):
        """移除事件监听器"""
        if event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]
            logger.info(f"[EventBus] 已移除监听: {event}")

    def emit(self, event: str, data: Any = None):
        """触发事件（同步广播，不 await，不阻塞调用方）"""
        if event not in self._handlers:
            return
        for handler in self._handlers[event]:
            try:
                if asyncio.iscoroutinefunction(handler):
                    asyncio.ensure_future(handler(event, data))
                else:
                    handler(event, data)
            except Exception as e:
                logger.warning(f"[EventBus] 处理 {event} 时出错: {e}")

    def list_events(self) -> dict[str, int]:
        """查看当前注册的事件和监听器数量"""
        return {e: len(hs) for e, hs in self._handlers.items()}


# 全局单例
event_bus = EventBus()