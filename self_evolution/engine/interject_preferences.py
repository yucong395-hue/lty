from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class InterjectOutcome(Enum):
    CONNECTED = "connected"
    IGNORED = "ignored"
    AWKWARD = "awkward"
    EXTENDED = "extended"


@dataclass
class InterjectPreferences:
    scope_id: str = ""

    posture_bias: dict = field(default_factory=dict)
    initiative_trend: float = 0.0
    recent_success_rate: float = 0.5

    total_interjections: int = 0
    successful_interjections: int = 0

    last_interject_at: float = 0.0
    last_interject_posture: str = ""
    last_interject_motive: str = ""

    outcome_counts: dict = field(default_factory=lambda: defaultdict(int))

    updated_at: float = field(default_factory=time.time)

    def record_interjection(self, posture: str, motive: str) -> None:
        self.last_interject_at = time.time()
        self.last_interject_posture = posture
        self.last_interject_motive = motive

    def record_outcome(self, outcome: InterjectOutcome, success: bool, delta: float = 0.02) -> None:
        self.total_interjections += 1
        if success:
            self.successful_interjections += 1

        self.outcome_counts[outcome.value] += 1

        alpha = 0.1
        self.recent_success_rate = alpha * (1.0 if success else 0.0) + (1.0 - alpha) * self.recent_success_rate

        if success:
            self.initiative_trend = min(1.0, self.initiative_trend + delta)
        else:
            self.initiative_trend = max(-1.0, self.initiative_trend - delta * 0.5)

        self.updated_at = time.time()

    def check_and_record_outcome(
        self, now: float, reply_received: bool, silence_seconds: float
    ) -> Optional[InterjectOutcome]:
        if self.last_interject_at == 0.0:
            return None

        elapsed = now - self.last_interject_at
        if elapsed > 300:
            return None

        if not reply_received:
            outcome = InterjectOutcome.IGNORED
            self.record_outcome(outcome, success=False)
            return outcome

        if elapsed < 60 and silence_seconds < 30:
            outcome = InterjectOutcome.EXTENDED
            self.record_outcome(outcome, success=True)
        elif elapsed < 120:
            outcome = InterjectOutcome.CONNECTED
            self.record_outcome(outcome, success=True)
        else:
            outcome = InterjectOutcome.AWKWARD
            self.record_outcome(outcome, success=False)

        self.last_interject_at = 0.0
        return outcome

    def adjust_posture_bias(self, posture: str, delta: float) -> None:
        current = self.posture_bias.get(posture, 0.0)
        self.posture_bias[posture] = max(-0.3, min(0.3, current + delta))
        self.updated_at = time.time()

    def get_posture_modifier(self, posture: str) -> float:
        return self.posture_bias.get(posture, 0.0)

    def get_initiative_modifier(self) -> float:
        return self.initiative_trend * 0.05

    def get_outcome_rate(self, outcome: InterjectOutcome) -> float:
        total = sum(self.outcome_counts.values())
        if total == 0:
            return 0.5
        return self.outcome_counts.get(outcome.value, 0) / total

    def to_dict(self) -> dict:
        return {
            "scope_id": self.scope_id,
            "posture_bias": self.posture_bias,
            "initiative_trend": self.initiative_trend,
            "recent_success_rate": self.recent_success_rate,
            "total_interjections": self.total_interjections,
            "successful_interjections": self.successful_interjections,
            "last_interject_at": self.last_interject_at,
            "last_interject_posture": self.last_interject_posture,
            "last_interject_motive": self.last_interject_motive,
            "outcome_counts": dict(self.outcome_counts),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InterjectPreferences":
        pref = cls(scope_id=data.get("scope_id", ""))
        pref.posture_bias = data.get("posture_bias", {})
        pref.initiative_trend = data.get("initiative_trend", 0.0)
        pref.recent_success_rate = data.get("recent_success_rate", 0.5)
        pref.total_interjections = data.get("total_interjections", 0)
        pref.successful_interjections = data.get("successful_interjections", 0)
        pref.last_interject_at = data.get("last_interject_at", 0.0)
        pref.last_interject_posture = data.get("last_interject_posture", "")
        pref.last_interject_motive = data.get("last_interject_motive", "")
        oc = data.get("outcome_counts", {})
        pref.outcome_counts = defaultdict(int, oc) if oc else defaultdict(int)
        pref.updated_at = data.get("updated_at", time.time())
        return pref


class InterjectPreferenceStore:
    def __init__(self):
        self._cache: dict[str, InterjectPreferences] = {}

    def get(self, scope_id: str) -> InterjectPreferences:
        if scope_id not in self._cache:
            self._cache[scope_id] = InterjectPreferences(scope_id=scope_id)
        return self._cache[scope_id]

    def set(self, scope_id: str, pref: InterjectPreferences) -> None:
        self._cache[scope_id] = pref

    def load_from_dict(self, scope_id: str, data: dict) -> None:
        if data:
            self._cache[scope_id] = InterjectPreferences.from_dict(data)
        else:
            self._cache[scope_id] = InterjectPreferences(scope_id=scope_id)
