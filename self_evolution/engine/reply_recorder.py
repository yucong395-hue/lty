import json
import time

from astrbot.api import logger

from .interject_preferences import InterjectOutcome, InterjectPreferenceStore, InterjectPreferences
from .reply_state import BotMessageKind, ConversationMomentum


_MESSAGE_WINDOW_SECONDS = 120.0


class ReplyRecorder:
    """统一记录 bot 发言后的状态回写。

    所有来源（框架主回复/被动社交/主动插话/表情包）的 bot 发言成功后
    都调用同一个入口，保证状态更新逻辑完全一致。
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self._pref_store = InterjectPreferenceStore()

    async def record(
        self,
        scope_id: str,
        executed: bool,
        momentum: ConversationMomentum,
        *,
        level: str = None,
        is_active_trigger: bool = False,
        posture_value: str = "",
        scene_value: str = "",
        motive_value: str = "",
    ) -> None:
        """记录 bot 发言后的状态。

        调用方应先调用 bot_spoke()，再调用 record()。

        Args:
            scope_id: 群 ID
            executed: 是否成功发送
            momentum: 已经过 bot_spoke() 更新后的 momentum
            level: engagement level
            is_active_trigger: 是否为主动插嘴
            posture_value: ResponsePosture enum value
            scene_value: SceneType enum value
            motive_value: ActiveMotive enum value
        """
        now = time.time()

        if not executed:
            momentum.consecutive_bot_replies = 0

        saved = momentum.to_dict()
        if level:
            saved["last_bot_engagement_level"] = level
            saved["last_bot_engagement_at"] = now

        await self.plugin.dao.save_engagement_state(scope_id, saved)

        if is_active_trigger:
            await self._update_interject_preferences(scope_id, executed, posture_value, scene_value, motive_value)

    async def _update_interject_preferences(
        self,
        scope_id: str,
        success: bool,
        posture_value: str,
        scene_value: str,
        motive_value: str = "",
    ) -> None:
        data = await self.plugin.dao.get_interject_preferences(scope_id)
        self._pref_store.load_from_dict(scope_id, data or {})
        pref = self._pref_store.get(scope_id)

        if success and posture_value and motive_value:
            pref.record_interjection(posture_value, motive_value)
        elif success:
            pref.record_outcome(InterjectOutcome.CONNECTED, success=True)
        else:
            pref.record_outcome(InterjectOutcome.IGNORED, success=False)

        if posture_value and success:
            delta = 0.01 if scene_value == "casual" else 0.005
            pref.adjust_posture_bias(posture_value, delta)

        await self.plugin.dao.save_interject_preferences(scope_id, json.dumps(pref.to_dict()))

    async def check_and_record_outcome(self, scope_id: str, reply_received: bool, silence_seconds: float) -> None:
        """在被动事件或下次主动检查时调用，判断上一次主动插嘴的结果并记录。"""
        now = time.time()
        data = await self.plugin.dao.get_interject_preferences(scope_id)
        self._pref_store.load_from_dict(scope_id, data or {})
        pref = self._pref_store.get(scope_id)

        outcome = pref.check_and_record_outcome(now, reply_received, silence_seconds)
        if outcome is not None:
            await self.plugin.dao.save_interject_preferences(scope_id, json.dumps(pref.to_dict()))
            logger.debug(f"[InterjectOutcome] scope={scope_id} outcome={outcome.value}")

    async def record_framework_normal(
        self,
        scope_id: str,
        momentum: ConversationMomentum,
        level: str = "full",
    ) -> None:
        """框架主回复（FRAMEWORK_NORMAL）专用记录入口。"""
        momentum.bot_spoke(time.time(), BotMessageKind.NORMAL, start_new_wave=False)
        await self.record(scope_id=scope_id, executed=True, momentum=momentum, level=level)
