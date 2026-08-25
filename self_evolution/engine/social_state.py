from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .speech_types import AnchorType, OpportunityKind, ResponsePosture, SpeechDecision, SpeechOpportunity, ThreadAnchor


class EngagementLevel(Enum):
    IGNORE = "ignore"
    REACT = "react"
    TEXT_LITE = "text_lite"
    FULL = "full"


class SceneType(Enum):
    IDLE = "idle"
    CASUAL = "casual"
    HELP = "help"
    DEBATE = "debate"


class SceneSubType(Enum):
    NONE = "none"
    JOYFUL_BUSTLE = "joyful_bustle"
    LIGHT_SMALLTALK = "light_smalltalk"
    TOPIC_FOCUS = "topic_focus"
    AWKWARD_GAP = "awkward_gap"


@dataclass
class GroupSocialState:
    scope_id: str
    last_message_time: float = 0.0
    last_bot_message_time: float = 0.0
    recent_bot_engagements: deque = field(default_factory=lambda: deque(maxlen=10))
    last_seen_message_seq: Optional[int] = None
    active_user_count: int = 0
    message_count_window: int = 0
    question_count_window: int = 0
    emotion_count_window: int = 0
    mention_bot_recently: bool = False
    scene: SceneType = SceneType.CASUAL
    sub_scene: "SceneSubType" = SceneSubType.NONE
    consecutive_bot_replies: int = 0
    thread_anchor: Optional[ThreadAnchor] = None
    recent_keywords: set = field(default_factory=set)
    wave_fresh: bool = False
    trigger_user_affinity: int = 0
    last_unanswered_question: str = ""
    last_unfinished_joke: str = ""
    recent_interaction_outcome: str = ""
    unfinished_topic: str = ""


@dataclass
class EngagementEligibility:
    allowed: bool
    reason_code: str
    reason_text: str
    new_message_count: int = 0
    silence_seconds: float = 0.0


@dataclass
class EngagementPlan:
    level: EngagementLevel
    reason: str
    confidence: float
    scene: SceneType
    suggested_text: str = ""
    use_sticker: bool = False
    sticker_id: Optional[str] = None
    anchor_type: AnchorType = AnchorType.NONE
    anchor_text: str = ""
    short_reply_bias: float = 0.0
    warmth_bias: float = 0.0
    initiative_bias: float = 0.0
    playfulness_bias: float = 0.0
    posture: ResponsePosture = ResponsePosture.NONE
    pending_anchor_text: str = ""
    pending_trigger_reason: str = ""
    sub_scene: "SceneSubType" = None

    def to_speech_decision(self) -> SpeechDecision:
        if self.level == EngagementLevel.IGNORE:
            return SpeechDecision.ignore(self.reason)
        if self.level == EngagementLevel.REACT:
            return SpeechDecision.emoji(self.reason, self.confidence)

        import random
        from .speech_types import TextLiteVariant

        if self.level == EngagementLevel.TEXT_LITE:
            variant = self._derive_text_lite_variant()
            max_chars_map = {
                TextLiteVariant.QUICK_TOUCH: random.choices([5, 10, 15], weights=[60, 30, 10])[0],
                TextLiteVariant.QUIET_FOLLOW: random.choices([10, 18, 25], weights=[40, 40, 20])[0],
                TextLiteVariant.SMALL_PROBE: random.choices([12, 20, 30], weights=[30, 45, 25])[0],
            }
            max_chars = max_chars_map.get(variant, 15)
            return SpeechDecision.text(
                text_mode="reply",
                anchor_type=self.anchor_type,
                confidence=self.confidence,
                reason=self.reason,
                max_chars=max_chars,
                must_follow_thread=True,
                anchor_text=self.anchor_text,
                warmth_hint=self.warmth_bias,
                initiative_hint=self.initiative_bias,
                playfulness_hint=self.playfulness_bias,
                posture=self.posture,
                text_lite_variant=variant,
            )

        if self.scene == SceneType.CASUAL:
            max_chars = random.choices([30, 60, 120], weights=[60, 30, 10])[0]
        elif self.scene == SceneType.HELP:
            max_chars = random.choices([60, 120, 200], weights=[30, 50, 20])[0]
        elif self.scene == SceneType.DEBATE:
            max_chars = random.choices([30, 80, 150], weights=[50, 35, 15])[0]
        else:
            max_chars = 100

        if self.short_reply_bias > 0.3:
            max_chars = int(max_chars * 0.6)
        elif self.short_reply_bias > 0.1:
            max_chars = int(max_chars * 0.8)

        return SpeechDecision.text(
            text_mode="reply",
            anchor_type=self.anchor_type,
            confidence=self.confidence,
            reason=self.reason,
            max_chars=max_chars,
            must_follow_thread=True,
            anchor_text=self.anchor_text,
            warmth_hint=self.warmth_bias,
            initiative_hint=self.initiative_bias,
            playfulness_hint=self.playfulness_bias,
            posture=self.posture,
        )

    def _derive_text_lite_variant(self) -> "TextLiteVariant":
        from .speech_types import TextLiteVariant

        posture = self.posture
        playfulness = self.playfulness_bias
        warmth = self.warmth_bias
        sub_scene = getattr(self, "sub_scene", None)

        if posture.value in ("quiet_ack", "soft_continue"):
            return TextLiteVariant.QUIET_FOLLOW
        if sub_scene and sub_scene.value == "topic_focus":
            return TextLiteVariant.SMALL_PROBE
        if posture.value == "gentle_answer":
            return TextLiteVariant.SMALL_PROBE
        if sub_scene and sub_scene.value == "joyful_bustle" and playfulness >= 0.0:
            return TextLiteVariant.QUICK_TOUCH
        if playfulness > 0.15:
            return TextLiteVariant.QUICK_TOUCH
        if warmth > 0.15:
            return TextLiteVariant.QUIET_FOLLOW
        return TextLiteVariant.QUICK_TOUCH


@dataclass
class EngagementExecutionResult:
    executed: bool
    level: EngagementLevel
    action: str
    reason: str
    actual_text: str = ""
