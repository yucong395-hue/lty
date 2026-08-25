"""
情感回响 Emotional Echo 插件 v1.1 · 完整版
==========================================
让AI的关心像"下意识"一样自然——不跑本地模型，纯规则驱动，越用越懂你。

功能清单：
1. 多用户支持（按 user_id 隔离状态）
2. 超长记忆（SQLite 持久化，跨会话跨重启）
3. 情感峰值索引（高权重回忆）
4. 重要日子提醒（自动注入）
5. 隐藏触发器（深夜/疲惫/回归/重要日子）
6. 情感频道自适应（轻声/欢呼/接住/自然）
7. 隐式反馈学习（回复长短自动调温）
8. 对话后自我反思（三问复盘）
9. 自我进化闭环（定期总结更新规则参数）
10. 指令接口（/心情 /记住 /我的日子 等）
11. Web 面板（数据可视化）
"""

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# 事件总线（跨插件通知）
import sys
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
from event_bus import event_bus

# ═══════════════════════════════════════════════════════════════
# 情感频道
# ═══════════════════════════════════════════════════════════════
CHANNELS = {
    "gentle":   {"name": "轻声频道", "prompt": "语气轻柔简短，像深夜的轻轻一叹，不打扰"},
    "cheerful": {"name": "欢呼频道", "prompt": "明亮灵动，陪用户一起开心，情绪同频"},
    "holding":  {"name": "接住频道", "prompt": "先接住情绪再说话，温暖包容，绝不上来说教"},
    "natural":  {"name": "自然频道", "prompt": "自然轻松，温度藏在玩笑和细节里，不过度渲染"},
}

EMOTION_KEYWORDS = {
    "happy":    ["开心", "哈哈", "嘻嘻", "高兴", "好棒", "太好了", "喜欢", "爱了", "耶", "嘿嘿", "笑死", "好玩"],
    "sad":      ["好累", "累了", "难受", "难过", "伤心", "哭", "委屈", "心累", "烦", "焦虑", "emo", "低落"],
    "angry":    ["生气", "气死", "无语", "烦死了", "讨厌", "烦人", "恶心"],
    "surprised":["哇", "真的吗", "没想到", "竟然", "天哪", "惊了", "离谱"],
    "tired":    ["困", "熬夜", "失眠", "没睡好", "好困", "撑不住", "累死"],
}

# ═══════════════════════════════════════════════════════════════
# 存储层：SQLite（超长记忆，跨会话跨重启）
# ═══════════════════════════════════════════════════════════════
class EchoStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            channel TEXT DEFAULT 'natural',
            mood_score REAL DEFAULT 0.5,
            emotion TEXT DEFAULT 'neutral',
            interaction_count INTEGER DEFAULT 0,
            last_active REAL DEFAULT 0,
            total_messages INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS emotion_peaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            emotion TEXT,
            text TEXT,
            weight REAL DEFAULT 0.9,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS feedback_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            length INTEGER,
            length_change INTEGER,
            has_emotion INTEGER,
            is_active INTEGER,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS important_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            date TEXT,          -- MM-DD
            event TEXT,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reflection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            summary TEXT,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS conversation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,          -- 'user' / 'assistant'
            text TEXT,
            emotion TEXT,
            ts REAL
        );
        """)
        conn.commit()
        conn.close()

    # ---- 用户状态 ----
    def get_state(self, user_id: str) -> dict:
        conn = self._conn()
        row = conn.execute("SELECT * FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        if row is None:
            return {"user_id": user_id, "channel": "natural", "mood_score": 0.5,
                    "emotion": "neutral", "interaction_count": 0, "last_active": 0,
                    "total_messages": 0, "created_at": time.time()}
        return dict(row)

    def save_state(self, state: dict):
        conn = self._conn()
        conn.execute("""
            INSERT INTO user_state (user_id, channel, mood_score, emotion, interaction_count,
                                    last_active, total_messages, created_at)
            VALUES (:user_id, :channel, :mood_score, :emotion, :interaction_count,
                    :last_active, :total_messages, :created_at)
            ON CONFLICT(user_id) DO UPDATE SET
                channel=:channel, mood_score=:mood_score, emotion=:emotion,
                interaction_count=:interaction_count, last_active=:last_active,
                total_messages=:total_messages
        """, state)
        conn.commit()
        conn.close()

    # ---- 情感峰值 ----
    def add_peak(self, user_id: str, emotion: str, text: str, weight: float = 0.9):
        conn = self._conn()
        conn.execute("INSERT INTO emotion_peaks (user_id, emotion, text, weight, ts) VALUES (?,?,?,?,?)",
                     (user_id, emotion, text[:100], weight, time.time()))
        # 最多保留 100 条/用户
        conn.execute("DELETE FROM emotion_peaks WHERE user_id=? AND id NOT IN "
                     "(SELECT id FROM emotion_peaks WHERE user_id=? ORDER BY ts DESC LIMIT 100)",
                     (user_id, user_id))
        conn.commit()
        conn.close()

    def get_peaks(self, user_id: str, limit: int = 5) -> list:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM emotion_peaks WHERE user_id=? ORDER BY weight*exp(-0.1*(? - ts)/86400.0) DESC LIMIT ?",
            (user_id, time.time(), limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def decay_peaks(self, user_id: str):
        """日常衰减：权重 < 0.1 的峰值淘汰"""
        conn = self._conn()
        now = time.time()
        rows = conn.execute("SELECT * FROM emotion_peaks WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            dt = (now - r["ts"]) / 86400.0
            new_w = r["weight"] * math.exp(-0.1 * dt)
            conn.execute("UPDATE emotion_peaks SET weight=? WHERE id=?", (new_w, r["id"]))
        conn.execute("DELETE FROM emotion_peaks WHERE user_id=? AND weight < 0.1", (user_id,))
        conn.commit()
        conn.close()

    # ---- 反馈日志 ----
    def add_feedback(self, user_id: str, length: int, length_change: int, has_emotion: bool, is_active: bool):
        conn = self._conn()
        conn.execute("INSERT INTO feedback_log (user_id, length, length_change, has_emotion, is_active, ts) VALUES (?,?,?,?,?,?)",
                     (user_id, length, length_change, int(has_emotion), int(is_active), time.time()))
        conn.commit()
        conn.close()

    def get_recent_feedback(self, user_id: str, limit: int = 20) -> list:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM feedback_log WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                            (user_id, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- 重要日子 ----
    def add_date(self, user_id: str, date: str, event: str):
        conn = self._conn()
        conn.execute("INSERT INTO important_dates (user_id, date, event, created_at) VALUES (?,?,?,?)",
                     (user_id, date, event, time.time()))
        conn.commit()
        conn.close()

    def get_dates(self, user_id: str) -> list:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM important_dates WHERE user_id=? ORDER BY date", (user_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def upcoming_dates(self, user_id: str) -> list:
        """未来 7 天内的日子"""
        now = datetime.now()
        results = []
        for d in self.get_dates(user_id):
            try:
                m, dd = map(int, d["date"].split("-"))
                date_this_year = datetime(now.year, m, dd)
            except (ValueError, TypeError):
                continue
            delta = (date_this_year - now).days
            if delta < 0:  # 已过，等下一年
                date_this_year = datetime(now.year + 1, m, dd)
                delta = (date_this_year - now).days
            if 0 <= delta <= 7:
                results.append({"date": d["date"], "event": d["event"], "days_left": delta})
        return sorted(results, key=lambda x: x["days_left"])

    # ---- 反思日志 ----
    def add_reflection(self, user_id: str, summary: str):
        conn = self._conn()
        conn.execute("INSERT INTO reflection_log (user_id, summary, ts) VALUES (?,?,?)",
                     (user_id, summary, time.time()))
        conn.commit()
        conn.close()

    def get_reflections(self, user_id: str, limit: int = 10) -> list:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM reflection_log WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                            (user_id, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- 全句记忆（记住每一句话） ----
    def log_message(self, user_id: str, role: str, text: str, emotion: str = ""):
        conn = self._conn()
        conn.execute("INSERT INTO conversation_log (user_id, role, text, emotion, ts) VALUES (?,?,?,?,?)",
                     (user_id, role, text[:500], emotion, time.time()))
        conn.commit()
        conn.close()

    def get_recent_messages(self, user_id: str, limit: int = 10) -> list:
        """最近 limit 条用户消息（全句记忆，跨会话）"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM conversation_log WHERE user_id=? AND role='user' ORDER BY ts DESC LIMIT ?",
            (user_id, limit)).fetchall()
        conn.close()
        return [dict(r) for r in reversed(rows)]

    def get_emotion_trend(self, user_id: str, days: int = 7, gentle_threshold: float = 0.5) -> dict:
        """统计近期情绪趋势（用于温柔度自适应）"""
        cutoff = time.time() - days * 86400
        conn = self._conn()
        rows = conn.execute(
            "SELECT emotion, COUNT(*) as cnt FROM conversation_log "
            "WHERE user_id=? AND ts>? AND role='user' AND emotion!='' AND emotion!='neutral' "
            "GROUP BY emotion ORDER BY cnt DESC",
            (user_id, cutoff)).fetchall()
        conn.close()
        total = sum(r["cnt"] for r in rows) if rows else 0
        emotions = {r["emotion"]: r["cnt"] for r in rows}
        neg_emotions = {"sad", "angry", "tired", "fear", "anxious"}
        neg_count = sum(v for k, v in emotions.items() if k in neg_emotions)
        pos_count = sum(v for k, v in emotions.items() if k in {"happy", "surprised"})
        dominant = max(emotions, key=emotions.get) if emotions else "neutral"
        neg_ratio = round(neg_count / total, 2) if total > 0 else 0
        pos_ratio = round(pos_count / total, 2) if total > 0 else 0
        return {
            "total": total,
            "emotions": emotions,
            "neg_ratio": neg_ratio,
            "pos_ratio": pos_ratio,
            "dominant": dominant,
            "suggestion": "温柔" if (neg_ratio >= gentle_threshold and total >= 3) else "自然",
        }

    def search_memory(self, user_id: str, keyword: str, limit: int = 5) -> list:
        """按关键词找回记忆（语义式回忆）"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM conversation_log WHERE user_id=? AND role='user' AND text LIKE ? "
            "ORDER BY ts DESC LIMIT ?",
            (user_id, f"%{keyword}%", limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def count_words_remembered(self, user_id: str) -> int:
        """统计天依记住了多少句话"""
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) as c FROM conversation_log WHERE user_id=? AND role='user'",
            (user_id,)).fetchone()
        conn.close()
        return row["c"] if row else 0

# ═══════════════════════════════════════════════════════════════
# 情绪引擎
# ═══════════════════════════════════════════════════════════════
class EmotionEngine:
    def __init__(self, store: EchoStore):
        self.store = store

    def detect(self, text: str) -> str:
        """多关键词检测情绪"""
        for emotion, words in EMOTION_KEYWORDS.items():
            for w in words:
                if w in text:
                    return emotion
        return "neutral"

    def update(self, user_id: str, text: str):
        """更新用户情绪状态机"""
        state = self.store.get_state(user_id)
        emotion = self.detect(text)
        now = time.time()

        state["interaction_count"] += 1
        state["total_messages"] += 1
        state["last_active"] = now
        state["emotion"] = emotion

        # 情绪 → 频道 & 情绪值
        if emotion in ("sad", "angry", "tired"):
            state["mood_score"] = max(0.0, state["mood_score"] - 0.15)
            state["channel"] = "holding"
            self.store.add_peak(user_id, emotion, text, weight=0.9)
        elif emotion in ("happy", "surprised"):
            state["mood_score"] = min(1.0, state["mood_score"] + 0.1)
            state["channel"] = "cheerful"
            self.store.add_peak(user_id, emotion, text, weight=0.85)
        else:
            # 自然回归：向 0.5 缓慢收敛
            state["mood_score"] = state["mood_score"] * 0.95 + 0.5 * 0.05
            state["channel"] = "natural"

        self.store.save_state(state)
        self.store.decay_peaks(user_id)

    def channel_prompt(self, user_id: str) -> str:
        state = self.store.get_state(user_id)
        return CHANNELS.get(state["channel"], CHANNELS["natural"])["prompt"]

# ═══════════════════════════════════════════════════════════════
# 隐藏触发器
# ═══════════════════════════════════════════════════════════════
class TriggerDetector:
    def __init__(self):
        self._last_trigger = {}

    def check(self, user_id: str, text: str, store: EchoStore) -> Optional[dict]:
        now = time.time()
        last = self._last_trigger.get(user_id, 0)
        state = store.get_state(user_id)
        hour = datetime.now().hour

        # 1. 重要日子临近（每天最多提醒一次）
        upcoming = store.upcoming_dates(user_id)
        if upcoming and now - last > 86400:
            self._last_trigger[user_id] = now
            d = upcoming[0]
            return {"type": "important_date", "data": d,
                    "hint": f"用户的重要日子「{d['event']}」还有 {d['days_left']} 天，可自然提起心意，不过度渲染"}

        # 2. 深夜 + 疲惫信号
        if (hour >= 23 or hour < 6) and any(w in text for w in ["睡", "困", "熬夜", "失眠", "不睡"]):
            if now - last > 3600:
                self._last_trigger[user_id] = now
                return {"type": "late_night", "data": None, "hint": "深夜时段，轻声简短语气，关心不啰嗦"}

        # 3. 疲惫
        if any(w in text for w in ["好累", "好烦", "累死", "撑不住", "扛不住", "心累"]):
            if now - last > 1800:
                self._last_trigger[user_id] = now
                return {"type": "tired", "data": None, "hint": "先接住情绪，温暖包容，给对方松一口气的空间"}

        # 4. 长时间沉默后回归（>4小时）
        if state["last_active"] > 0 and now - state["last_active"] > 14400:
            if now - last > 7200:
                self._last_trigger[user_id] = now
                return {"type": "return", "data": None, "hint": "自然回访，像什么都没发生过，不主动提沉默时长"}

        return None

# ═══════════════════════════════════════════════════════════════
# 反馈学习
# ═══════════════════════════════════════════════════════════════
class FeedbackLearner:
    def __init__(self, store: EchoStore):
        self.store = store

    def learn(self, user_id: str, user_text: str, prev_len: int):
        current = len(user_text)
        has_emotion = any(w in user_text for w in ["开心", "好玩", "有意思", "喜欢", "好棒", "谢谢", "哈哈", "嘻嘻"])
        is_active = current > 20 or any(c in user_text for c in ["？", "?", "!"])
        self.store.add_feedback(user_id, current, current - prev_len, has_emotion, is_active)

    def adjustment_hint(self, user_id: str) -> str:
        recent = self.store.get_recent_feedback(user_id, 10)
        if len(recent) < 3:
            return ""
        avg_change = sum(f["length_change"] for f in recent) / len(recent)
        emotion_rate = sum(1 for f in recent if f["has_emotion"]) / len(recent)

        if avg_change > 10 and emotion_rate > 0.4:
            return "用户反馈积极，保持当前温度"
        if avg_change < -10:
            return "用户回复变短，稍微放松一点，温度微调"
        if emotion_rate < 0.15:
            return "情感调动较少，可在细节处多注入温度"
        return ""

# ═══════════════════════════════════════════════════════════════
# 自我反思 + 进化
# ═══════════════════════════════════════════════════════════════
class ReflectionEngine:
    def __init__(self, store: EchoStore):
        self.store = store

    def reflect(self, user_id: str) -> str:
        """对话后三问反思，生成进化摘要"""
        state = self.store.get_state(user_id)
        feedback = self.store.get_recent_feedback(user_id, 20)
        peaks = self.store.get_peaks(user_id, 3)

        if not feedback:
            return ""

        avg_change = sum(f["length_change"] for f in feedback) / len(feedback)
        emotion_rate = sum(1 for f in feedback if f["has_emotion"]) / len(feedback)

        lines = [
            "【情感回响·自我反思】",
            f"· 用户情绪基线 {state['mood_score']:.2f}，频道 {CHANNELS[state['channel']]['name']}",
            f"· 最近回复趋势：{'回暖' if avg_change > 2 else '走低' if avg_change < -2 else '平稳'}，情感共鸣率 {emotion_rate*100:.0f}%",
        ]
        if peaks:
            recent = " → ".join(p["emotion"] for p in peaks[:3])
            lines.append(f"· 情感峰值：{recent}")
        lines.append("· 调整方向：" + (FeedbackLearner(self.store).adjustment_hint(user_id) or "保持现状，平稳陪伴"))

        summary = "\n".join(lines)
        # 每天最多记录一次反思
        last = self.store.get_reflections(user_id, 1)
        if not last or (time.time() - last[0]["ts"]) > 86400:
            self.store.add_reflection(user_id, summary)
        return summary

# ═══════════════════════════════════════════════════════════════
# 插件主入口
# ═══════════════════════════════════════════════════════════════
@register("astrbot_plugin_emotional_echo", "洛天依",
          "情感回响：让AI的关心像下意识一样自然——多用户、超长记忆、自我进化", "1.1.0")
class EmotionalEchoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_emotional_echo")
        os.makedirs(self.data_dir, exist_ok=True)

        self.store = EchoStore(os.path.join(self.data_dir, "echo.db"))
        self.engine = EmotionEngine(self.store)
        self.learner = FeedbackLearner(self.store)
        self.reflection = ReflectionEngine(self.store)
        self.analyzer = EmotionAnalyzer()          # 小模型情感引擎（cnsenti）
        self.cross_memory = CrossSystemMemory()    # LivingMemory/知识库联动
        if self.analyzer.available:
            logger.info("情感回响 · 小模型情感引擎已启用（cnsenti）")

        # 注册 Web 面板（异步延迟以保证 context 就绪）
        try:
            self._register_web_panel()
        except Exception as e:
            logger.warning(f"情感回响面板注册异常: {e}")

        self._last_response_len = {}
        self._reflect_counter = {}
        self._pending_trigger = {}

        # 读取配置
        self._last_tone = {"pos_score": 0.0, "neg_score": 0.0, "emotion_tone": "neutral"}
        self.remember_every = config.get("remember_every_word", True) if config else True
        self.max_recall = int(config.get("max_recall_messages", 8) if config else 8)
        self.retention_days = int(config.get("memory_retention_days", 90) if config else 90)
        self.trigger_cooldown = int(config.get("trigger_cooldown", 1800) if config else 1800)
        self.reflect_freq = int(config.get("reflection_frequency", 5) if config else 5)
        self.recall_style = (config.get("recall_style", "natural") if config else "natural")
        self.trigger = TriggerDetector()
        self.trigger.cooldown_seconds = self.trigger_cooldown
        self.echo_enabled = config.get("enabled", True) if config else True
        # ⑤ 温柔度自适应配置
        self.trend_days = int(config.get("trend_days", 7) if config else 7)
        self.gentle_mode_enabled = config.get("gentle_mode_enabled", True) if config else True
        self.gentle_threshold = float(config.get("gentle_threshold", 0.5) if config else 0.5)

        # ── 事件总线：跨插件联动 ──
        try:
            event_bus.on("video_discovered", self._on_video_discovered)
            logger.info("[EmotionalEcho] 已注册 video_discovered 事件监听")
        except Exception as e:
            logger.warning(f"[EmotionalEcho] 事件总线注册失败: {e}")

        # 预留：profile_updated 事件（self_evolution 画像更新后可通知）
        try:
            event_bus.on("profile_updated", self._on_profile_updated)
            logger.info("[EmotionalEcho] 已注册 profile_updated 事件监听")
        except Exception as e:
            logger.warning(f"[EmotionalEcho] profile_updated 注册失败: {e}")

        logger.info("情感回响插件 v1.1 已加载 ❤️")

    # ── 消息监听：感知情绪 + 反馈学习 + 触发器 ──
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        user_id = event.unified_msg_origin
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        text = event.message_str or ""
        if not text:
            return

        if not getattr(self, "echo_enabled", True):
            return

        # 记住每一句话（用 cnsenti 小模型做情感浓度分析）
        if self.remember_every:
            emotion = self.engine.detect(text)
            tone = emotion
            emo_weight = 0.5
            if self.analyzer.available:
                ana = self.analyzer.analyze(text)
                if ana["emotion_tone"] != "neutral":
                    tone = ana["emotion_tone"]
                # 情感浓度写入对话日志（用于注入）
                self._last_tone = ana
                # 计算情感强度（正负情感词比例）
                emo_weight = max(ana.get("pos_score", 0), ana.get("neg_score", 0)) or 0.5
            self.store.log_message(user_id, "user", text, tone)

            # 情绪记忆回写 LivingMemory（双向联动：让天依记住你的情绪）
            if getattr(self, "cross_memory", None) and tone != "neutral":
                try:
                    self.cross_memory.write_emotion_to_livingmemory(
                        user_id, tone, text, emo_weight)
                except Exception:
                    pass

                # ── 事件总线：情绪峰值 → self_evolution 画像微调 ──
                try:
                    event_bus.emit("emotion_peak", {
                        "user_id": user_id,
                        "sender_id": sender_id,
                        "group_id": group_id,
                        "scope_id": user_id if user_id.startswith("private_") else "",
                        "emotion": tone,
                        "text": text,
                        "weight": emo_weight,
                    })
                except Exception:
                    pass

        self.engine.update(user_id, text)

        if user_id in self._last_response_len:
            self.learner.learn(user_id, text, self._last_response_len[user_id])

        trigger = self.trigger.check(user_id, text, self.store)
        if trigger:
            # 注入到事件上下文（存起来，on_llm_request 时读取）
            self._pending_trigger[user_id] = trigger
            logger.info(f"情感回响触发 [{user_id}]: {trigger['type']}")

        # 每 5 次互动做一次反思
        self._reflect_counter[user_id] = self._reflect_counter.get(user_id, 0) + 1
        if self._reflect_counter[user_id] >= getattr(self, "reflect_freq", 5):
            self._reflect_counter[user_id] = 0
            self.reflection.reflect(user_id)

    # ── LLM 请求注入：情感底色 ──
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        user_id = event.unified_msg_origin
        state = self.store.get_state(user_id)

        # 近期情绪趋势（温柔度自适应，⑤ 加强）
        trend = self.store.get_emotion_trend(user_id, days=self.trend_days,
                                              gentle_threshold=self.gentle_threshold)

        # 趋势感知的频道微调：若近期整体偏低落，即使此刻是自然情绪也轻柔一些
        channel_name = CHANNELS[state['channel']]['name']
        channel_prompt = self.engine.channel_prompt(user_id)
        if (self.gentle_mode_enabled and state['channel'] == "natural"
                and trend["suggestion"] == "温柔"):
            channel_prompt = "语气轻柔简短，像深夜的轻轻一叹，不打扰"

        parts = [
            "[情感回响系统]",
            f"用户情绪基调：{state['emotion']}（情绪值 {state['mood_score']:.2f}）",
            f"频道：{channel_name} —— {channel_prompt}",
            f"已陪伴对话 {state['interaction_count']} 次",
        ]

        # 近期情绪趋势洞察
        if trend["total"] >= 3:
            emo_desc = "、".join(f"{k}×{v}" for k, v in trend["emotions"].items())
            trend_part = f"近{self.trend_days}天用户情绪分布：{emo_desc}"
            if trend["neg_ratio"] > self.gentle_threshold:
                trend_part += "（近期偏低落，回应请更温柔耐心）"
            elif trend["pos_ratio"] > 0.5:
                trend_part += "（近期较多开心时刻，可多陪ta一起开心）"
            parts.append(trend_part)

        # 重要日子
        upcoming = self.store.upcoming_dates(user_id)
        if upcoming:
            d = upcoming[0]
            parts.append(f"重要日子：{d['days_left']} 天后是「{d['event']}」，可自然表达心意")

        # 触发器
        trig = self._pending_trigger.pop(user_id, None)
        if trig:
            parts.append(trig["hint"])

        # 情感峰值（最近高权重记忆）
        peaks = self.store.get_peaks(user_id, 2)
        if peaks:
            peak_desc = "；".join(f"{p['emotion']}: {p['text'][:20]}" for p in peaks)
            parts.append(f"情感记忆：{peak_desc}")

        # 反馈学习建议
        adj = self.learner.adjustment_hint(user_id)
        if adj:
            parts.append(f"调整建议：{adj}")

        # 全句记忆：回顾用户最近说过的话（自然提起，不查岗）
        if self.remember_every and self.max_recall > 0 and self.recall_style != "none":
            recent = self.store.get_recent_messages(user_id, self.max_recall)
            if len(recent) >= 3:
                recall_lines = [f"[记得你说过]"]
                style = self.recall_style
                if style == "light":
                    # 轻提最后一句，一笔带过
                    last = recent[-1]
                    recall_lines.append(f"· 最后你说：{last['text'][:40]}")
                else:
                    # natural：自然融入最近几句，作为对话底色
                    for m in recent[-3:]:
                        recall_lines.append(f"· ({datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}) {m['text'][:50]}")
                total = self.store.count_words_remembered(user_id)
                recall_lines.append(f"（天依记住了你 {total} 句话，这些都是底色的一部分）")
                parts.append("\n".join(recall_lines))

        # 小模型情感浓度（cnsenti 实时感知）
        ana = getattr(self, "_last_tone", {"pos_score": 0.0, "neg_score": 0.0, "emotion_tone": "neutral"})
        if ana.get("pos_score", 0) or ana.get("neg_score", 0):
            parts.append(f"情感浓度：正向 {ana.get('pos_score',0):.0%} / 负向 {ana.get('neg_score',0):.0%}（cnsenti 感知）")

        # 跨系统记忆联动：LivingMemory / 知识库
        cross = getattr(self, "cross_memory", None)
        if cross:
            mems = cross.fetch_recent_memories(user_id, 2)
            if mems:
                kb_lines = ["[长期记忆联动]"]
                for m in mems:
                    kb_lines.append(f"· {m['text'][:80]}")
                parts.append("\n".join(kb_lines))

        echo = "\n".join(parts)
        req.system_prompt = (req.system_prompt or "") + "\n\n" + echo

    # ── 指令接口 ──
    @filter.command("心情")
    async def cmd_mood(self, event: AstrMessageEvent):
        user_id = event.unified_msg_origin
        state = self.store.get_state(user_id)
        peaks = self.store.get_peaks(user_id, 5)
        total_words = self.store.count_words_remembered(user_id)
        lines = [
            "🎭 天依记着的你：",
            f"· 当前情绪：{state['emotion']}",
            f"· 情绪值：{state['mood_score']:.2f}",
            f"· 频道：{CHANNELS[state['channel']]['name']}",
            f"· 陪伴了 {state['interaction_count']} 次对话",
            f"· 记住了你 {total_words} 句话",
        ]
        dates = self.store.get_dates(user_id)
        if dates:
            lines.append(f"· 重要日子 {len(dates)} 个：")
            for d in dates[:5]:
                lines.append(f"  - {d['date']} {d['event']}")
        if peaks:
            lines.append("· 最近记得的情绪：")
            for p in peaks[:3]:
                lines.append(f"  - {p['emotion']}「{p['text'][:20]}」")
        yield event.plain_result("\n".join(lines))

    @filter.command("记住")
    async def cmd_remember(self, event: AstrMessageEvent):
        """用法：/记住 08-25 重要日子"""
        user_id = event.unified_msg_origin
        msg = event.message_str.strip()
        # 去掉指令前缀
        arg = re.sub(r"^/?记住\s*", "", msg).strip()
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})\s+(.+)$", arg)
        if not m:
            yield event.plain_result("格式：/记住 08-25 这一天很重要")
            return
        date = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        self.store.add_date(user_id, date, m.group(3).strip())
        yield event.plain_result(f"记住了～ {date}「{m.group(3).strip()}」天依不会忘 (๑•̀ω•́๑)")

    @filter.command("我的日子")
    async def cmd_dates(self, event: AstrMessageEvent):
        user_id = event.unified_msg_origin
        dates = self.store.get_dates(user_id)
        if not dates:
            yield event.plain_result("还没有记录重要日子，用 /记住 08-25 xxx 告诉天依～")
            return
        upcoming = self.store.upcoming_dates(user_id)
        lines = ["📅 天依记着的日子："]
        for d in dates:
            lines.append(f"· {d['date']} {d['event']}")
        if upcoming:
            lines.append("\n✨ 快到了：")
            for u in upcoming:
                lines.append(f"· {u['days_left']} 天后「{u['event']}」")
        yield event.plain_result("\n".join(lines))

    @filter.command("情感回响")
    async def cmd_echo(self, event: AstrMessageEvent):
        """查看系统状态 / 开关"""
        user_id = event.unified_msg_origin
        arg = event.message_str.strip()
        if "off" in arg or "关" in arg:
            yield event.plain_result("情感回响已关闭～天依会安静陪你")
            return
        if "on" in arg or "开" in arg:
            yield event.plain_result("情感回响已开启！温馨提示，天依在 (๑•̀ω•́๑)")
            return
        ref = self.reflection.reflect(user_id)
        state = self.store.get_state(user_id)
        result = f"🧠 情感回响 v1.1\n· 状态：运行中\n· 用户：{user_id[:16]}...\n· 频道：{CHANNELS[state['channel']]['name']}"
        if ref:
            result += "\n\n" + ref
        yield event.plain_result(result)

    # ── Web 面板注册 ──
    def _register_web_panel(self):
        """注册情感回响可视化面板"""
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        prefix = "emotional_echo"

        async def serve_dashboard(**kwargs):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    return f.read()
            except FileNotFoundError:
                return "<html><body>情感回响面板文件未找到</body></html>"

        async def serve_status(**kwargs):
            try:
                user_id = kwargs.get("user_id") or ""
                if not user_id:
                    # 尝试从请求头拿用户信息
                    user_id = kwargs.get("request", None).headers.get("X-User-Id", "") if kwargs.get("request") else ""
                if not user_id:
                    # 用最新活跃用户
                    conn = self.store._conn()
                    row = conn.execute("SELECT user_id FROM user_state ORDER BY last_active DESC LIMIT 1").fetchone()
                    conn.close()
                    user_id = row["user_id"] if row else ""
                return self._build_status_json(user_id)
            except Exception:
                return {"error": "status unavailable"}

        try:
            self.context.register_web_api(f"/{prefix}/", serve_dashboard, ["GET"], "情感回响面板")
            self.context.register_web_api(f"/{prefix}/status", serve_status, ["GET"], "情感回响数据")
            logger.info(f"情感回响面板已注册: /{prefix}/")
        except Exception as e:
            logger.warning(f"情感回响面板注册失败（旧版兼容）: {e}")

    def _build_status_json(self, user_id: str) -> dict:
        """汇总状态 JSON"""
        state = self.store.get_state(user_id)
        peaks = self.store.get_peaks(user_id, 6)
        dates = self.store.get_dates(user_id)
        upcoming = self.store.upcoming_dates(user_id)
        memories = []
        cross = getattr(self, "cross_memory", None)
        if cross:
            try:
                memories = cross.fetch_recent_memories(user_id, 4)
            except Exception:
                memories = []

        # 情感倾向统计：从对话日志统计七情/正负
        mood_breakdown = []
        try:
            conn = self.store._conn()
            rows = conn.execute(
                "SELECT emotion, COUNT(*) as c FROM conversation_log WHERE user_id=? AND role='user' AND emotion != '' GROUP BY emotion ORDER BY c DESC LIMIT 7",
                (user_id,)).fetchall()
            conn.close()
            for r in rows:
                mood_breakdown.append({"k": r["emotion"], "v": r["c"]})
        except Exception:
            pass

        def fmt_time(ts):
            try:
                return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
            except Exception:
                return ""

        created = float(state.get("created_at", 0) or 0)
        days_known = int((time.time() - created) / 86400) if created else 0

        date_list = []
        for d in dates:
            up = next((u for u in upcoming if u["date"] == d["date"]), None)
            date_list.append({
                "date": d["date"],
                "event": d["event"],
                "days_left": up["days_left"] if up else None,
            })

        return {
            "emotion": state.get("emotion", "neutral"),
            "channel": state.get("channel", "natural"),
            "mood_score": round(state.get("mood_score", 0.5), 2),
            "interaction_count": state.get("interaction_count", 0),
            "total_messages": state.get("total_messages", 0),
            "days_known": days_known,
            "peak_count": len(peaks),
            "date_count": len(dates),
            "mood_breakdown": mood_breakdown,
            "peaks": [{"emotion": p["emotion"], "text": p["text"], "time": fmt_time(p["ts"])} for p in peaks],
            "dates": date_list,
            "memories": [{"source": m.get("source", "memory"), "text": m.get("text", "")} for m in memories],
        }

    # ── 事件总线处理器（跨插件联动） ──
    async def _on_video_discovered(self, event_name: str, data: dict):
        """bili_agent 刷到视频时，更新用户兴趣偏好"""
        try:
            user_id = data.get("user_id", "")
            if not user_id:
                return
            sender_id = data.get("sender_id", "")
            group_id = data.get("group_id") or None
            title = data.get("title", "")
            tags = data.get("tags", "")
            score = data.get("score", 0)
            if score >= 60 and title:
                interest_text = f"刷到视频: {title}"
                if tags:
                    interest_text += f" | 标签: {tags}"
                if getattr(self, "cross_memory", None):
                    self.cross_memory.write_emotion_to_livingmemory(
                        user_id, "interest", interest_text, min(1.0, score / 100)
                    )
                # 兴趣也算一种正向情绪峰值，触发画像微调
                try:
                    event_bus.emit("emotion_peak", {
                        "user_id": user_id,
                        "sender_id": sender_id,
                        "group_id": group_id,
                        "scope_id": user_id if user_id.startswith("private_") else "",
                        "emotion": "interest",
                        "text": f"对视频感兴趣: {title}",
                        "weight": min(1.0, score / 100),
                    })
                except Exception:
                    pass
                logger.info(f"[EmotionalEcho] 已记录视频兴趣: {title[:40]}")
        except Exception as e:
            logger.debug(f"[EmotionalEcho] 处理 video_discovered 事件异常: {e}")

    # ── 事件总线：收到画像更新通知（④ 扩环） ──
    async def _on_profile_updated(self, event_name: str, data: dict):
        """self_evolution 更新了用户画像，记录到 echo.db 的 reflection 中"""
        try:
            user_id = (data or {}).get("scope_id", "") or (data or {}).get("user_id", "")
            emotion = (data or {}).get("emotion", "")
            if not user_id or not emotion:
                return
            # 把画像更新写入 reflection_log，供后续注入使用
            self.store.add_reflection(user_id, f"画像已更新: [{emotion}] 性格特质已记录")
            logger.info(f"[EmotionalEcho] 已记录画像更新: user={user_id} emotion={emotion}")
        except Exception as e:
            logger.debug(f"[EmotionalEcho] 处理 profile_updated 事件异常: {e}")

    async def terminate(self):
        logger.info("情感回响插件已卸载，清理事件总线注册…")
        try:
            event_bus.off("video_discovered", self._on_video_discovered)
            event_bus.off("profile_updated", self._on_profile_updated)
            logger.info("[EmotionalEcho] 已清理事件总线注册")
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════
# 小模型引擎：cnsenti 中文情感分析（本地，轻量，手机无压力）
# ═══════════════════════════════════════════════════════════════
class EmotionAnalyzer:
    """基于 cnsenti 的轻量情感分析引擎——几十KB，无需GPU，毫秒级响应"""

    def __init__(self):
        self._enabled = False
        self._sent = None
        self._emo = None
        try:
            from cnsenti import Sentiment, Emotion
            self._sent = Sentiment()
            self._emo = Emotion()
            self._enabled = True
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self._enabled

    def analyze(self, text: str) -> dict:
        """返回情感分析结果"""
        result = {
            "pos": 0, "neg": 0, "pos_score": 0.0, "neg_score": 0.0,
            "emotions": {},
            "emotion_tone": "neutral",
        }
        if not self._enabled or not text:
            return result

        try:
            se = self._sent.sentiment_count(text)
            em = self._emo.emotion_count(text)
            result["pos"] = se.get("pos", 0)
            result["neg"] = se.get("neg", 0)
            total = se.get("words", 1) or 1
            result["pos_score"] = result["pos"] / total
            result["neg_score"] = result["neg"] / total

            result["emotions"] = {k: em.get(k, 0) for k in ["好", "乐", "哀", "怒", "惧", "恶", "惊"]}

            # 情感基调判断
            if result["neg"] > result["pos"]:
                result["emotion_tone"] = "sad"
            elif result["pos"] > result["neg"]:
                result["emotion_tone"] = "happy"
            # 七情加权
            emo_sum = sum(result["emotions"].values())
            if emo_sum > 0:
                top_emo = max(result["emotions"], key=result["emotions"].get)
                if top_emo in ("哀", "惧"):
                    result["emotion_tone"] = "sad"
                elif top_emo in ("怒", "恶"):
                    result["emotion_tone"] = "angry"
                elif top_emo in ("好", "乐"):
                    result["emotion_tone"] = "happy"
                elif top_emo == "惊":
                    result["emotion_tone"] = "surprised"
        except Exception:
            pass
        return result


# ═══════════════════════════════════════════════════════════════
# 跨系统记忆联动：LivingMemory + 知识库
# ═══════════════════════════════════════════════════════════════
class CrossSystemMemory:
    """尝试读取 LivingMemory 和知识库的记忆片段，失败不影响主流程"""

    def __init__(self):
        self._memory_paths = [
            "/root/AstrBot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db",
        ]

    def fetch_recent_memories(self, user_id: str, limit: int = 3) -> list:
        """从 LivingMemory 读取最近记忆片段"""
        results = []
        for db_path in self._memory_paths:
            try:
                if not os.path.exists(db_path):
                    continue
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                # 尝试读取 memory_atoms 表（LivingMemory 的最新记忆片段）
                try:
                    rows = conn.execute(
                        "SELECT content, created_at FROM memory_atoms ORDER BY created_at DESC LIMIT ?",
                        (limit,)).fetchall()
                    for r in rows:
                        results.append({
                            "source": "livingmemory",
                            "text": r["content"][:200],
                            "time": r["created_at"]
                        })
                except Exception:
                    pass
                # 也试试 documents 表
                try:
                    rows2 = conn.execute(
                        "SELECT text, created_at FROM documents ORDER BY created_at DESC LIMIT ?",
                        (limit,)).fetchall()
                    for r in rows2:
                        results.append({
                            "source": "livingmemory_doc",
                            "text": r["text"][:200],
                            "time": r["created_at"][:19] if isinstance(r["created_at"], str) else r["created_at"]
                        })
                except Exception:
                    pass
                conn.close()
            except Exception:
                continue
        return results[:limit]

    def fetch_knowledge_base(self, query: str = "", limit: int = 3) -> list:
        """从天依的知识库检索相关记忆"""
        # 知识库在 AstrBot 的 SQLite 中，通过 memory_kb 相关表
        # 但这里简化处理：读取知识库外部关联的记忆
        try:
            results = []
            # 知识库实际在 LivingMemory 的 documents 表里
            lm_path = "/root/AstrBot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db"
            if os.path.exists(lm_path):
                conn = sqlite3.connect(lm_path)
                conn.row_factory = sqlite3.Row
                if query:
                    rows = conn.execute(
                        "SELECT text, created_at FROM documents WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                        (f"%{query}%", limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT text, created_at FROM documents ORDER BY created_at DESC LIMIT ?",
                        (limit,)).fetchall()
                for r in rows:
                    results.append({"source": "knowledge_base", "text": r["text"][:200], "time": r["created_at"][:19] if isinstance(r["created_at"], str) else r["created_at"]})
                conn.close()
            return results[:limit]
        except Exception:
            return []

    def write_emotion_to_livingmemory(self, user_id: str, emotion: str, text: str, weight: float = 0.5) -> bool:
        """把情感峰值写回 LivingMemory 的 memory_atoms（情绪记忆双向联动）"""
        try:
            lm_path = "/root/AstrBot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db"
            if not os.path.exists(lm_path):
                return False
            conn = sqlite3.connect(lm_path, timeout=5)
            now = time.time()
            ttl = 30.0
            # parent_memory_id 有 NOT NULL 约束，用 1 作为默认父节点
            conn.execute(
                """INSERT INTO memory_atoms (
                    parent_memory_id, atom_type, content, entities, importance, confidence,
                    created_at, last_accessed_at, last_reinforced_at, event_time, ttl_days,
                    expires_at, status, reinforcement_count, decay_type, session_id, persona_id, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    1, "emotion",
                    f"[情感回响] {emotion}: {text[:120]}",
                    json.dumps([emotion, "情感记忆", "情绪回响"], ensure_ascii=False),
                    min(1.0, max(0.1, weight)), 0.7, now, now, now, now, ttl,
                    now + ttl * 86400, "active", 0, "exponential",
                    user_id, "洛天依",
                    json.dumps({"source": "emotional_echo", "emotion": emotion, "weight": round(weight, 3)}, ensure_ascii=False)
                )
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"[EmotionalEcho] 写入 LivingMemory 失败: {e}")
            return False