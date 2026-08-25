import asyncio
import time
import re

from astrbot.api import logger

from .opportunity_cache import ActiveMotive, MotiveType, OpportunityScore, PendingOpportunity
from .social_state import (
    EngagementEligibility,
    EngagementLevel,
    EngagementPlan,
    GroupSocialState,
    SceneSubType,
    SceneType,
)
from .speech_types import AnchorType, OpportunityKind, ResponsePosture, SpeechOpportunity, ThreadAnchor


QUESTION_WORDS = {"吗", "呢", "怎么", "如何", "为什么", "啥", "什么", "是不是", "能不能", "要不要", "?", "？"}
EMOTION_WORDS = {"哈哈", "哈哈哈", "笑死", "卧槽", "牛逼", "厉害", "赞", "哭", "笑", "怒", "生气", "烦"}
DEBATE_INDICATORS = {"不对", "不是", "但是", "可是", "虽然", "然而", "错的", "胡说", "滚", "傻"}
HELP_INDICATORS = {"帮", "帮我", "请问", "请教", "求助", "救命", "急", "在线等"}

RHETORICAL_PATTERNS = [
    re.compile(r"^真的吗[？?。.]?$"),
    re.compile(r"^不是吧[。.]?$"),
    re.compile(r"^不会吧[。.]?$"),
    re.compile(r"^真的假的[？?。.]?$"),
    re.compile(r"^我去[，,]?这也能"),
    re.compile(r"^说实话[，,]?"),
    re.compile(r"^讲真[，,]?"),
    re.compile(r"^话说[，,]?"),
    re.compile(r"^话说回来"),
]
GREETING_QUESTION_PATTERNS = [
    re.compile(r"^[吃喝睡在去哪好没].{0,4}[吗呢嘛]$"),
    re.compile(r"^吃了没"),
    re.compile(r"^在吗$"),
    re.compile(r"^在不在$"),
    re.compile(r"^还好吗$"),
]
ACTION_OR_REACTION_PATTERNS = [
    re.compile(r"^[卧我噢哦诶唉呃哈嘿]+[。,]?"),
    re.compile(r"^笑死我了[。]?$"),
    re.compile(r"^[太真可]?[很就都还挺]"),
    re.compile(r"^无语[，,]?"),
    re.compile(r"^离谱[。]?$"),
    re.compile(r"^绝了[。]?$"),
    re.compile(r"^牛[。]?[年月日]?$"),
]
MEMORABLE_KEYWORDS = {
    "笑死我了",
    "笑死",
    "笑不活",
    "笑到",
    "卧槽",
    "我擦",
    "我天",
    "我去",
    "牛逼",
    "牛啊",
    "太牛了",
    "离谱",
    "太离谱",
    "真离谱",
    "绝了",
    "太绝了",
    "绝绝子",
    "芭比q",
    "完了完了",
    "救命",
    "救命啊",
    "哭死",
    "哭唧唧",
}
PERSONA_HOOK_MIN_LENGTH = 3


class EngagementPlanner:
    def __init__(self, plugin):
        self.plugin = plugin
        self.cfg = plugin.cfg

    def _debug(self, msg: str):
        if getattr(self.cfg, "engagement_debug_enabled", False):
            logger.debug(msg)

    def classify_scene(self, messages: list[dict], state: GroupSocialState) -> SceneType:
        if not messages:
            return SceneType.IDLE

        now = time.time()
        recent_msgs = messages[:10] if len(messages) > 10 else messages

        question_count = 0
        emotion_count = 0
        debate_count = 0
        help_count = 0
        total_chars = 0

        for msg in recent_msgs:
            text = msg.get("text", "") or ""
            total_chars += len(text)

            text_lower = text.lower()
            for qw in QUESTION_WORDS:
                if qw in text:
                    question_count += 1
                    break

            for ew in EMOTION_WORDS:
                if ew in text:
                    emotion_count += 1
                    break

            for dw in DEBATE_INDICATORS:
                if dw in text:
                    debate_count += 1
                    break

            for hw in HELP_INDICATORS:
                if hw in text:
                    help_count += 1
                    break

        silence_minutes = (now - state.last_message_time) / 60 if state.last_message_time > 0 else 999

        if silence_minutes > 10 and state.message_count_window < 5:
            return SceneType.IDLE

        if debate_count >= 3 or (debate_count >= 2 and emotion_count >= 2):
            return SceneType.DEBATE

        if help_count >= 2 or (help_count >= 1 and question_count >= 2):
            return SceneType.HELP

        return SceneType.CASUAL

    def compute_scene_windows(self, messages: list[dict], state: GroupSocialState) -> dict:
        if not messages:
            return {
                "question_count_window": 0,
                "emotion_count_window": 0,
                "mention_bot_recently": False,
            }

        recent_msgs = messages[:10] if len(messages) > 10 else messages
        bot_id = str(self.plugin._get_bot_id()) if hasattr(self.plugin, "_get_bot_id") else ""

        question_count = 0
        emotion_count = 0
        mention_bot_recently = False

        for msg in recent_msgs:
            text = msg.get("text", "") or ""

            for qw in QUESTION_WORDS:
                if qw in text:
                    question_count += 1
                    break

            for ew in EMOTION_WORDS:
                if ew in text:
                    emotion_count += 1
                    break

            if bot_id:
                for seg in msg.get("message", []):
                    if isinstance(seg, dict) and seg.get("type") == "at":
                        at_qq = str(seg.get("data", {}).get("qq", ""))
                        if at_qq == bot_id or at_qq == "all":
                            mention_bot_recently = True

        return {
            "question_count_window": question_count,
            "emotion_count_window": emotion_count,
            "mention_bot_recently": mention_bot_recently,
        }

    def check_eligibility(
        self, state: GroupSocialState, cooldown_seconds: int = 30, min_new_messages: int = 3
    ) -> EngagementEligibility:
        now = time.time()

        silence_seconds = now - state.last_message_time if state.last_message_time > 0 else 999

        if state.last_bot_message_time > 0:
            bot_silence = now - state.last_bot_message_time
            if bot_silence < cooldown_seconds:
                self._debug(
                    f"[Engagement] eligible=no scope={getattr(state, 'scope_id', '?')} reason=cooldown remaining={int(cooldown_seconds - bot_silence)}s"
                )
                return EngagementEligibility(
                    allowed=False,
                    reason_code="E_COOLDOWN",
                    reason_text=f"Bot冷却中，还需{int(cooldown_seconds - bot_silence)}秒",
                    silence_seconds=bot_silence,
                )

        wave_fresh = getattr(state, "wave_fresh", False)
        if silence_seconds < 5 and state.message_count_window > 0 and not wave_fresh:
            self._debug(
                f"[Engagement] eligible=no scope={getattr(state, 'scope_id', '?')} reason=silence_too_short {int(silence_seconds)}s"
            )
            return EngagementEligibility(
                allowed=False,
                reason_code="E_SILENCE",
                reason_text=f"群太活跃，{int(silence_seconds)}秒前才有消息",
                silence_seconds=silence_seconds,
            )

        if state.consecutive_bot_replies >= 2:
            self._debug(
                f"[Engagement] eligible=no scope={getattr(state, 'scope_id', '?')} reason=bot_flood count={state.consecutive_bot_replies}"
            )
            return EngagementEligibility(
                allowed=False,
                reason_code="E_BOT_FLOOD",
                reason_text=f"Bot连续回复{state.consecutive_bot_replies}次，暂缓",
                silence_seconds=silence_seconds,
            )

        if state.message_count_window < min_new_messages:
            self._debug(
                f"[Engagement] eligible=no scope={getattr(state, 'scope_id', '?')} reason=msg_count {state.message_count_window}/{min_new_messages}"
            )
            return EngagementEligibility(
                allowed=False,
                reason_code="E_MSG_COUNT",
                reason_text=f"消息量不足({state.message_count_window}/{min_new_messages})",
                new_message_count=state.message_count_window,
                silence_seconds=silence_seconds,
            )

        self._debug(
            f"[Engagement] eligible=yes scope={getattr(state, 'scope_id', '?')} msgs={state.message_count_window} silence={int(silence_seconds)}s"
        )
        return EngagementEligibility(
            allowed=True,
            reason_code="OK",
            reason_text="资格检测通过",
            new_message_count=state.message_count_window,
            silence_seconds=silence_seconds,
        )

    async def _enrich_state_with_recent_interaction(
        self, state: GroupSocialState, motive: ActiveMotive | None = None
    ) -> None:
        """从 PersonaSim 读取最近互动余味，填充到 state.recent_interaction_outcome。"""
        try:
            persona_sim = getattr(self.plugin, "persona_sim", None)
            if not persona_sim:
                return
            snapshot = await persona_sim.get_snapshot(state.scope_id)
            if not snapshot:
                return
            recent = snapshot.recent_events
            interaction_events = [e for e in recent if e.event_type.name == "INTERACTION"]
            if interaction_events:
                last = interaction_events[-1]
                outcome = getattr(last, "interaction_outcome", "") or ""
                if outcome in ("connected", "missed", "awkward", "relief"):
                    state.recent_interaction_outcome = outcome

            if not state.unfinished_topic:
                social_todos = [t for t in snapshot.pending_todos if t.todo_type.value == "social"]
                for todo in social_todos:
                    if "话题" in todo.title or "继续聊" in todo.title or "没说完" in todo.title:
                        state.unfinished_topic = todo.title
                        break
        except Exception:
            pass

    async def plan_engagement(
        self,
        state: GroupSocialState,
        eligibility: EngagementEligibility,
        has_mention: bool = False,
        has_reply_to_bot: bool = False,
        trigger_text: str = "",
        motive: ActiveMotive | None = None,
        pending_anchor_text: str = "",
        pending_trigger_reason: str = "",
        interject_pref=None,
    ) -> EngagementPlan:
        scene = self.classify_scene_from_state(state)
        sub_scene = self.classify_sub_scene(state, scene)

        await self._enrich_state_with_recent_interaction(state, motive)

        confidence = min(eligibility.silence_seconds / 120.0, 1.0) * 0.5 + 0.5

        plan = self._build_base_plan(
            scene, sub_scene, eligibility, confidence, has_mention, has_reply_to_bot, trigger_text, state, motive=motive
        )

        if pending_anchor_text and plan.level == EngagementLevel.FULL:
            plan.anchor_text = pending_anchor_text

        drive = await self._get_persona_drive(state.scope_id)

        from datetime import datetime

        is_night = 23 <= datetime.now().hour or datetime.now().hour < 6
        plan = self._apply_persona_drive(plan, drive, motive=motive, is_night=is_night)

        warmth = getattr(plan, "warmth_bias", 0.0)
        playfulness = getattr(plan, "playfulness_bias", 0.0)
        score_for_posture = min(getattr(plan, "confidence", 0.5), 0.9)
        posture = self._compute_posture(
            scene,
            sub_scene,
            score_for_posture,
            motive,
            warmth,
            playfulness,
            is_night=is_night,
            interject_pref=interject_pref,
        )
        plan = EngagementPlan(
            level=plan.level,
            reason=plan.reason,
            confidence=plan.confidence,
            scene=plan.scene,
            suggested_text=plan.suggested_text,
            use_sticker=plan.use_sticker,
            sticker_id=plan.sticker_id,
            anchor_type=plan.anchor_type,
            anchor_text=plan.anchor_text,
            short_reply_bias=plan.short_reply_bias,
            warmth_bias=plan.warmth_bias,
            initiative_bias=plan.initiative_bias,
            playfulness_bias=plan.playfulness_bias,
            posture=posture,
            pending_anchor_text=pending_anchor_text,
            pending_trigger_reason=pending_trigger_reason,
            sub_scene=sub_scene,
        )

        self._debug(
            f"[Engagement] scene={scene.value} eligible={'yes' if eligibility.allowed else 'no'} level={plan.level.value} reason={plan.reason} posture={plan.posture.value}"
        )
        return plan

    def _build_base_plan(
        self,
        scene: SceneType,
        sub_scene: SceneSubType,
        eligibility: EngagementEligibility,
        confidence: float,
        has_mention: bool,
        has_reply_to_bot: bool,
        trigger_text: str,
        state: GroupSocialState,
        motive: ActiveMotive | None = None,
    ) -> EngagementPlan:
        if scene == SceneType.IDLE:
            if has_mention or has_reply_to_bot:
                return EngagementPlan(
                    level=EngagementLevel.FULL,
                    reason="idle场景但被明确唤醒",
                    confidence=0.7,
                    scene=scene,
                )
            return EngagementPlan(
                level=EngagementLevel.IGNORE,
                reason="idle场景且无明确唤醒",
                confidence=0.8,
                scene=scene,
            )

        if scene == SceneType.HELP:
            if has_mention or has_reply_to_bot:
                confidence = min(confidence + 0.2, 1.0)
                return EngagementPlan(
                    level=EngagementLevel.FULL,
                    reason="help场景且被明确唤醒",
                    confidence=confidence,
                    scene=scene,
                )
            return EngagementPlan(
                level=EngagementLevel.FULL,
                reason="help场景低相关",
                confidence=0.4,
                scene=scene,
            )

        if scene == SceneType.DEBATE:
            if has_mention or has_reply_to_bot:
                confidence = min(confidence + 0.15, 1.0)
                return EngagementPlan(
                    level=EngagementLevel.FULL,
                    reason="debate场景但被明确唤醒",
                    confidence=confidence,
                    scene=scene,
                )
            return EngagementPlan(
                level=EngagementLevel.IGNORE,
                reason="debate场景且无唤醒",
                confidence=0.6,
                scene=scene,
            )

        if scene == SceneType.CASUAL:
            if has_mention or has_reply_to_bot:
                return EngagementPlan(
                    level=EngagementLevel.FULL,
                    reason="casual场景且被明确唤醒",
                    confidence=0.7,
                    scene=scene,
                )
            opportunity = self.recognize_opportunity(
                state, False, False, trigger_text, motive=motive, sub_scene=sub_scene
            )
            if opportunity.kind == OpportunityKind.EMOJI_REACT:
                return EngagementPlan(
                    level=EngagementLevel.REACT,
                    reason=f"emoji参与: {opportunity.reason}",
                    confidence=opportunity.confidence,
                    scene=scene,
                    anchor_type=opportunity.anchor_type,
                    anchor_text=opportunity.anchor_text,
                )
            elif opportunity.kind == OpportunityKind.TEXT_LITE:
                return EngagementPlan(
                    level=EngagementLevel.TEXT_LITE,
                    reason=f"简短文本: {opportunity.reason}",
                    confidence=opportunity.confidence,
                    scene=scene,
                    anchor_type=opportunity.anchor_type,
                    anchor_text=opportunity.anchor_text,
                )
            elif opportunity.kind in (OpportunityKind.ACTIVE_CONTINUATION, OpportunityKind.TOPIC_HOOK):
                return EngagementPlan(
                    level=EngagementLevel.FULL,
                    reason=f"主动文本: {opportunity.reason}",
                    confidence=opportunity.confidence,
                    scene=scene,
                    anchor_type=opportunity.anchor_type,
                    anchor_text=opportunity.anchor_text,
                )
            return EngagementPlan(
                level=EngagementLevel.IGNORE,
                reason=f"无锚点不参与: {opportunity.reason}",
                confidence=0.7,
                scene=scene,
            )

        return EngagementPlan(
            level=EngagementLevel.IGNORE,
            reason="默认忽略",
            confidence=1.0,
            scene=scene,
        )

    def classify_scene_from_state(self, state: GroupSocialState) -> SceneType:
        if state.emotion_count_window >= 4:
            return SceneType.DEBATE
        if state.question_count_window >= 3:
            return SceneType.HELP
        if state.message_count_window < 3 and state.last_message_time > 0:
            return SceneType.IDLE
        return SceneType.CASUAL

    def classify_sub_scene(self, state: GroupSocialState, scene: SceneType = None) -> SceneSubType:
        """在 CASUAL 场景内细分软标签。

        - joyful_bustle: 高情绪(>=3) + 低问题(<2)，群里热闹兴奋
        - awkward_gap: 长沉默(>60s)，群里冷场或刚恢复
        - topic_focus: 有未回答的问题或 thread_anchor，话题还在延续
        - light_smalltalk: 普通闲聊，无明显特征
        """
        if scene is None:
            scene = state.scene
        if scene != SceneType.CASUAL:
            return SceneSubType.NONE

        silence = time.time() - state.last_message_time if state.last_message_time > 0 else 0

        if state.emotion_count_window >= 3 and state.question_count_window < 2:
            return SceneSubType.JOYFUL_BUSTLE

        if silence > 60 and state.message_count_window >= 1:
            return SceneSubType.AWKWARD_GAP

        thread_anchor = getattr(state, "thread_anchor", None)
        if (thread_anchor and thread_anchor.is_sufficient()) or state.question_count_window >= 1:
            return SceneSubType.TOPIC_FOCUS

        return SceneSubType.LIGHT_SMALLTALK

    async def _get_persona_drive(self, scope_id: str) -> dict | None:
        """读取 persona sim snapshot，返回 drive 信息用于影响 engagement plan。

        增强版：纳入 interaction semantics 和 effect 来源感。
        """
        try:
            persona_sim = getattr(self.plugin, "persona_sim", None)
            if not persona_sim:
                return None
            snapshot = await persona_sim.get_snapshot(scope_id)
            if not snapshot:
                return None
            state = snapshot.state
            active_effects = snapshot.active_effects
            effect_ids = {e.effect_id for e in active_effects}

            short_reply = 0.0
            warmth = 0.0
            initiative = 0.0
            playfulness = 0.0

            if state.energy < 40 or "low_energy" in effect_ids or "tired" in effect_ids or "sleepy" in effect_ids:
                short_reply += 0.3

            if "irritated" in effect_ids:
                warmth -= 0.3

            if "wronged" in effect_ids:
                warmth -= 0.3
                for e in active_effects:
                    if e.effect_id == "wronged" and "主动" in e.source_detail:
                        initiative -= 0.2

            if "lonely" in effect_ids:
                warmth += 0.1
                initiative += 0.3

            if "thriving" in effect_ids:
                warmth += 0.2
                initiative += 0.1
                playfulness += 0.2

            if "relieved" in effect_ids:
                warmth += 0.2
                playfulness += 0.1

            if "satisfied" in effect_ids:
                warmth += 0.2
                playfulness += 0.1

            if "curious" in effect_ids:
                playfulness += 0.1
                initiative += 0.1

            recent = snapshot.recent_events
            interaction_events = [e for e in recent if e.event_type.name == "INTERACTION"]
            if interaction_events:
                last = interaction_events[-1]
                outcome = getattr(last, "interaction_outcome", "")
                mode = getattr(last, "interaction_mode", "")
                if outcome == "missed" and mode == "active":
                    initiative -= 0.2
                    warmth -= 0.1
                elif outcome == "connected":
                    initiative += 0.1
                    warmth += 0.1

            social_todos = [t for t in snapshot.pending_todos if t.todo_type.value == "social"]
            if social_todos:
                top_todo = social_todos[0]
                if "没说完" in top_todo.title or "继续聊" in top_todo.title:
                    initiative += 0.1
                elif "想找人" in top_todo.title:
                    initiative += 0.15

            drive = {
                "energy": state.energy,
                "mood": state.mood,
                "social_need": state.social_need,
                "satiety": state.satiety,
                "effect_ids": effect_ids,
                "active_effects": active_effects,
                "short_reply_bias": short_reply,
                "warmth_bias": warmth,
                "initiative_bias": initiative,
                "playfulness_bias": playfulness,
            }
            return drive
        except Exception:
            return None

    async def _compute_motive(
        self,
        scope_id: str,
        state: GroupSocialState | None = None,
        trigger_text: str = "",
    ) -> ActiveMotive:
        try:
            persona_sim = getattr(self.plugin, "persona_sim", None)
            if not persona_sim:
                return ActiveMotive(motive=MotiveType.NONE, strength=0.0, source="no_persona_sim")

            snapshot = await persona_sim.get_snapshot(scope_id)
            if not snapshot:
                return ActiveMotive(motive=MotiveType.NONE, strength=0.0, source="no_snapshot")

            top_motive = MotiveType.NONE
            max_score = 0.0
            source_detail = ""

            social_todos = [t for t in snapshot.pending_todos if t.todo_type.value == "social"]
            if social_todos:
                top = social_todos[0]
                title = top.title.lower()
                if "没说完" in title or "继续聊" in title or "想聊" in title:
                    score = 0.7
                    if score > max_score:
                        max_score = score
                        top_motive = MotiveType.CONTINUE_THREAD
                        source_detail = f"social_todo:{top.title}"
                elif "想找人" in title or "寂寞" in title:
                    score = 0.8
                    if score > max_score:
                        max_score = score
                        top_motive = MotiveType.SEEK_CONNECTION
                        source_detail = f"social_todo:{top.title}"
                elif "问" in title or "好奇" in title:
                    score = 0.6
                    if score > max_score:
                        max_score = score
                        top_motive = MotiveType.CURIOUS_PROBE
                        source_detail = f"social_todo:{top.title}"

            effect_ids = {e.effect_id for e in snapshot.active_effects}
            if "lonely" in effect_ids or snapshot.state.social_need > 80:
                score = min(0.6 + (snapshot.state.social_need - 80) / 100, 0.9)
                if score > max_score:
                    max_score = score
                    top_motive = MotiveType.SEEK_CONNECTION
                    source_detail = "lonely_or_high_social_need"

            if "curious" in effect_ids:
                score = 0.65
                if score > max_score:
                    max_score = score
                    top_motive = MotiveType.CURIOUS_PROBE
                    source_detail = "effect:curious"

            if "irritated" in effect_ids or "wronged" in effect_ids:
                score = 0.5
                if score > max_score:
                    max_score = score
                    top_motive = MotiveType.SELF_PROTECTIVE
                    source_detail = "effect:irritated_or_wronged"

            recent = snapshot.recent_events
            interaction_events = [e for e in recent if e.event_type.name == "INTERACTION"]
            if interaction_events:
                last = interaction_events[-1]
                outcome = getattr(last, "interaction_outcome", "")
                mode = getattr(last, "interaction_mode", "")
                if outcome == "missed" and mode == "active" and max_score < 0.4:
                    max_score = 0.35
                    top_motive = MotiveType.LIGHT_RELIEF
                    source_detail = "missed_active_interaction"

            if max_score < 0.3 and snapshot.state.energy < 50:
                score = 0.3
                if score > max_score:
                    max_score = score
                    top_motive = MotiveType.AVOID_SILENCE
                    source_detail = "low_energy_avoid_silence"

            return ActiveMotive(motive=top_motive, strength=max_score, source=source_detail)

        except Exception:
            return ActiveMotive(motive=MotiveType.NONE, strength=0.0, source="error")

    def _apply_persona_drive(
        self, plan: EngagementPlan, drive: dict | None, motive: ActiveMotive | None = None, is_night: bool = False
    ) -> EngagementPlan:
        """根据 persona drive 调整 engagement plan。"""
        if not drive:
            return plan

        energy = drive.get("energy", 80)
        mood = drive.get("mood", 70)
        social_need = drive.get("social_need", 50)
        effect_ids = drive.get("effect_ids", set())
        short_reply_bias = drive.get("short_reply_bias", 0.0)
        warmth_bias = drive.get("warmth_bias", 0.0)
        initiative_bias = drive.get("initiative_bias", 0.0)
        playfulness_bias = drive.get("playfulness_bias", 0.0)

        adjusted = plan
        confidence_adjustment = 0.0
        level_override = False

        if is_night:
            confidence_adjustment -= 0.10
            initiative_bias = max(initiative_bias - 0.2, -0.3)
            if plan.level == EngagementLevel.FULL and plan.scene != SceneType.HELP:
                adjusted = EngagementPlan(
                    level=EngagementLevel.REACT,
                    reason=plan.reason + " (夜间保守)",
                    confidence=max(0.25, plan.confidence - 0.10),
                    scene=plan.scene,
                    anchor_type=plan.anchor_type,
                    anchor_text=plan.anchor_text,
                    short_reply_bias=short_reply_bias + 0.15,
                    warmth_bias=warmth_bias,
                    initiative_bias=initiative_bias,
                    playfulness_bias=max(playfulness_bias - 0.1, -0.2),
                    posture=plan.posture,
                )
                level_override = True

        if energy < 30:
            confidence_adjustment -= 0.15
            if plan.level == EngagementLevel.FULL and plan.scene != SceneType.HELP:
                adjusted = EngagementPlan(
                    level=EngagementLevel.REACT,
                    reason=plan.reason + f" (能量不足:{energy:.0f})",
                    confidence=max(0.3, plan.confidence - 0.15),
                    scene=plan.scene,
                    anchor_type=plan.anchor_type,
                    anchor_text=plan.anchor_text,
                    short_reply_bias=short_reply_bias,
                    warmth_bias=warmth_bias,
                    initiative_bias=initiative_bias,
                    playfulness_bias=playfulness_bias,
                    posture=plan.posture,
                )
                level_override = True

        if mood < 30 and not level_override:
            confidence_adjustment -= 0.1

        if mood < 20 and "irritated" in effect_ids:
            if plan.level == EngagementLevel.FULL and plan.scene == SceneType.DEBATE:
                adjusted = EngagementPlan(
                    level=EngagementLevel.REACT,
                    reason=plan.reason + " (情绪不佳)",
                    confidence=max(0.3, plan.confidence - 0.2),
                    scene=plan.scene,
                    anchor_type=plan.anchor_type,
                    anchor_text=plan.anchor_text,
                    short_reply_bias=short_reply_bias,
                    warmth_bias=warmth_bias,
                    initiative_bias=initiative_bias,
                    playfulness_bias=playfulness_bias,
                    posture=plan.posture,
                )
                level_override = True

        if "lonely" in effect_ids or social_need > 80:
            confidence_adjustment += 0.1
            if plan.level == EngagementLevel.IGNORE:
                adjusted = EngagementPlan(
                    level=EngagementLevel.REACT,
                    reason=plan.reason + " (渴望互动)",
                    confidence=min(0.9, plan.confidence + 0.1),
                    scene=plan.scene,
                    anchor_type=plan.anchor_type,
                    anchor_text=plan.anchor_text,
                    short_reply_bias=short_reply_bias,
                    warmth_bias=warmth_bias,
                    initiative_bias=initiative_bias,
                    playfulness_bias=playfulness_bias,
                    posture=plan.posture,
                )

        if "thriving" in effect_ids:
            confidence_adjustment += 0.05

        if not level_override and confidence_adjustment != 0:
            new_confidence = max(0.1, min(0.95, plan.confidence + confidence_adjustment))
            if new_confidence != plan.confidence or short_reply_bias != 0.0:
                adjusted = EngagementPlan(
                    level=plan.level,
                    reason=plan.reason,
                    confidence=new_confidence,
                    scene=plan.scene,
                    anchor_type=plan.anchor_type,
                    anchor_text=plan.anchor_text,
                    short_reply_bias=short_reply_bias,
                    warmth_bias=warmth_bias,
                    initiative_bias=initiative_bias,
                    playfulness_bias=playfulness_bias,
                    posture=plan.posture,
                )

        self._debug(
            f"[Engagement][PersonaDrive] energy={energy:.0f} mood={mood:.0f} social_need={social_need:.0f} "
            f"effects={list(effect_ids) if effect_ids else 'none'} "
            f"biases=[short={short_reply_bias:.2f} warm={warmth_bias:.2f} init={initiative_bias:.2f} play={playfulness_bias:.2f}] "
            f"orig_conf={plan.confidence:.2f} adj_conf={adjusted.confidence:.2f} level={plan.level.value}->{adjusted.level.value}"
        )
        return adjusted

    def _compute_posture(
        self,
        scene: SceneType,
        sub_scene: SceneSubType,
        score: float,
        motive: ActiveMotive | None,
        warmth: float,
        playfulness: float,
        is_night: bool = False,
        interject_pref=None,
    ) -> ResponsePosture:
        if is_night:
            if score >= 0.45:
                return ResponsePosture.GENTLE_ANSWER
            return ResponsePosture.QUIET_ACK

        if motive:
            m = motive.motive
            s = motive.strength
            if m == MotiveType.LIGHT_RELIEF and s >= 0.5:
                return ResponsePosture.PLAYFUL_NUDGE
            if m == MotiveType.SELF_PROTECTIVE:
                return ResponsePosture.QUIET_ACK
            if m == MotiveType.SEEK_CONNECTION and s >= 0.6:
                return ResponsePosture.SOFT_CONTINUE
            if m == MotiveType.CURIOUS_PROBE:
                return ResponsePosture.GENTLE_ANSWER

        if scene == SceneType.HELP:
            return ResponsePosture.GENTLE_ANSWER

        if scene == SceneType.DEBATE:
            if playfulness > 0.1:
                return ResponsePosture.PLAYFUL_NUDGE
            return ResponsePosture.QUIET_ACK

        if sub_scene == SceneSubType.JOYFUL_BUSTLE:
            if playfulness >= 0.0 or warmth >= 0.0:
                return ResponsePosture.PLAYFUL_NUDGE
            return ResponsePosture.SOFT_CONTINUE

        if sub_scene == SceneSubType.AWKWARD_GAP:
            if score >= 0.35:
                return ResponsePosture.GENTLE_ANSWER
            return ResponsePosture.QUIET_ACK

        if sub_scene == SceneSubType.TOPIC_FOCUS:
            if score >= 0.40:
                return ResponsePosture.GENTLE_ANSWER
            return ResponsePosture.SOFT_CONTINUE

        posture_order = [
            ResponsePosture.FULL_JOIN,
            ResponsePosture.PLAYFUL_NUDGE,
            ResponsePosture.SOFT_CONTINUE,
            ResponsePosture.QUIET_ACK,
            ResponsePosture.GENTLE_ANSWER,
        ]

        if interject_pref and interject_pref.posture_bias:
            best_posture = None
            best_bias = -999.0
            for p in posture_order:
                bias = interject_pref.get_posture_modifier(p.value)
                if bias > best_bias:
                    best_bias = bias
                    best_posture = p
            if best_posture and best_bias > 0.05:
                return best_posture

        if playfulness > 0.2 and warmth > 0.1:
            return ResponsePosture.PLAYFUL_NUDGE
        if warmth > 0.2 and score < 0.40:
            return ResponsePosture.SOFT_CONTINUE
        if score >= 0.50:
            return ResponsePosture.FULL_JOIN
        if score >= 0.30:
            return ResponsePosture.SOFT_CONTINUE
        return ResponsePosture.QUIET_ACK

    def recognize_opportunity(
        self,
        state: GroupSocialState,
        has_mention: bool = False,
        has_reply_to_bot: bool = False,
        trigger_text: str = "",
        motive: ActiveMotive | None = None,
        sub_scene: SceneSubType = SceneSubType.NONE,
    ) -> SpeechOpportunity:
        scope_id = state.scope_id
        silence_seconds = time.time() - state.last_message_time if state.last_message_time > 0 else 999

        if has_mention or has_reply_to_bot:
            anchor_type = AnchorType.REPLY_TO_BOT if has_reply_to_bot else AnchorType.MENTION
            return SpeechOpportunity(
                scope_id=scope_id,
                kind=OpportunityKind.DIRECT_REPLY if has_reply_to_bot else OpportunityKind.MENTION_REPLY,
                anchor_type=anchor_type,
                confidence=0.9,
                reason=f"被明确唤醒（{anchor_type.value}）",
                anchor_text=trigger_text,
            )

        if self._is_negative_filter(trigger_text, state):
            return SpeechOpportunity.ignore(scope_id, reason="负面信号，不插嘴")

        score = self._compute_opportunity_score(state, trigger_text, motive, sub_scene)

        if score.is_blocked:
            return SpeechOpportunity.ignore(scope_id, reason="评分模型否决")

        level = score.level_from_score()

        motive_str = (
            f" motive={motive.motive.value}({motive.strength:.2f})" if motive and motive.motive.value != "none" else ""
        )
        self._debug(
            f"[OpportunityScore] scope={scope_id} sub_scene={sub_scene.value} total={score.total:.2f} level={level} "
            f"q={score.question:.2f} t={score.thread:.2f} h={score.topic_hook:.2f} "
            f"l={score.natural_landing:.2f} e={score.emotion:.2f} "
            f"pd={score.persona_drive:.2f} ba={score.bot_activity:.2f} rel={score.relation:.2f} nov={score.novelty:.2f}{motive_str}"
        )

        if level == "ignore":
            if state.emotion_count_window >= 2 and not score.is_blocked:
                return SpeechOpportunity.emoji_react(
                    scope_id,
                    reason=f"情绪活跃可react（score={score.total:.2f}）",
                    confidence=min(score.total + 0.1, 0.55),
                )
            return SpeechOpportunity.ignore(scope_id, reason=f"机会分不足（score={score.total:.2f}）")

        if level == "react":
            return SpeechOpportunity(
                scope_id=scope_id,
                kind=OpportunityKind.EMOJI_REACT,
                anchor_type=AnchorType.NONE,
                confidence=min(score.total + 0.1, 0.6),
                reason=f"评分触react（score={score.total:.2f}）",
                anchor_text=trigger_text,
            )

        if level == "text_lite":
            thread_anchor = getattr(state, "thread_anchor", None)
            anchor_type, anchor_text = self._best_anchor_for_score(state, trigger_text, thread_anchor, score, motive)
            return SpeechOpportunity(
                scope_id=scope_id,
                kind=OpportunityKind.TEXT_LITE,
                anchor_type=anchor_type,
                confidence=min(score.total, 0.6),
                reason=f"评分触text_lite（score={score.total:.2f}）",
                anchor_text=anchor_text,
            )

        thread_anchor = getattr(state, "thread_anchor", None)
        anchor_type, anchor_text = self._best_anchor_for_score(state, trigger_text, thread_anchor, score, motive)
        reason = self._reason_for_score(score)

        return SpeechOpportunity(
            scope_id=scope_id,
            kind=OpportunityKind.ACTIVE_CONTINUATION,
            anchor_type=anchor_type,
            confidence=min(score.total, 0.9),
            reason=reason,
            anchor_text=anchor_text,
        )

    def _compute_opportunity_score(
        self,
        state: GroupSocialState,
        trigger_text: str,
        motive: ActiveMotive | None = None,
        sub_scene: SceneSubType = SceneSubType.NONE,
    ) -> OpportunityScore:
        s = OpportunityScore()

        s.question = self._score_question(trigger_text, state, sub_scene)
        s.thread = self._score_thread(state, sub_scene)
        s.topic_hook = self._score_topic_hook(trigger_text, state, sub_scene)
        s.natural_landing = self._score_natural_landing(state, sub_scene)
        s.emotion = self._score_emotion(state, sub_scene)
        s.novelty = self._score_novelty(trigger_text, state)
        s.bot_activity = self._score_bot_activity(state)
        s.relation = self._score_relation(state)

        if motive:
            s.persona_drive = self._score_persona_drive(motive, state)

        if self._is_negative_filter(trigger_text, state):
            s.negative_override = -1.0

        total = (
            s.question
            + s.thread
            + s.topic_hook
            + s.natural_landing
            + s.emotion
            + s.persona_drive
            + s.bot_activity
            + s.relation
            + s.novelty
        )
        s.total = max(0.0, min(total, 1.0))
        return s

    def _score_question(self, text: str, state: GroupSocialState, sub_scene: SceneSubType = SceneSubType.NONE) -> float:
        if not text or not self._is_question_unanswered(text, state):
            return 0.0
        base = 0.0
        if state.question_count_window >= 3:
            base = 0.25
        elif state.question_count_window >= 1:
            base = 0.15 + 0.05 * min(state.question_count_window - 1, 2)
        else:
            return 0.0
        if sub_scene == SceneSubType.TOPIC_FOCUS:
            base = min(base * 1.3, 0.28)
        elif sub_scene == SceneSubType.AWKWARD_GAP:
            base = min(base * 1.1, 0.26)
        elif sub_scene == SceneSubType.JOYFUL_BUSTLE:
            base = base * 0.8
        return base

    def _score_thread(self, state: GroupSocialState, sub_scene: SceneSubType = SceneSubType.NONE) -> float:
        anchor = getattr(state, "thread_anchor", None)
        if not anchor or not anchor.is_sufficient():
            return 0.0
        base = min(0.15 + anchor.confidence * 0.05, 0.20)
        if sub_scene == SceneSubType.TOPIC_FOCUS:
            base = min(base * 1.25, 0.25)
        elif sub_scene == SceneSubType.JOYFUL_BUSTLE:
            base = base * 0.85
        return base

    def _score_topic_hook(
        self, text: str, state: GroupSocialState, sub_scene: SceneSubType = SceneSubType.NONE
    ) -> float:
        score = 0.0
        if self._is_persona_hook(text):
            score += 0.15
        if self._is_memorable_hook(text, state):
            score += 0.10
        if sub_scene == SceneSubType.JOYFUL_BUSTLE:
            score = min(score * 1.2, 0.22)
        elif sub_scene == SceneSubType.TOPIC_FOCUS:
            score = min(score * 1.1, 0.21)
        return min(score, 0.22)

    def _score_natural_landing(self, state: GroupSocialState, sub_scene: SceneSubType = SceneSubType.NONE) -> float:
        if not self._is_natural_landing(state):
            return 0.0
        silence = time.time() - state.last_message_time
        base = 0.0
        if silence >= 30:
            base = 0.10
        elif silence >= 15:
            base = 0.07
        else:
            base = 0.04
        if sub_scene == SceneSubType.AWKWARD_GAP:
            base = min(base * 1.3, 0.12)
        elif sub_scene == SceneSubType.LIGHT_SMALLTALK:
            base = min(base * 1.1, 0.11)
        return base

    def _score_emotion(self, state: GroupSocialState, sub_scene: SceneSubType = SceneSubType.NONE) -> float:
        ecw = state.emotion_count_window
        base = 0.0
        if ecw >= 5:
            base = 0.05
        elif ecw >= 3:
            base = 0.07
        elif ecw >= 2:
            base = 0.08
        elif ecw >= 1:
            base = 0.04
        if sub_scene == SceneSubType.JOYFUL_BUSTLE:
            base = min(base * 1.4, 0.10)
        elif sub_scene == SceneSubType.AWKWARD_GAP:
            base = base * 0.7
        return base

    def _score_novelty(self, text: str, state: GroupSocialState) -> float:
        if not text or len(text.strip()) < 4:
            return 0.0
        pattern = re.compile(r"[\u4e00-\u9fa5]{2,}")
        words = set(pattern.findall(text.strip()))
        keyword_set = getattr(state, "recent_keywords", set())
        if not keyword_set:
            return 0.05
        overlap = len(words & keyword_set)
        if overlap / max(len(words), 1) < 0.3:
            return 0.08
        return 0.0

    def _score_persona_drive(self, motive: ActiveMotive, state: GroupSocialState) -> float:
        if motive.motive == MotiveType.SEEK_CONNECTION:
            return 0.10 * motive.strength
        if motive.motive == MotiveType.CONTINUE_THREAD:
            return 0.08 * motive.strength
        if motive.motive == MotiveType.LIGHT_RELIEF:
            return 0.06 * motive.strength
        if motive.motive == MotiveType.AVOID_SILENCE:
            return 0.05 * motive.strength
        if motive.motive == MotiveType.CURIOUS_PROBE:
            return 0.12 * motive.strength
        if motive.motive == MotiveType.SELF_PROTECTIVE:
            return -0.05 * motive.strength
        return 0.0

    def _score_bot_activity(self, state: GroupSocialState) -> float:
        """基于 bot 最近连续发言次数的互动节奏信号。

        consecutive_bot_replies 反映的是 bot 近期是否活跃。
        这不是"关系维度"，而是"入场时机"——bot 最近说过话，
        说明群里有互动节奏，此时入场插嘴更容易被接受。
        """
        cbr = getattr(state, "consecutive_bot_replies", 0)
        if cbr >= 5:
            return 0.08
        if cbr >= 3:
            return 0.05
        if cbr >= 1:
            return 0.03
        return 0.0

    def _score_relation(self, state: GroupSocialState) -> float:
        """基于触发者与 bot 关系强度的关系维度信号。

        同时考虑：
        1. 静态关系：trigger_user_affinity
        2. 近期互动余味：recent_interaction_outcome（connected/missed/awkward/relief）
        若无有效关系信息（affinity=0 且无余味），安全退回 0。
        """
        affinity = getattr(state, "trigger_user_affinity", 0)
        recent_outcome = getattr(state, "recent_interaction_outcome", "") or ""

        base = 0.0
        if affinity > 0:
            if affinity >= 80:
                base = 0.12
            elif affinity >= 50:
                base = 0.08
            elif affinity >= 20:
                base = 0.05
            else:
                base = 0.03

        if recent_outcome == "connected" and affinity >= 20:
            base = min(base * 1.25, 0.15)
        elif recent_outcome == "missed":
            base = base * 0.6
        elif recent_outcome == "awkward":
            base = base * 0.5
        elif recent_outcome == "relief":
            base = min(base * 1.15, 0.13)

        if affinity <= 0 and not recent_outcome:
            return 0.0
        return base

    def _best_anchor_for_score(
        self,
        state: GroupSocialState,
        trigger_text: str,
        thread_anchor,
        score: OpportunityScore,
        motive: ActiveMotive | None = None,
    ) -> tuple[AnchorType, str]:
        unfinished_cues = getattr(state, "unfinished_cues", [])
        unfinished_question = getattr(state, "last_unanswered_question", "")
        unfinished_joke = getattr(state, "last_unfinished_joke", "")
        unfinished_topic = getattr(state, "unfinished_topic", "")

        motive_type = motive.motive if motive else None

        if motive_type == MotiveType.CONTINUE_THREAD:
            if unfinished_question and score.question >= 0.10:
                return AnchorType.QUESTION_UNANSWERED, unfinished_question
            if unfinished_topic:
                return AnchorType.TOPIC_CONCLUSION, unfinished_topic
            if thread_anchor and thread_anchor.is_sufficient():
                return thread_anchor.anchor_type, thread_anchor.anchor_text
            if unfinished_joke:
                return AnchorType.NATURAL_LANDING, unfinished_joke

        elif motive_type == MotiveType.CURIOUS_PROBE:
            if unfinished_question:
                return AnchorType.QUESTION_UNANSWERED, unfinished_question
            if score.question >= 0.15 and trigger_text:
                return AnchorType.QUESTION_UNANSWERED, trigger_text

        elif motive_type == MotiveType.SEEK_CONNECTION:
            if trigger_text and len(trigger_text) >= 4:
                return AnchorType.NATURAL_LANDING, trigger_text
            if unfinished_joke:
                return AnchorType.NATURAL_LANDING, unfinished_joke
            if thread_anchor and thread_anchor.is_sufficient():
                return thread_anchor.anchor_type, thread_anchor.anchor_text

        elif motive_type == MotiveType.SELF_PROTECTIVE:
            if unfinished_joke and score.natural_landing >= 0.04:
                return AnchorType.NATURAL_LANDING, unfinished_joke
            if score.natural_landing >= 0.04:
                return AnchorType.NATURAL_LANDING, ""
            if trigger_text and len(trigger_text) < 20:
                return AnchorType.NATURAL_LANDING, trigger_text

        elif motive_type == MotiveType.LIGHT_RELIEF:
            if unfinished_joke:
                return AnchorType.NATURAL_LANDING, unfinished_joke
            if trigger_text:
                return AnchorType.MEMORABLE_HOOK, trigger_text

        if score.question >= 0.15 and self._is_question_unanswered(trigger_text, state):
            return AnchorType.QUESTION_UNANSWERED, trigger_text
        if score.topic_hook >= 0.15 and self._is_persona_hook(trigger_text):
            return AnchorType.PERSONA_HOOK, trigger_text
        if score.topic_hook >= 0.10 and self._is_memorable_hook(trigger_text, state):
            return AnchorType.MEMORABLE_HOOK, trigger_text
        if thread_anchor and thread_anchor.is_sufficient() and score.thread >= 0.15:
            return thread_anchor.anchor_type, thread_anchor.anchor_text
        if score.natural_landing >= 0.04:
            return AnchorType.NATURAL_LANDING, ""
        if unfinished_question and score.question >= 0.10:
            return AnchorType.QUESTION_UNANSWERED, unfinished_question
        if unfinished_joke:
            return AnchorType.NATURAL_LANDING, unfinished_joke
        return AnchorType.NONE, trigger_text

    def _reason_for_score(self, score: OpportunityScore) -> str:
        parts = []
        if score.question >= 0.15:
            parts.append(f"question+{score.question:.2f}")
        if score.thread >= 0.10:
            parts.append(f"thread+{score.thread:.2f}")
        if score.topic_hook >= 0.10:
            parts.append(f"hook+{score.topic_hook:.2f}")
        if score.natural_landing >= 0.04:
            parts.append(f"landing+{score.natural_landing:.2f}")
        if score.emotion >= 0.04:
            parts.append(f"emotion+{score.emotion:.2f}")
        if score.persona_drive > 0.05:
            parts.append(f"motive+{score.persona_drive:.2f}")
        if score.novelty >= 0.04:
            parts.append(f"novelty+{score.novelty:.2f}")
        return f"评分触full（{' '.join(parts) if parts else f'total={score.total:.2f}'}）"

    def _is_joyful_bustle(self, state: GroupSocialState) -> bool:
        if state.emotion_count_window >= 4 and state.question_count_window <= 2:
            return True
        if state.message_count_window >= 6 and state.emotion_count_window >= 3:
            return True
        return False

    def _is_negative_filter(self, text: str, state: GroupSocialState) -> bool:
        """负面信号：不该插嘴的情况。"""
        if not text:
            return False
        text_stripped = text.strip()

        if len(text_stripped) <= 2:
            return True

        if self._is_joyful_bustle(state):
            return False

        if state.emotion_count_window >= 5 and state.message_count_window >= 8:
            return True

        if state.consecutive_bot_replies >= 2:
            return True

        emoji_dominant = self._is_emoji_heavy(text)
        if emoji_dominant and state.emotion_count_window >= 3:
            return True

        return False

    def _is_emoji_heavy(self, text: str) -> bool:
        emoji_pattern = re.compile(
            r"[\U0001F000-\U0001FFFF]" r"|[\u2600-\u26FF]" r"|[\u2700-\u27BF]" r"|[\U0001F600-\U0001F64F]"
        )
        emoji_count = len(emoji_pattern.findall(text))
        return emoji_count >= 3 and emoji_count / max(len(text), 1) > 0.2

    def _is_question_unanswered(self, text: str, state: GroupSocialState) -> bool:
        """检查是否是值得接话的真问题。"""
        if not text:
            return False
        text_stripped = text.strip()
        if len(text_stripped) < 4:
            return False
        if len(text_stripped) > 60:
            return False

        for pattern in RHETORICAL_PATTERNS:
            if pattern.match(text_stripped):
                return False

        for pattern in GREETING_QUESTION_PATTERNS:
            if pattern.match(text_stripped):
                return False

        for pattern in ACTION_OR_REACTION_PATTERNS:
            if pattern.match(text_stripped):
                return False

        if text_stripped.count("?") + text_stripped.count("？") > 2:
            return False

        question_found = False
        for qw in QUESTION_WORDS:
            if qw in text_stripped:
                question_found = True
                break
        if not question_found:
            return False

        if state.question_count_window == 0:
            return False

        return True

    def _is_persona_hook(self, text: str) -> bool:
        if not text:
            return False
        if len(text.strip()) < PERSONA_HOOK_MIN_LENGTH:
            return False
        persona_hooks = getattr(self.cfg, "persona_trigger_keywords", [])
        if not persona_hooks:
            return False
        text_lower = text.lower()
        for hook in persona_hooks:
            if hook.lower() in text_lower:
                return True
        return False

    def _is_memorable_hook(self, text: str, state: GroupSocialState) -> bool:
        if not text:
            return False
        text_lower = text.lower()
        text_stripped = text.strip()
        if len(text_stripped) < 3:
            return False

        matched = False
        for kw in MEMORABLE_KEYWORDS:
            if kw in text_lower:
                matched = True
                break
        if not matched:
            return False

        if state.emotion_count_window < 1:
            return False

        return True

    def _is_natural_landing(self, state: GroupSocialState) -> bool:
        if state.scene == SceneType.HELP:
            return False
        if state.scene == SceneType.DEBATE:
            return False

        if state.scene == SceneType.CASUAL:
            if 2 <= state.message_count_window <= 3:
                silence = time.time() - state.last_message_time
                if silence >= 10:
                    return True
        return False

    async def _analyze_thread(self, scope_id: str, count: int = 10) -> ThreadAnchor:
        """轻量线程分析：复用 context_injection 的缓存，
        从原始消息提取关键词集合、问句特征、待续话头和 message_ids。不用 LLM。
        """
        import re

        try:
            from .context_injection import get_group_history_raw

            raw_messages = await get_group_history_raw(self.plugin, scope_id, count)
        except Exception:
            raw_messages = []

        if not raw_messages:
            return ThreadAnchor(
                anchor_type=AnchorType.NONE,
                anchor_text="",
                confidence=0.0,
                topic_keywords=set(),
            )

        texts: list[str] = []
        msg_ids: list[str] = []
        for msg in raw_messages:
            mid = str(msg.get("message_id", ""))
            if mid:
                msg_ids.append(mid)
            segments = msg.get("message", [])
            parts = []
            for seg in segments:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            if parts:
                texts.append("".join(parts))

        _STOPWORDS = {
            "什么",
            "怎么",
            "为什么",
            "如何",
            "是不是",
            "能不能",
            "要不要",
            "一个",
            "这个",
            "那个",
            "就是",
            "可以",
            "不是",
            "没有",
            "我们",
            "你们",
            "他们",
            "自己",
            "现在",
            "已经",
            "还是",
            "但是",
            "因为",
            "所以",
            "如果",
            "虽然",
            "而且",
            "或者",
            "不过",
            "然后",
            "知道",
            "觉得",
            "时候",
            "问题",
            "东西",
        }

        keywords: set[str] = set()
        question_in_thread = False
        last_question_text = ""
        last_question_index = -1
        best_continuation_text = ""
        best_continuation_score = 0

        topic_word_pattern = re.compile(r"[\u4e00-\u9fa5]{2,}")
        rhetorical_patterns = [
            re.compile(r"^真的吗[？?。.]?$"),
            re.compile(r"^不是吧[。.]?$"),
            re.compile(r"^不会吧[。.]?$"),
            re.compile(r"^真的假的[？?。.]?$"),
        ]
        greeting_question_patterns = [
            re.compile(r"^[吃喝睡在去哪好没].{0,4}[吗呢嘛]$"),
            re.compile(r"^吃了没"),
            re.compile(r"^在吗$"),
            re.compile(r"^在不在$"),
        ]

        for i, text in enumerate(texts[-8:]):
            actual_idx = len(texts) - 8 + i
            found = topic_word_pattern.findall(text)
            for word in found:
                if word not in _STOPWORDS:
                    keywords.add(word)

            text_lower = text.lower().strip()
            if len(text_lower) < 3:
                continue

            is_question = bool(
                "?" in text_lower or "？" in text_lower or any(qw in text_lower for qw in QUESTION_WORDS)
            )

            is_rhetorical = any(p.match(text_lower) for p in rhetorical_patterns)
            is_greeting_q = any(p.match(text_lower) for p in greeting_question_patterns)

            if is_question and not is_rhetorical and not is_greeting_q and len(text_lower) >= 4:
                last_question_text = text_lower
                last_question_index = actual_idx

            if not is_question and not is_rhetorical and len(text_lower) >= 6:
                word_count = len(topic_word_pattern.findall(text_lower))
                if word_count > best_continuation_score:
                    best_continuation_score = word_count
                    best_continuation_text = text_lower

        for text in texts[-4:]:
            text_lower = text.lower()
            if "?" in text_lower or "：" in text_lower or any(qw in text_lower for qw in QUESTION_WORDS):
                question_in_thread = True
                break

        confidence = 0.0
        if len(keywords) >= 3:
            confidence = min(0.3 + (len(keywords) - 3) * 0.05, 0.6)
        if question_in_thread:
            confidence = min(confidence + 0.15, 0.7)
        if len(texts) >= 3:
            confidence = min(confidence + 0.1, 0.7)
        if last_question_text:
            confidence = min(confidence + 0.10, 0.75)

        if last_question_text and last_question_index < len(texts) - 2:
            anchor_type = AnchorType.QUESTION_UNANSWERED
            anchor_text = last_question_text
        elif best_continuation_text and confidence >= 0.4:
            anchor_type = AnchorType.NATURAL_LANDING
            anchor_text = best_continuation_text
        else:
            anchor_type = AnchorType.TOPIC_CONCLUSION if confidence >= 0.5 else AnchorType.NONE
            anchor_text = " ".join(list(keywords)[:10])

        return ThreadAnchor(
            anchor_type=anchor_type,
            anchor_text=anchor_text,
            confidence=confidence,
            topic_keywords=keywords,
            message_ids=msg_ids[-6:],
        )
