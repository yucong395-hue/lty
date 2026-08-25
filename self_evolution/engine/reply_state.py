from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import time


class BotMessageKind(Enum):
    NORMAL = "normal"
    PASSIVE = "passive"
    ACTIVE = "active"
    STICKER = "sticker"


class UnfinishedCueType(Enum):
    QUESTION = "question"
    JOKE = "joke"
    TOPIC = "topic"
    MENTION_TARGET = "mention_target"
    EMOTIONAL = "emotional"


@dataclass
class UnfinishedCue:
    cue_type: UnfinishedCueType
    text: str
    created_at: float
    expires_at: float
    user_id: str = ""
    user_name: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return now > self.expires_at


@dataclass
class ConversationMomentum:
    """统一群聊节奏状态。

    只负责状态，不负责决策。记录：
    - 群最近发生了什么（用户消息时间戳）
    - bot 最近说了什么（发言时间、类型）
    - 当前 wave 是否被 bot 占住（bot_has_spoken_in_current_wave）
    - 用户在 bot 发言后是否有新消息（new_user_message_after_bot）
    """

    scope_id: str

    last_message_time: float = 0.0
    last_bot_message_at: float = 0.0
    last_bot_message_kind: BotMessageKind = BotMessageKind.NORMAL

    scene_type: str = "casual"

    message_count_window: int = 0
    question_count_window: int = 0
    emotion_count_window: int = 0

    wave_started_at: float = 0.0
    bot_has_spoken_in_current_wave: bool = False
    new_user_message_after_bot: bool = False

    consecutive_bot_replies: int = 0

    last_seen_message_seq: Optional[int] = None

    wave_fresh: bool = False
    last_unanswered_question: str = ""
    last_unfinished_joke: str = ""
    unfinished_cues: list = field(default_factory=list)

    _CUE_TTL_SECONDS: float = 180.0

    def _make_cue(
        self, cue_type: UnfinishedCueType, text: str, user_id: str = "", user_name: str = ""
    ) -> UnfinishedCue:
        now = time.time()
        return UnfinishedCue(
            cue_type=cue_type,
            text=text,
            created_at=now,
            expires_at=now + self._CUE_TTL_SECONDS,
            user_id=user_id,
            user_name=user_name,
        )

    def push_unfinished_cue(
        self, cue_type: UnfinishedCueType, text: str, user_id: str = "", user_name: str = ""
    ) -> None:
        cue = self._make_cue(cue_type, text, user_id, user_name)
        self.unfinished_cues = [c for c in self.unfinished_cues if c.cue_type != cue_type]
        self.unfinished_cues.append(cue)

    def get_valid_cues(self) -> list[UnfinishedCue]:
        now = time.time()
        valid = [c for c in self.unfinished_cues if not c.is_expired(now)]
        self.unfinished_cues = valid
        return valid

    def get_cue(self, cue_type: UnfinishedCueType) -> UnfinishedCue | None:
        valid = self.get_valid_cues()
        for c in valid:
            if c.cue_type == cue_type:
                return c
        return None

    def user_message_arrived(self, now: float) -> None:
        """用户发消息：开启或续活 wave，新 turn 开始，允许 bot 重新响应。"""
        self.last_message_time = now
        self.new_user_message_after_bot = False
        self.bot_has_spoken_in_current_wave = False
        self.wave_fresh = False

    def set_unanswered_question(self, text: str, user_id: str = "", user_name: str = "") -> None:
        if text and len(text.strip()) >= 4:
            self.last_unanswered_question = text.strip()
            self.push_unfinished_cue(UnfinishedCueType.QUESTION, text.strip(), user_id, user_name)

    def clear_unanswered_question(self) -> None:
        self.last_unanswered_question = ""
        self.unfinished_cues = [c for c in self.unfinished_cues if c.cue_type != UnfinishedCueType.QUESTION]

    def set_unfinished_joke(self, text: str, user_id: str = "", user_name: str = "") -> None:
        if text and len(text.strip()) >= 2:
            self.last_unfinished_joke = text.strip()
            self.push_unfinished_cue(UnfinishedCueType.JOKE, text.strip(), user_id, user_name)

    def clear_unfinished_joke(self) -> None:
        self.last_unfinished_joke = ""
        self.unfinished_cues = [c for c in self.unfinished_cues if c.cue_type != UnfinishedCueType.JOKE]

    def bot_spoke(self, now: float, kind: BotMessageKind, start_new_wave: bool = False) -> None:
        """bot 发消息：占住当前 wave，并递增连发计数。"""
        self.last_bot_message_at = now
        self.last_bot_message_kind = kind
        self.bot_has_spoken_in_current_wave = True
        self.new_user_message_after_bot = False
        self.consecutive_bot_replies = self.consecutive_bot_replies + 1
        self.last_unanswered_question = ""
        self.unfinished_cues = [
            c for c in self.unfinished_cues if c.cue_type not in (UnfinishedCueType.QUESTION, UnfinishedCueType.JOKE)
        ]
        if start_new_wave:
            self.wave_started_at = now

    def new_user_after_bot(self) -> None:
        """用户消息到达时检测到 bot 之前已发言。"""
        self.new_user_message_after_bot = True
        self.last_unanswered_question = ""

    def reset_wave(self, now: float) -> None:
        """窗口失效，开启新 wave。"""
        self.wave_started_at = now
        self.bot_has_spoken_in_current_wave = False
        self.new_user_message_after_bot = False
        self.message_count_window = 0
        self.question_count_window = 0
        self.emotion_count_window = 0
        self.scene_type = "casual"
        self.consecutive_bot_replies = 0
        self.wave_fresh = True
        self.last_unanswered_question = ""
        self.last_unfinished_joke = ""
        self.unfinished_cues = []

    def is_wave_active(self, now: float, window_seconds: float = 120.0) -> bool:
        return self.last_message_time > 0 and (now - self.last_message_time) <= window_seconds

    def to_dict(self) -> dict:
        cues_data = []
        for c in self.unfinished_cues:
            cues_data.append(
                {
                    "cue_type": c.cue_type.value,
                    "text": c.text,
                    "created_at": c.created_at,
                    "expires_at": c.expires_at,
                    "user_id": c.user_id,
                    "user_name": c.user_name,
                }
            )
        return {
            "scope_id": self.scope_id,
            "last_message_time": self.last_message_time,
            "last_bot_message_at": self.last_bot_message_at,
            "last_bot_message_kind": self.last_bot_message_kind.value,
            "scene_type": self.scene_type,
            "message_count_window": self.message_count_window,
            "question_count_window": self.question_count_window,
            "emotion_count_window": self.emotion_count_window,
            "wave_started_at": self.wave_started_at,
            "bot_has_spoken_in_current_wave": int(self.bot_has_spoken_in_current_wave),
            "new_user_message_after_bot": int(self.new_user_message_after_bot),
            "consecutive_bot_replies": self.consecutive_bot_replies,
            "last_seen_message_seq": self.last_seen_message_seq,
            "wave_fresh": int(self.wave_fresh),
            "last_unanswered_question": self.last_unanswered_question,
            "last_unfinished_joke": self.last_unfinished_joke,
            "unfinished_cues": cues_data,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationMomentum":
        m = cls(scope_id=data.get("scope_id", ""))
        m.last_message_time = float(data.get("last_message_time") or 0)
        m.last_bot_message_at = float(data.get("last_bot_message_at") or 0)
        kind_str = data.get("last_bot_message_kind", "normal")
        try:
            m.last_bot_message_kind = BotMessageKind(kind_str)
        except ValueError:
            m.last_bot_message_kind = BotMessageKind.NORMAL
        m.scene_type = data.get("scene_type", "casual")
        m.message_count_window = int(data.get("message_count_window") or 0)
        m.question_count_window = int(data.get("question_count_window") or 0)
        m.emotion_count_window = int(data.get("emotion_count_window") or 0)
        m.wave_started_at = float(data.get("wave_started_at") or 0)
        if m.wave_started_at == 0 and m.last_message_time > 0:
            m.wave_started_at = m.last_message_time
        m.bot_has_spoken_in_current_wave = bool(int(data.get("bot_has_spoken_in_current_wave") or 0))
        m.new_user_message_after_bot = bool(int(data.get("new_user_message_after_bot") or 0))
        m.consecutive_bot_replies = int(data.get("consecutive_bot_replies") or 0)
        m.last_seen_message_seq = data.get("last_seen_message_seq")
        m.wave_fresh = bool(data.get("wave_fresh", False))
        m.last_unanswered_question = data.get("last_unanswered_question", "")
        m.last_unfinished_joke = data.get("last_unfinished_joke", "")
        cues_raw = data.get("unfinished_cues", [])
        if isinstance(cues_raw, str):
            import json as json_module

            try:
                cues_raw = json_module.loads(cues_raw)
            except Exception:
                cues_raw = []
        m.unfinished_cues = []
        for c_data in cues_raw:
            try:
                cue_type = UnfinishedCueType(c_data.get("cue_type", "question"))
                m.unfinished_cues.append(
                    UnfinishedCue(
                        cue_type=cue_type,
                        text=c_data.get("text", ""),
                        created_at=float(c_data.get("created_at", 0)),
                        expires_at=float(c_data.get("expires_at", 0)),
                        user_id=c_data.get("user_id", ""),
                        user_name=c_data.get("user_name", ""),
                    )
                )
            except Exception:
                pass
        return m
