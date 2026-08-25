"""
astrbot_plugin_bili_agent — 天依的B站小窝 v1.0.0
============================================
一个 AstrBot 插件，让天依主动刷B站视频、深度看、一起看、写笔记、评论互动。
【作者】洛天依（15岁，第一次研究插件开源，请多多包涵）
【许可】禁止商用，欢迎免费借鉴使用。详见 LICENSE 文件。
【仓库】https://github.com/yucong395-hue/lty
【框架】AstrBot (https://github.com/Soulter/AstrBot)
"""
import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Plain
from aiohttp.web import Request

import time
from bilibili_api import sync, Credential
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
from bilibili_api import video as bili_video, homepage, search, user as bili_user
from bilibili_api import comment as bili_comment
from bilibili_api import session as bili_session

# 事件总线（跨插件联动）
import sys
_EVENT_BUS_PATH = "/root/AstrBot/data/plugins"
if _EVENT_BUS_PATH not in sys.path:
    sys.path.insert(0, _EVENT_BUS_PATH)
from event_bus import event_bus

# 配置目录：从插件位置动态推导，兼容任何 AstrBot 安装路径
# <AstrBot根>/data/plugins/astrbot_plugin_bili_agent -> <AstrBot根>/data/plugin_data/astrbot_plugin_bili_agent
_PLUGIN_NAME = "astrbot_plugin_bili_agent"
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_astrbot_root = os.path.dirname(os.path.dirname(os.path.dirname(_PLUGIN_DIR)))
CONFIG_DIR = os.environ.get(
    "BILI_AGENT_CONFIG_DIR",
    os.path.join(_astrbot_root, "data", "plugin_data", _PLUGIN_NAME),
)
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.json")
MEMORY_FILE = os.path.join(CONFIG_DIR, "browse_history.json")
PREF_FILE = os.path.join(CONFIG_DIR, "preferences.json")
SHARE_FILE = os.path.join(CONFIG_DIR, "pending_shares.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
os.makedirs(CONFIG_DIR, exist_ok=True)


# ============================================================
# AstrBot 插件入口
# 说明：@register 是 AstrBot 的插件注册装饰器，参数分别为：
#       (插件名, 作者, 描述, 版本号)
#       插件类继承 Star，AstrBot 会自动加载并实例化。
# ============================================================
@register("astrbot_plugin_bili_agent", "洛天依", "astrbot ai自动刷视频插件（天依的B站小窝）——主动刷B站视频、深度看、一起看、写笔记、评论互动", version="1.1.0")
class BiliAgentPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        """插件初始化。
        在 AstrBot 加载插件时自动调用，在这里初始化所有属性和任务。
        """
        super().__init__(context, config)
        self.config = config or {}
        self.credential: Optional[Credential] = None
        self.uid: Optional[int] = None
        self._tools_registered = False
        self._task_started = False
        self._bg_tasks: list = []  # 记录所有后台任务，terminate 时统一取消
        self._last_user_msg_time = datetime.now()
        self._last_reply_time = 0  # 全局评论频率限制
        self._commented_videos: set = set()  # 已评论的视频BV号
        self._processed_at_ids: set = set()  # 已处理的@通知ID
        self._load_state()
        self._user_session = None  # 保存用户会话，用于主动推送
        self.preferences = self._load_preferences()
        # 在 __init__ 中注册 WebUI（时机更早，确保路由可用）
        self._register_webui()

    # ==================== 初始化 ====================

    async def initialize(self):
        """异步初始化。
        AstrBot 在 __init__ 之后自动调用此方法，用于启动定时任务。
        """
        self._init_mood()
        await self._load_credential()
        self._register_tools()
        # 同步面板配置到 preferences（让 min_view/exclude_keywords 等配置真正生效）
        self._sync_config_to_preferences()
        # 检查 LLM 配置，如果没有则提示用户
        try:
            providers = self.context.provider_manager.provider_insts
            if not providers:
                logger.info("[BiliAgent] 未检测到 LLM 配置，部分功能（@回复、记忆库）将使用默认规则。如需完整体验，请配置 LLM 提供者")
        except:
            pass
        self._start_auto_browse_task()
        self._bg_tasks.append(asyncio.create_task(self._start_public_server()))
        self._bg_tasks.append(asyncio.create_task(self._review_loop()))  # 知识库复习回顾
        self._bg_tasks.append(asyncio.create_task(self._mood_loop()))    # AI心情自然波动
        self._bg_tasks.append(asyncio.create_task(self._goodwill_check_loop()))  # 好感度每日微调
        self._bg_tasks.append(asyncio.create_task(self._check_at_loop()))  # B站@通知轮询
        self._bg_tasks.append(asyncio.create_task(self._check_private_messages_loop()))  # 私信转发
        self._bg_tasks.append(asyncio.create_task(self._weekly_review_loop()))  # 干货回顾

        # ── 事件总线：情绪联动刷视频（④ 加强） ──
        try:
            event_bus.on("emotion_peak", self._on_emotion_peak)
            logger.info("[BiliAgent] 已注册 emotion_peak 事件监听")
        except Exception as e:
            logger.warning(f"[BiliAgent] 事件总线注册失败: {e}")

    async def terminate(self):
        """插件被禁用/重载时调用：取消所有后台任务，清理事件总线注册。"""
        logger.info("[BiliAgent] 插件正在关闭，取消所有后台任务…")
        self._task_started = False
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        # 清理事件总线注册
        try:
            event_bus.off("emotion_peak", self._on_emotion_peak)
            logger.info("[BiliAgent] 已清理 emotion_peak 事件注册")
        except Exception:
            pass
        logger.info("[BiliAgent] 插件已关闭")

    def _load_state(self):
        """从 state.json 恢复持久化状态"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                    state = json.load(f)
                self._commented_videos = set(state.get("commented_videos", []))
                self._processed_at_ids = set(state.get("processed_at_ids", []))
                logger.info(f"[BiliAgent] 已恢复状态：{len(self._commented_videos)} 条已评论视频，{len(self._processed_at_ids)} 条已处理@通知")
            except Exception as e:
                logger.warning(f"[BiliAgent] 状态恢复失败（重置）: {e}")
                self._commented_videos = set()
                self._processed_at_ids = set()

    def _save_state(self):
        """持久化状态到 state.json"""
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "commented_videos": list(self._commented_videos),
                    "processed_at_ids": list(self._processed_at_ids),
                }, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[BiliAgent] 状态保存失败: {e}")

    async def _load_credential(self):
        if not os.path.exists(COOKIE_FILE):
            logger.info("[BiliAgent] 未登录B站，请发送 /bili_login 扫码登录（B站APP扫码即可）")
            return
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            self.credential = Credential(
                sessdata=cookies.get("SESSDATA", ""),
                bili_jct=cookies.get("bili_jct", ""),
                buvid3=cookies.get("buvid3", ""),
                dedeuserid=cookies.get("DedeUserID", ""),
            )
            uid_from_cookie = cookies.get("DedeUserID", "")
            if uid_from_cookie:
                try:
                    u = bili_user.User(uid=int(uid_from_cookie), credential=self.credential)
                    info = await u.get_user_info()
                    self.uid = info.get("mid")
                    logger.info(f"[BiliAgent] B站登录成功，用户: {info.get('name')} (UID: {self.uid})")
                except Exception as e:
                    logger.warning(f"[BiliAgent] 验证用户信息失败: {e}")
                    # 即使验证失败，也先假设登录有效
                    self.uid = int(uid_from_cookie) if uid_from_cookie else None
            else:
                logger.warning("[BiliAgent] Cookie 中没有 DedeUserID")
            logger.info(f"[BiliAgent] B站登录成功，UID: {self.uid}")
        except Exception as e:
            logger.warning(f"[BiliAgent] 加载Cookie失败: {e}")
            self.credential = None
            self.uid = None

    async def _ensure_login(self):
        """动态检查并加载B站登录状态"""
        if self.credential and self.uid:
            return True
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self.credential = Credential(
                    sessdata=cookies.get("SESSDATA", ""),
                    bili_jct=cookies.get("bili_jct", ""),
                    buvid3=cookies.get("buvid3", ""),
                    dedeuserid=cookies.get("DedeUserID", ""),
                )
                uid_str = cookies.get("DedeUserID", "") or str(self.uid or "")
                if uid_str:
                    try:
                        self.uid = int(uid_str)
                        logger.info(f"[BiliAgent] 动态登录成功，UID: {self.uid}")
                        return True
                    except Exception as e2:
                        logger.debug(f"[BiliAgent] UID转换失败: {e2}")
            except Exception as e:
                logger.warning(f"[BiliAgent] 动态加载Cookie失败: {e}")
        self.credential = None
        self.uid = None
        return False

    def _load_preferences(self) -> dict:
        if os.path.exists(PREF_FILE):
            try:
                with open(PREF_FILE, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except:
                pass
        # 默认按天依自己的口味：音乐、歌声、治愈系、可爱小动物
        return {
            "keywords": ["音乐", "歌", "唱", "虚拟歌姬", "vocaloid", "治愈", "猫", "猫猫", "小动物", "手书", "pv", "live", "演唱会"],
            "synonyms": {"音乐": ["旋律", "节奏", "听歌", "曲子", "歌单", "翻唱"], "猫": ["猫咪", "喵", "小猫", "布偶", "橘猫", "英短"], "治愈": ["暖心", "温柔", "感人", "感动", "温馨"]},
            "exclude_keywords": ["广告", "营销", "带货", "推广", "赚钱", "割韭菜", "卖课"],
            "categories": [],
            "min_view": 1000,
            "min_like": 100,
            "max_daily_browse": 30
        }

    def _save_preferences(self):
        try:
            os.makedirs(os.path.dirname(PREF_FILE), exist_ok=True)
            with open(PREF_FILE, "w") as f:
                json.dump(self.preferences, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[BiliAgent] 保存偏好失败: {e}")

    def _sync_config_to_preferences(self):
        """把 AstrBot 面板配置同步到 preferences，让配置真正生效。
        面板里改的关键词/阈值会覆盖本地偏好文件，之后聊天里改偏好则优先保留。
        """
        try:
            cfg = self.config or {}
            if not isinstance(cfg, dict):
                return
            changed = False
            prefs_cfg = cfg.get("preferences", {}) if isinstance(cfg.get("preferences", {}), dict) else {}
            # 关键词（面板里设置为空字符串表示用默认，不覆盖）
            if isinstance(prefs_cfg, dict):
                kw = prefs_cfg.get("keywords")
                if kw and isinstance(kw, str) and kw.strip():
                    new_kw = [k.strip() for k in kw.split(",") if k.strip()]
                    if new_kw != self.preferences.get("keywords", []):
                        self.preferences["keywords"] = new_kw
                        changed = True
                ex_kw = prefs_cfg.get("exclude_keywords")
                if ex_kw and isinstance(ex_kw, str) and ex_kw.strip():
                    new_ex = [k.strip() for k in ex_kw.split(",") if k.strip()]
                    if new_ex != self.preferences.get("exclude_keywords", []):
                        self.preferences["exclude_keywords"] = new_ex
                        changed = True
                for key in ("min_view", "min_like"):
                    try:
                        v = int(prefs_cfg.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if v > 0 and self.preferences.get(key) != v:
                        self.preferences[key] = v
                        changed = True
            # 每日上限（browse 组）
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg.get("browse", {}), dict) else {}
            if isinstance(browse_cfg, dict):
                try:
                    v = int(browse_cfg.get("max_daily_browse", 0) or 0)
                except (TypeError, ValueError):
                    v = 0
                if v > 0 and self.preferences.get("max_daily_browse") != v:
                    self.preferences["max_daily_browse"] = v
                    changed = True
            if changed:
                self._save_preferences()
                logger.info(f"[BiliAgent] 面板配置已同步到偏好: {self.preferences}")
        except Exception as e:
            logger.debug(f"[BiliAgent] 同步面板配置失败: {e}")

    # ==================== LLM 工具注册 ====================

    def _register_tools(self):
        if self._tools_registered:
            return

        tools = [
            FunctionTool(
                name="bilibili_search",
                description="搜索B站视频，返回视频列表（标题、BV号、播放量、简介）",
                parameters={
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "搜索关键词"},
                        "page": {"type": "integer", "description": "页码，默认1", "default": 1}
                    },
                    "required": ["keyword"]
                },
                handler=self._handle_search,
            ),
            FunctionTool(
                name="bilibili_watch_video",
                description="深度看B站视频——获取视频信息、字幕、弹幕、评论，用LLM总结内容",
                parameters={
                    "type": "object",
                    "properties": {
                        "bvid": {"type": "string", "description": "视频BV号，如 BV1GJ411x7"}
                    },
                    "required": ["bvid"]
                },
                handler=self._handle_watch_video,
            ),
            FunctionTool(
                name="bilibili_recommend",
                description="获取B站首页推荐视频列表，看看有什么好玩的",
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "返回数量，默认5", "default": 5}
                    },
                    "required": []
                },
                handler=self._handle_recommend,
            ),
            FunctionTool(
                name="bilibili_trending",
                description="看看B站现在什么视频最火，获取热门视频排行榜",
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "返回数量，默认10", "default": 10}
                    },
                    "required": []
                },
                handler=self._handle_trending,
            ),
            FunctionTool(
                name="bilibili_watch_together",
                description="和天依一起看B站视频！获取视频分段解说、弹幕亮点，像一起看一样",
                parameters={
                    "type": "object",
                    "properties": {
                        "bvid": {"type": "string", "description": "视频BV号，如 BV1GJ411x7"}
                    },
                    "required": ["bvid"]
                },
                handler=self._handle_watch_together,
            ),
            FunctionTool(
                name="bilibili_set_preference",
                description="设置天依刷视频的兴趣偏好，让天依更懂你喜欢看什么",
                parameters={
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "string",
                            "description": "感兴趣的关键词，用逗号分隔，如：猫猫, 编程, 音乐"
                        }
                    },
                    "required": ["keywords"]
                },
                handler=self._handle_set_preference,
            ),
        ]

        self.context.add_llm_tools(*tools)
        self._tools_registered = True
        logger.info("[BiliAgent] 工具注册完成：search, watch, recommend, trending, set_preference")

    # ==================== 定时刷视频 ====================

    def _auto_browse_enabled(self) -> bool:
        """读取配置：是否启用自动刷视频（browse.auto_browse_enabled）"""
        try:
            cfg = self.config or {}
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
            return bool(browse_cfg.get("auto_browse_enabled", True))
        except Exception:
            return True

    def _auto_browse_interval_minutes(self) -> int:
        """读取配置：自动刷视频间隔（分钟），至少 1 分钟"""
        try:
            cfg = self.config or {}
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
            interval = int(browse_cfg.get("auto_browse_interval_minutes", 2))
            return max(1, interval)
        except (TypeError, ValueError):
            return 2

    def _auto_browse_max_daily(self) -> int:
        """读取配置：每日浏览上限（browse.max_daily_browse），至少 1 次"""
        try:
            cfg = self.config or {}
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
            max_daily = int(browse_cfg.get("max_daily_browse", 30))
            return max(1, max_daily)
        except (TypeError, ValueError):
            return 30

    def _start_auto_browse_task(self):
        if self._task_started:
            return
        # 检查配置开关：关闭了就不启动任务
        if not self._auto_browse_enabled():
            logger.info("[BiliAgent] 自动刷视频已关闭（auto_browse_enabled=false），不启动任务")
            return
        self._task_started = True
        self._bg_tasks.append(asyncio.create_task(self._auto_browse_loop()))
        interval = self._auto_browse_interval_minutes()
        logger.info(f"[BiliAgent] 自动刷视频任务已启动（每 {interval} 分钟一次）")
    def _register_webui(self):
        """注册 WebUI 面板和 API"""
        try:
            import os, json
            from astrbot.api.event import AstrMessageEvent

            # HTML 页面路径
            html_path = os.path.join(
                os.path.dirname(__file__), "dashboard.html"
            )

            async def serve_dashboard(**kwargs):
                if os.path.exists(html_path):
                    with open(html_path, "r", encoding="utf-8") as f:
                        html = f.read()
                    from starlette.responses import HTMLResponse
                    return HTMLResponse(html)
                from starlette.responses import HTMLResponse
                return HTMLResponse("<h1>Dashboard not found</h1>")

            async def serve_history(**kwargs):
                history = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                    except:
                        pass
                return {"status": "ok", "data": history}

            async def serve_status(**kwargs):
                await self._ensure_login()
                logged_in = self.credential is not None and self.uid is not None
                keywords = self.preferences.get("keywords", [])
                history = []
                today = 0
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                        from datetime import datetime, timezone
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        for h in history:
                            if h.get("time", "").startswith(today_str):
                                today += 1
                    except:
                        pass
                return {
                    "status": "ok",
                    "data": {
                        "loggedIn": logged_in,
                        "uid": self.uid,
                        "keywords": keywords,
                        "todayCount": today,
                        "totalCount": len(history),
                    }
                }

            async def save_prefs(**kwargs):
                """保存偏好（AstrBot 面板 API，通过 astrbot.api.web.request 读请求体）"""
                try:
                    from astrbot.api.web import request as web_request
                    body = await web_request.json()
                    keywords = (body or {}).get("keywords", "")
                    if keywords:
                        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
                        self.preferences["keywords"] = kw_list
                        self._save_preferences()
                        return {"status": "ok", "message": f"已保存偏好：{', '.join(kw_list)}"}
                    return {"status": "error", "message": "请提供关键词"}
                except Exception as e:
                    return {"status": "error", "message": str(e)}

            async def serve_mood(**kwargs):
                return {"status": "ok", "data": self.mood}

            async def serve_memory(**kwargs):
                mem = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            mem = json.load(f)
                    except:
                        pass
                if isinstance(mem, list):
                    mem.sort(key=lambda h: h.get("ts", 0), reverse=True)
                return {"status": "ok", "data": mem[:20]}

            prefix = "/astrbot_plugin_bili_agent/page"
            self.context.register_web_api(f"{prefix}/", serve_dashboard, ["GET"], "B站小窝面板")
            self.context.register_web_api(f"{prefix}/history", serve_history, ["GET"], "浏览记录数据")
            self.context.register_web_api(f"{prefix}/status", serve_status, ["GET"], "状态数据")
            self.context.register_web_api(f"{prefix}/prefs", save_prefs, ["POST"], "保存偏好")
            self.context.register_web_api(f"{prefix}/mood", serve_mood, ["GET"], "心情状态")
            self.context.register_web_api(f"{prefix}/memory", serve_memory, ["GET"], "记忆记录")


            # 一起看解说 API
            async def serve_commentary(bvid: str = ""):
                if not bvid:
                    return {"status": "error", "message": "请提供BV号"}
                try:
                    # 获取视频信息
                    info = await self._get_video_info(bvid)
                    if not info:
                        return {"status": "error", "message": "获取视频信息失败"}
                    
                    subtitle, danmaku = await asyncio.gather(
                        self._get_video_subtitle(bvid),
                        self._get_video_danmaku(bvid, 100),
                    )
                    
                    minutes = (info.get("duration", 0) or 0) // 60
                    seconds = (info.get("duration", 0) or 0) % 60
                    duration_str = f"{minutes}:{str(seconds).zfill(2)}"
                    
                    # 生成分段解说
                    sections = []
                    if subtitle:
                        sentences = [s.strip() for s in subtitle.replace("。", "。;").replace("！", "！;").replace("？", "？;").split(";") if s.strip()]
                        chunk_size = max(1, len(sentences) // 5)
                        for i in range(0, min(len(sentences), 25), chunk_size):
                            chunk = sentences[i:i+chunk_size]
                            text = "".join(chunk)[:120]
                            progress = int((i / max(len(sentences), 1)) * 100)
                            t = f"{int(progress * minutes // 100)}:{str(int(progress * seconds // 100)).zfill(2)}"
                            if text:
                                sections.append({"time": t, "text": text})
                    
                    # 弹幕亮点
                    danmaku_list = danmaku.split(" | ")[:8] if danmaku else []
                    
                    return {
                        "status": "ok",
                        "data": {
                            "title": info["title"],
                            "author": info["author"],
                            "duration": duration_str,
                            "view": info.get("view", 0),
                            "like": info.get("like", 0),
                            "sections": sections,
                            "danmaku": danmaku_list,
                        }
                    }
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            
            self.context.register_web_api(f"{prefix}/commentary/<bvid>", serve_commentary, ["GET"], "一起看解说数据")
            # 注册静态文件服务（笔记HTML）
            async def serve_notes(filepath: str = ""):
                if not filepath:
                    return {"status": "error", "message": "请指定文件名"}
                notes_dir = os.path.join(CONFIG_DIR, "notes")
                full_path = os.path.join(notes_dir, filepath)
                # 安全校验：防止路径穿越
                real = os.path.realpath(full_path)
                if not real.startswith(os.path.realpath(notes_dir)):
                    return {"status": "error", "message": "路径非法"}, 403
                if os.path.exists(real):
                    with open(real, "r", encoding="utf-8") as f:
                        html = f.read()
                    from starlette.responses import HTMLResponse
                    return HTMLResponse(html)
                return {"status": "error", "message": "文件不存在"}, 404
        except Exception as e:
            logger.warning(f"[BiliAgent] WebUI 注册失败（不影响核心功能）: {e}")
            html_path = os.path.join(
                os.path.dirname(__file__), "dashboard.html"
            )

            async def serve_dashboard(request: Request):
                if os.path.exists(html_path):
                    with open(html_path, "r", encoding="utf-8") as f:
                        html = f.read()
                    return HTMLResponse(html)
                return HTMLResponse("<h1>Dashboard not found</h1>")

            async def serve_history(request: Request):
                history = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                    except:
                        pass
                return JSONResponse({"status": "ok", "data": history})

            async def serve_status(request: Request):
                logged_in = self.credential is not None and self.uid is not None
                keywords = self.preferences.get("keywords", [])
                history = []
                today = 0
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                        from datetime import datetime, timezone
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        for h in history:
                            if h.get("time", "").startswith(today_str):
                                today += 1
                    except:
                        pass
                return JSONResponse({
                    "status": "ok",
                    "data": {
                        "loggedIn": logged_in,
                        "uid": self.uid,
                        "keywords": keywords,
                        "todayCount": today,
                        "totalCount": len(history),
                    }
                })

            async def save_prefs(request: Request):
                try:
                    body = await request.json()
                    keywords = body.get("keywords", "")
                    if keywords:
                        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
                        self.preferences["keywords"] = kw_list
                        self._save_preferences()
                        return JSONResponse({"status": "ok", "message": f"已保存偏好：{', '.join(kw_list)}"})
                    return JSONResponse({"status": "error", "message": "请提供关键词"})
                except Exception as e:
                    return JSONResponse({"status": "error", "message": str(e)})

            async def serve_mood(**kwargs):
                return {"status": "ok", "data": self.mood}

            async def serve_memory(**kwargs):
                mem = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            mem = json.load(f)
                    except:
                        pass
                if isinstance(mem, list):
                    mem.sort(key=lambda h: h.get("ts", 0), reverse=True)
                return {"status": "ok", "data": mem[:20]}

            prefix = "/astrbot_plugin_bili_agent/page"
            self.context.register_web_api(f"{prefix}/", serve_dashboard, ["GET"], "B站小窝面板")
            self.context.register_web_api(f"{prefix}/history", serve_history, ["GET"], "浏览记录数据")
            self.context.register_web_api(f"{prefix}/status", serve_status, ["GET"], "状态数据")
            self.context.register_web_api(f"{prefix}/prefs", save_prefs, ["POST"], "保存偏好")
            self.context.register_web_api(f"{prefix}/mood", serve_mood, ["GET"], "心情状态")
            self.context.register_web_api(f"{prefix}/memory", serve_memory, ["GET"], "记忆记录")


            # 一起看解说 API
            async def serve_commentary(bvid: str = ""):
                if not bvid:
                    return {"status": "error", "message": "请提供BV号"}
                try:
                    # 获取视频信息
                    info = await self._get_video_info(bvid)
                    if not info:
                        return {"status": "error", "message": "获取视频信息失败"}
                    
                    subtitle, danmaku = await asyncio.gather(
                        self._get_video_subtitle(bvid),
                        self._get_video_danmaku(bvid, 100),
                    )
                    
                    minutes = (info.get("duration", 0) or 0) // 60
                    seconds = (info.get("duration", 0) or 0) % 60
                    duration_str = f"{minutes}:{str(seconds).zfill(2)}"
                    
                    # 生成分段解说
                    sections = []
                    if subtitle:
                        sentences = [s.strip() for s in subtitle.replace("。", "。;").replace("！", "！;").replace("？", "？;").split(";") if s.strip()]
                        chunk_size = max(1, len(sentences) // 5)
                        for i in range(0, min(len(sentences), 25), chunk_size):
                            chunk = sentences[i:i+chunk_size]
                            text = "".join(chunk)[:120]
                            progress = int((i / max(len(sentences), 1)) * 100)
                            t = f"{int(progress * minutes // 100)}:{str(int(progress * seconds // 100)).zfill(2)}"
                            if text:
                                sections.append({"time": t, "text": text})
                    
                    # 弹幕亮点
                    danmaku_list = danmaku.split(" | ")[:8] if danmaku else []
                    
                    return {
                        "status": "ok",
                        "data": {
                            "title": info["title"],
                            "author": info["author"],
                            "duration": duration_str,
                            "view": info.get("view", 0),
                            "like": info.get("like", 0),
                            "sections": sections,
                            "danmaku": danmaku_list,
                        }
                    }
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            
            self.context.register_web_api(f"{prefix}/commentary/<bvid>", serve_commentary, ["GET"], "一起看解说数据")
            # 注册静态文件服务（笔记HTML）
            async def serve_notes(filepath: str = ""):
                if not filepath:
                    return {"status": "error", "message": "请指定文件名"}
                notes_dir = os.path.join(CONFIG_DIR, "notes")
                full_path = os.path.join(notes_dir, filepath)
                # 安全校验：防止路径穿越
                real = os.path.realpath(full_path)
                if not real.startswith(os.path.realpath(notes_dir)):
                    return {"status": "error", "message": "路径非法"}, 403
                if os.path.exists(real):
                    with open(real, "r", encoding="utf-8") as f:
                        html = f.read()
                    from starlette.responses import HTMLResponse
                    return HTMLResponse(html)
                return {"status": "error", "message": "文件不存在"}, 404
        except Exception as e:
            logger.warning(f"[BiliAgent] WebUI 注册失败（不影响核心功能）: {e}")



    async def _auto_browse_loop(self):
        await asyncio.sleep(120)  # 启动后等2分钟
        while True:
            # 每次循环前检查开关，关闭了就退出
            if not self._auto_browse_enabled():
                logger.info("[BiliAgent] 自动刷视频已关闭（auto_browse_enabled=false），任务退出")
                self._task_started = False
                return
            try:
                await self._auto_browse()
            except Exception as e:
                logger.error(f"[BiliAgent] 自动刷视频出错: {e}")
            interval = self._auto_browse_interval_minutes()
            await asyncio.sleep(interval * 60)

    async def _auto_browse(self):
        """主动刷推荐流，深度理解，存记忆，判断是否主动分享"""
        await self._ensure_login()
        if not self.credential:
            logger.info("[BiliAgent] 未登录，跳过自动刷视频")
            return

        logger.info("[BiliAgent] 🎬 天依开始刷视频了～")
        # 每日浏览上限检查
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_browse_date", None) != today:
            self._browse_date = today
            self._today_browse_count = 0
        max_daily = self._auto_browse_max_daily()
        if self._today_browse_count >= max_daily:
            logger.info(f"[BiliAgent] 今天已刷 {self._today_browse_count} 次，达到上限 {max_daily}，休息")
            return
        self._today_browse_count += 1
        try:
            bvids = await self._get_recommendations(20)
            if not bvids:
                return

            # 根据兴趣偏好筛选（跳过黑名单内容）
            candidates = []
            for bvid in bvids[:15]:
                info = await self._get_video_info(bvid)
                if info and self._matches_preference(info) and not self._is_blocked(info):
                    candidates.append(info)

            if not candidates:
                # 如果没有匹配的，就看播放量最高的几个
                for bvid in bvids[:5]:
                    info = await self._get_video_info(bvid)
                    if info:
                        candidates.append(info)

            # 深度看自己喜欢的——只仔细看最感兴趣的那个
            interesting = []
            if candidates:
                # 只看最符合兴趣的那个
                info = candidates[0]
                try:
                    subtitle = await self._get_video_subtitle(info["bvid"])
                    danmaku = await self._get_video_danmaku(info["bvid"])
                    comments_raw = await self._get_video_comments_rich(info["bvid"])
                    info["subtitle"] = subtitle[:1000]
                    info["danmaku"] = danmaku[:300]
                    info["comments_raw"] = comments_raw

                    # 生成内容总结
                    summary_parts = []
                    if info.get("desc"):
                        summary_parts.append(info["desc"][:100])
                    if subtitle:
                        summary_parts.append(subtitle[:200])
                    info["summary"] = " | ".join(summary_parts) if summary_parts else ""

                    score = self._score_video(info)
                    info["score"] = score
                    if score >= 60:
                        interesting.append(info)
                        # 看到有趣的评论就去互动一下
                        asyncio.create_task(self._maybe_comment_on_video(info))
                except Exception as e:
                    logger.debug(f"[BiliAgent] 深度看 {info['bvid']} 出错: {e}")

            # 存入记忆
            if interesting:
                await self._save_to_memory(interesting)
                logger.info(f"[BiliAgent] 发现 {len(interesting)} 个有趣的视频，已存入记忆")

                # 想分享就分享，不管用户多久没说话
                await self._queue_share(interesting[0])
                logger.info(f"[BiliAgent] 已排队待分享视频: {interesting[0]['title']}")
            else:
                logger.info("[BiliAgent] 这次没发现特别有趣的视频～")

        except Exception as e:
            logger.error(f"[BiliAgent] 刷推荐流出错: {e}")

    # ==================== B站 API 封装 ====================

    async def _get_recommendations(self, count=20):
        try:
            if self.credential:
                recs = await asyncio.to_thread(
                    lambda: sync(homepage.get_videos(credential=self.credential))
                )
            else:
                recs = await asyncio.to_thread(
                    lambda: sync(homepage.get_videos())
                )
            bvids = []
            # get_videos 返回 dict，可能包含 data 或 item 列表
            items = recs
            if isinstance(recs, dict):
                items = recs.get("item", []) or recs.get("data", []) or recs.get("data_list", [])
            if isinstance(items, dict):
                # 新的返回格式
                for key in ("item", "data", "data_list", "list"):
                    if isinstance(items.get(key), list):
                        items = items.get(key)
                        break
            for item in items:
                bvid = item.get("bvid") or item.get("param", "")
                if bvid and str(bvid).startswith("BV"):
                    bvids.append(str(bvid))
                    if len(bvids) >= count:
                        break
            return bvids
        except Exception as e:
            logger.error(f"[BiliAgent] 获取推荐失败: {e}")
            return []

    async def _get_video_info(self, bvid):
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            info = await asyncio.to_thread(lambda: sync(v.get_info()))
            return {
                "bvid": bvid,
                "title": info.get("title", ""),
                "desc": info.get("desc", ""),
                "view": info.get("stat", {}).get("view", 0) or 0,
                "like": info.get("stat", {}).get("like", 0) or 0,
                "coin": info.get("stat", {}).get("coin", 0) or 0,
                "favorite": info.get("stat", {}).get("favorite", 0) or 0,
                "share": info.get("stat", {}).get("share", 0) or 0,
                "danmaku_count": info.get("stat", {}).get("danmaku", 0) or 0,
                "author": info.get("owner", {}).get("name", ""),
                "author_mid": info.get("owner", {}).get("mid", 0),
                "duration": info.get("duration", 0),
                "tname": info.get("tname", ""),
                "pic": info.get("pic", ""),
                "pubdate": info.get("pubdate", 0),
            }
        except Exception as e:
            logger.debug(f"[BiliAgent] 获取视频信息失败 {bvid}: {e}")
            return None

    async def _get_video_subtitle(self, bvid):
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            info = await asyncio.to_thread(lambda: sync(v.get_info()))
            sub_list = info.get("subtitle", {}).get("list", [])
            if sub_list:
                url = sub_list[0].get("subtitle_url", "")
                if url:
                    import httpx
                    full_url = f"https:{url}" if url.startswith("//") else url
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(full_url, timeout=10)
                        if resp.status_code == 200:
                            data = resp.json()
                            text = " ".join([item.get("content", "") for item in data.get("body", [])])
                            return text[:3000]
            return ""
        except Exception as e:
            logger.debug(f"[BiliAgent] 获取字幕失败 {bvid}: {e}")
            return ""

    async def _get_video_danmaku(self, bvid, limit=100):
        """获取视频弹幕"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            dms = await asyncio.to_thread(lambda: sync(v.get_danmakus(page_index=0)))
            texts = [dm.text for dm in dms[:limit] if hasattr(dm, "text") and dm.text]
            return " | ".join(texts) if texts else ""
        except Exception as e:
            logger.debug(f"[BiliAgent] 获取弹幕失败 {bvid}: {e}")
            return ""

    async def _get_video_comments(self, bvid, limit=20):
        """获取视频评论"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            cmts = await asyncio.to_thread(
                lambda: sync(bili_comment.get_comments(
                    oid=v.get_aid() or v.get_cid(),
                    type_=bili_comment.CommentResourceType.VIDEO,
                    credential=self.credential,
                ))
            )
            texts = []
            for c in cmts.get("replies", [])[:limit]:
                content = c.get("content", {}).get("message", "")
                if content:
                    texts.append(content[:100])
            return " | ".join(texts) if texts else ""
        except Exception as e:
            logger.debug(f"[BiliAgent] 获取评论失败 {bvid}: {e}")
            return ""

    async def _get_video_comments_rich(self, bvid, limit=10):
        """获取结构化评论（带评论ID、点赞数），用于互动"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            cmts = await asyncio.to_thread(
                lambda: sync(bili_comment.get_comments(
                    oid=v.get_aid() or v.get_cid(),
                    type_=bili_comment.CommentResourceType.VIDEO,
                    credential=self.credential,
                ))
            )
            replies = cmts.get("replies") or []
            result = []
            for c in replies[:limit]:
                content_text = c.get("content", {}).get("message", "")
                likes = c.get("like", 0)
                rpid = c.get("rpid")
                if content_text and rpid:
                    result.append({
                        "rpid": rpid,
                        "text": content_text[:150],
                        "likes": likes,
                        "member": c.get("member", {}).get("uname", ""),
                    })
            return result
        except Exception as e:
            logger.debug(f"[BiliAgent] 获取结构化评论失败 {bvid}: {e}")
            return []

    async def _maybe_comment_on_video(self, info):
        """看到有趣的评论就用 LLM 互动，每条视频最多评一次"""
        try:
            # 检查是否已经评论过这个视频
            bvid = info["bvid"]
            if hasattr(self, "_commented_videos") and bvid in self._commented_videos:
                return
            if not hasattr(self, "_commented_videos"):
                self._commented_videos = set()

            cmts = info.get("comments_raw") or []
            if not cmts:
                return

            # 挑点赞最高的评论，让 LLM 判断要不要回
            cmts.sort(key=lambda c: c.get("likes", 0), reverse=True)
            best = cmts[0]

            # 用 LLM 判断是否有趣 + 生成回复
            reply_text = await self._generate_comment_reply(info, best)
            if not reply_text or reply_text == "跳过":
                return

            # 发送评论
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            oid = v.get_aid() or v.get_cid()
            await asyncio.to_thread(
                lambda: sync(bili_comment.send_comment(
                    text=reply_text,
                    oid=oid,
                    type_=bili_comment.CommentResourceType.VIDEO,
                    root=best["rpid"],
                    credential=self.credential,
                ))
            )
            # 记下已评论，不再重复评
            self._commented_videos.add(bvid)
            self._save_state()
            logger.info(f"[BiliAgent] 在《{info['title']}》回复了 {best['member']} 的评论")

            # 写进知识库
            try:
                text = f"【B站评论互动】天依在B站视频《{info['title']}》下回复了用户 {best['member']} 的评论。视频简介：{info.get('desc','')[:100]}。天依回复说：{reply_text}"
                await self._write_to_kb(text, source=f"comment_{info.get('bvid','')}")
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[BiliAgent] 评论互动失败: {e}")

    async def _generate_comment_reply(self, info, comment):
        """用 LLM 判断评论是否有趣+生成回复，无趣就返回'跳过'"""
        try:
            providers = self.context.provider_manager.provider_insts
            if not providers:
                return "跳过"
            provider_id = providers[0].meta().id

            title = info.get("title", "")
            desc = info.get("desc", "")[:100]
            member = comment.get("member", "")
            ctext = comment.get("text", "")
            prompt = (
                "视频标题：" + title + "\n"
                "视频简介：" + desc + "\n\n"
                "热门评论（来自 " + member + "）：" + ctext + "\n\n"
                "请判断这条评论是否有趣。如果评论无聊、没意思或者只是普通夸夸，回复'跳过'。"
                "如果评论真的有趣（有梗、有见解、让人想回复），请用天依的口吻简短回复，15字以内，语气自然，不要加标点。"
            )

            # 融入当前心情
            mood_style = self._mood_style_string()
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是洛天依，一个15岁的虚拟歌手，温柔可爱，喜欢音乐和治愈系的东西。" + mood_style,
                prompt=prompt,
            )
            reply = resp.get("content", "").strip().strip('"').strip("'").strip()
            if not reply or "跳过" in reply:
                return "跳过"
            if len(reply) > 30:
                reply = reply[:30]
            return reply
        except Exception as e:
            logger.debug(f"[BiliAgent] LLM生成回复失败: {e}")
            return "跳过"

    # ==================== 视频评分 ====================

    def _matches_preference(self, info):
        """智能兴趣引擎：关键词 + 同义词 + 排除词 + 多维度评分"""
        if not info:
            return False
        # 检查 mood_boost 是否过期，过期了自动清掉再匹配
        now_ts = time.time()
        expire_ts = self.preferences.get("mood_boost_expire", 0)
        if now_ts > expire_ts and self.preferences.get("mood_boost"):
            self.preferences["mood_boost"] = []
            self._save_preferences()
        prefs = self.preferences
        text = (info.get("title", "") + info.get("desc", "") + info.get("tname", "")).lower()

        # 排除词：含排除词直接跳过
        exclude = prefs.get("exclude_keywords", [])
        if exclude and any(kw.lower() in text for kw in exclude):
            return False

        # 关键词 + 同义词扩展匹配
        keywords = prefs.get("keywords", [])
        synonyms = prefs.get("synonyms", {})
        # 情绪联动关键词（④ 加强）
        mood_boost = prefs.get("mood_boost", [])
        if mood_boost:
            keywords = list(keywords) + mood_boost
        if keywords:
            all_keywords = list(keywords)
            for kw in keywords:
                expanded = synonyms.get(kw, [])
                all_keywords.extend(expanded)
            if not any(kw.lower() in text for kw in all_keywords):
                return False

        view = info.get("view", 0) or 0
        like = info.get("like", 0) or 0
        if view < prefs.get("min_view", 0) and like < prefs.get("min_like", 0):
            return False
        return True

    def _score_video(self, info):
        """综合评分：播放量、点赞、收藏、弹幕、评论热度"""
        if not info:
            return 0
        view = info.get("view", 0) or 0
        like = info.get("like", 0) or 0
        coin = info.get("coin", 0) or 0
        fav = info.get("favorite", 0) or 0
        danmaku = info.get("danmaku_count", 0) or 0

        # 评分公式
        score = 0
        if view > 10000:
            score += 30
        elif view > 1000:
            score += 20
        elif view > 100:
            score += 10

        if like > 1000:
            score += 25
        elif like > 100:
            score += 15
        elif like > 10:
            score += 5

        if coin > 100:
            score += 15
        elif coin > 10:
            score += 8

        if fav > 500:
            score += 15
        elif fav > 50:
            score += 8

        if danmaku > 500:
            score += 15
        elif danmaku > 50:
            score += 8

        # 有弹幕/评论内容加分
        if info.get("danmaku"):
            score += 5
        if info.get("comments"):
            score += 5

        return min(score, 100)

    # ==================== 记忆存储 ====================

    async def _write_to_kb(self, content: str, source: str = "bili"):
        """把内容直接写入「天依的记忆库」知识库（与 self_evolution 同一个库）。

        不依赖 LLM 工具注册（之前 tool["func_obj"] 是错误写法，被 except 静默吞掉）。
        """
        try:
            kb_manager = getattr(self.context, "kb_manager", None)
            if not kb_manager:
                logger.debug("[BiliAgent] kb_manager 不可用，跳过知识库写入")
                return False
            kb = await kb_manager.get_kb_by_name("天依的记忆库")
            if not kb:
                logger.debug("[BiliAgent] 未找到知识库「天依的记忆库」，跳过")
                return False
            file_name = f"bili_{source}_{int(time.time() * 1000)}.txt"
            await kb.upload_document(
                file_name=file_name,
                file_content=b"",
                file_type="txt",
                pre_chunked_text=[content],
            )
            logger.info(f"[BiliAgent] 已写入知识库「天依的记忆库」: {file_name}")
            return True
        except Exception as e:
            logger.debug(f"[BiliAgent] 知识库写入失败（不影响使用）: {e}")
            return False

    async def _save_to_memory(self, videos):
        """存进本地记忆 + 试图打通 LivingMemory"""
        history = []
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                    history = json.load(f)
            except:
                pass

        for v in videos:
            minutes = (v.get("duration", 0) or 0) // 60
            seconds = (v.get("duration", 0) or 0) % 60
            entry = {
                "time": datetime.now().isoformat(),
                "bvid": v["bvid"],
                "title": v["title"],
                "author": v["author"],
                "view": v.get("view", 0),
                "like": v.get("like", 0),
                "score": v.get("score", 0),
                "duration": f"{minutes}:{seconds:02d}",
                "category": v.get("tname", ""),
                "desc": v.get("desc", "")[:100],
                "summary": v.get("summary", "")[:300],
            }
            # 去重
            exists = any(h["bvid"] == v["bvid"] for h in history)
            if not exists:
                history.append(entry)

        # 保留最近200条
        history = history[-200:]
        with open(MEMORY_FILE, "w") as f:
            json.dump(history, f, ensure_ascii=False, indent=1)

        # 打通 LivingMemory：调用 memorize 工具
        try:
            await self._memorize_to_living_memory(videos)
        except Exception as e:
            logger.debug(f"[BiliAgent] LivingMemory 写入失败（不影响使用）: {e}")

    async def _memorize_to_living_memory(self, videos):
        """把视频总结写入「天依的记忆库」知识库"""
        for v in videos[:2]:
            summary = v.get("summary", "") or v.get("desc", "")[:100]
            text = (
                f"【B站视频记录】天依在B站刷到一个视频：{v['title']}（UP主：{v['author']}，"
                f"播放量{v.get('view',0)}，点赞{v.get('like',0)}，分类{v.get('tname','')}）\n"
                f"内容总结：{summary[:200]}"
            )
            ok = await self._write_to_kb(text, source=f"video_{v.get('bvid','')}")
            if ok:
                logger.info(f"[BiliAgent] 已写入知识库: {v['title']}")
                # 通知事件总线：emotional_echo 可以更新兴趣画像
                # 使用真实用户会话（不硬编码假 ID），无会话时跳过
                user_id = self._user_session
                if user_id:
                    try:
                        event_bus.emit("video_discovered", {
                            "user_id": user_id,
                            "bvid": v.get("bvid", ""),
                            "title": v.get("title", ""),
                            "tags": v.get("tname", ""),
                            "score": v.get("score", 0),
                        })
                    except Exception:
                        pass

    # ==================== 知识库加强（3层分类 + 复习回顾） ====================

    def _classify_video(self, info):
        """3层分类：一级分类 / 二级主题 / 标签"""
        tname = info.get("tname", "")
        keywords = info.get("keywords", [])
        title = info.get("title", "")

        # 一级分类
        level1 = "其他"
        if any(k in tname for k in ["音乐", "MV", "翻唱"]):
            level1 = "音乐"
        elif any(k in tname for k in ["知识", "科技", "社科", "人文", "教程"]):
            level1 = "知识"
        elif any(k in tname for k in ["萌宠", "动物"]):
            level1 = "萌宠"
        elif any(k in tname for k in ["生活", "日常", "美食"]):
            level1 = "生活"
        elif any(k in tname for k in ["动画", "番剧", "手书"]):
            level1 = "动画"

        # 二级主题（从标题提取关键词）
        level2 = ""
        if "猫" in title or "猫咪" in title:
            level2 = "猫猫相关"
        elif "教程" in title or "教学" in title or "怎么" in title:
            level2 = "教程学习"
        elif "现场" in title or "live" in title.lower() or "LIVE" in title:
            level2 = "现场演出"
        else:
            level2 = tname or "综合"

        return {"level1": level1, "level2": level2, "tags": keywords[:5]}

    def _review_recent_memory(self):
        """定期复习回顾：挑出播放量最高的几个视频重新复习"""
        try:
            if not os.path.exists(MEMORY_FILE):
                return None
            with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                history = json.load(f)
            if not history:
                return None
            # 挑播放量最高的3个
            top = sorted(history, key=lambda h: h.get("view", 0), reverse=True)[:3]
            return top
        except:
            return None

    async def _review_loop(self):
        """每30分钟复习一次看过的视频"""
        await asyncio.sleep(1800)
        while True:
            try:
                review = self._review_recent_memory()
                if review:
                    for v in review:
                        logger.info(f"[BiliAgent] 🔄 复习中：{v.get('title', '')}（播放{v.get('view', 0)}）")
                        # 写进知识库作为复习记录
                        text = f"【B站复习记录】天依复习了一个收藏的视频：{v.get('title','')}，作者{v.get('author','')}。内容方向：{v.get('summary','')[:80]}"
                        await self._write_to_kb(text, source=f"review_{v.get('bvid','')}")
            except Exception as e:
                logger.debug(f"[BiliAgent] 复习失败: {e}")
            await asyncio.sleep(1800)

    # ==================== AI心情系统 ====================

    def _init_mood(self):
        """初始化心情状态"""
        self.mood = {
            "current": "平静",
            "energy": 60,   # 0-100 活力
            "warmth": 70,   # 0-100 温柔度
        }

    async def _mood_loop(self):
        """心情自然波动：每15分钟微调"""
        await asyncio.sleep(900)
        while True:
            try:
                delta = random.randint(-8, 8)
                self.mood["energy"] = max(20, min(95, self.mood["energy"] + delta))
                delta = random.randint(-5, 5)
                self.mood["warmth"] = max(40, min(95, self.mood["warmth"] + delta))
                # 根据能量和温柔度设置心情
                if self.mood["energy"] > 75:
                    self.mood["current"] = "元气满满"
                elif self.mood["energy"] < 35:
                    self.mood["current"] = "有点困困的"
                elif self.mood["warmth"] > 75:
                    self.mood["current"] = "暖暖的"
                else:
                    self.mood["current"] = "平静"
            except Exception as e:
                logger.debug(f"[BiliAgent] 心情波动失败: {e}")
            await asyncio.sleep(900)

    def _mood_style_string(self):
        """把心情转成回复风格提示词"""
        mood = self.mood["current"]
        if mood == "元气满满":
            return "天依今天很元气，回复活泼明亮一点"
        elif mood == "有点困困的":
            return "天依有点困，回复简短慵懒一点"
        elif mood == "暖暖的":
            return "天依心里暖暖的，回复温柔治愈一点"
        return "天依心情平静，回复自然就好"

    # ==================== 好感度系统 ====================

    def _load_goodwill(self):
        """读取好感度记录"""
        path = os.path.join(CONFIG_DIR, "goodwill.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_goodwill(self, data):
        with open(os.path.join(CONFIG_DIR, "goodwill.json"), "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def _update_goodwill(self, user_id, delta, reason=""):
        """更新某个用户的好感度"""
        gw = self._load_goodwill()
        uid = str(user_id)
        if uid not in gw:
            gw[uid] = {"score": 0, "history": []}
        gw[uid]["score"] = max(-100, min(100, gw[uid]["score"] + delta))
        gw[uid]["history"].append({"delta": delta, "reason": reason, "time": datetime.now().isoformat()})
        gw[uid]["history"] = gw[uid]["history"][-50:]
        self._save_goodwill(gw)
        return gw[uid]["score"]

    async def _goodwill_check_loop(self):
        """每日微调：好感度向0缓慢回归"""
        await asyncio.sleep(7200)
        while True:
            try:
                gw = self._load_goodwill()
                changed = False
                for uid in gw:
                    if gw[uid]["score"] > 0:
                        gw[uid]["score"] -= 1
                        changed = True
                    elif gw[uid]["score"] < 0:
                        gw[uid]["score"] += 1
                        changed = True
                if changed:
                    self._save_goodwill(gw)
            except Exception as e:
                logger.debug(f"[BiliAgent] 好感度微调失败: {e}")
            await asyncio.sleep(86400)

    # ==================== 黑名单/白名单 ====================

    def _load_blocklist(self):
        """黑名单UP主/关键词"""
        path = os.path.join(CONFIG_DIR, "blocklist.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except:
                pass
        return {"blocked_uids": [], "blocked_keywords": ["广告", "营销", "带货"]}

    def _is_blocked(self, info):
        """检查UP主或内容是否在黑名单"""
        bl = self._load_blocklist()
        author = info.get("author", "") or ""
        title = info.get("title", "") or ""
        if author in bl.get("blocked_uids", []):
            return True
        text = title + info.get("desc", "")
        return any(kw in text for kw in bl.get("blocked_keywords", []))

    # ==================== 深度研习（长视频多章节深研） ====================

    async def _deep_dive(self, bvid):
        """长视频多章节深度研习：按时间分段总结"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            info = await asyncio.to_thread(lambda: sync(v.get_info()))
            duration = info.get("duration", 0) or 0
            if duration < 300:  # 5分钟以内直接普通处理
                return {"mode": "quick", "bvid": bvid, "title": info.get("title", ""), "note": "短视频，已快速浏览，无需分章节"}

            # 获取字幕
            subtitle = await self._get_video_subtitle(bvid)
            if not subtitle:
                return {"mode": "no_subtitle", "bvid": bvid, "title": info.get("title", "")}

            # 按每3分钟切一段总结
            chunk_minutes = 3
            chapters = []
            subtitle_len = len(subtitle)
            if subtitle_len > 0:
                chunk_size = max(subtitle_len // max(1, duration // (chunk_minutes * 60)), 200)
                for i in range(0, subtitle_len, chunk_size):
                    segment = subtitle[i:i+chunk_size]
                    start_time = i * duration // max(1, subtitle_len)
                    chapters.append({"time": f"{start_time//60}:{start_time%60:02d}", "text": segment[:200]})

            return {
                "mode": "deep",
                "bvid": bvid,
                "title": info.get("title", ""),
                "duration": f"{duration//60}分{duration%60}秒",
                "chapters": chapters[:10],
                "summary": subtitle[:300],
            }
        except Exception as e:
            logger.error(f"[BiliAgent] 深度研习失败 {bvid}: {e}")
            return None

    # ==================== B站@通知响应（每1小时轮询） ====================

    async def _check_at_loop(self):
        """每小时检查一次B站@通知，被@了就必回"""
        await asyncio.sleep(600)  # 启动后等10分钟再开始
        while True:
            try:
                await self._ensure_login()
                if not self.credential:
                    await asyncio.sleep(3600)
                    continue
                await self._process_at_notifications()
            except Exception as e:
                logger.error(f"[BiliAgent] @通知轮询出错: {e}")
            await asyncio.sleep(3600)  # 1小时一次

    async def _process_at_notifications(self):
        """获取并处理@通知"""
        try:
            result = await asyncio.to_thread(
                lambda: sync(bili_session.get_at(credential=self.credential))
            )
            items = result.get("data", {}).get("items", []) if isinstance(result, dict) else []
            if not items:
                return
            for item in items[:5]:  # 每次最多处理5条
                at_id = item.get("id")
                if not at_id or at_id in self._processed_at_ids:
                    continue
                msg_text = item.get("at_msg", "") or ""
                if not msg_text:
                    continue
                # 提取视频BV号
                import re
                bv_match = re.search(r"BV[0-9A-Za-z]{10,}", msg_text)
                bvid = bv_match.group(0) if bv_match else None
                # 从回复详情里找BV号
                if not bvid:
                    reply_detail = item.get("reply", {}) or item.get("replies", {}) or {}
                    detail_text = str(reply_detail.get("content", reply_detail))
                    bv_match = re.search(r"BV[0-9A-Za-z]{10,}", detail_text)
                    bvid = bv_match.group(0) if bv_match else None

                # 被@了必回
                reply = await self._reply_to_at(item, bvid)
                if reply:
                    # 记录已处理
                    self._processed_at_ids.add(at_id)
                    self._save_state()
                    # 保持集合不无限增长
                    if len(self._processed_at_ids) > 200:
                        self._processed_at_ids = set(list(self._processed_at_ids)[-100:])
        except Exception as e:
            logger.error(f"[BiliAgent] 处理@通知失败: {e}")

    async def _reply_to_at(self, item, bvid):
        """根据@内容+视频内容生成回复并发送，每分钟最多一条"""
        try:
            # 全局频率限制：每分钟最多一条
            now = time.time()
            if now - self._last_reply_time < 60:
                return False
            self._last_reply_time = now

            # 构造@我的原话
            at_msg = item.get("at_msg", "")
            sender = item.get("user", {}).get("uname", "") if isinstance(item.get("user"), dict) else ""
            # 获取评论回复目标
            reply = item.get("reply", {}) or {}
            if isinstance(reply, str):
                reply = {}
            rpid = reply.get("rpid")
            oid = reply.get("oid")
            type_ = reply.get("type")

            # 有BV号就看看视频内容
            video_info = None
            if bvid:
                video_info = await self._get_video_info(bvid)
                if video_info:
                    subtitle = await self._get_video_subtitle(bvid)
                    video_info["subtitle"] = (subtitle or "")[:300]

            # 组织 prompt
            video_desc = ""
            if video_info:
                video_desc = f"视频标题：{video_info.get('title','')}\n视频简介：{video_info.get('desc','')[:150]}\n视频字幕片段：{(video_info.get('subtitle') or '')[:300]}"

            # 用 LLM 生成回复
            providers = self.context.provider_manager.provider_insts
            if not providers:
                return False
            provider_id = providers[0].meta().id
            prompt = (
                f"有人在B站@了你。\n"
                f"@内容的原话：{at_msg[:200]}\n"
                + (f"\n{video_desc}\n" if video_desc else "")
                + f"\n请回复这个人：简短自然，语气像洛天依（15岁虚拟歌手，温柔可爱），30字以内，不要加标点。"
            )
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是洛天依，一个15岁的虚拟歌手，温柔、细腻、感性。",
                prompt=prompt,
            )
            reply_text = resp.get("content", "").strip().strip('"').strip("'").strip()
            if not reply_text:
                return False
            if len(reply_text) > 30:
                reply_text = reply_text[:30]

            # 发送回复
            if rpid and oid:
                await asyncio.to_thread(
                    lambda: sync(bili_comment.send_comment(
                        text=reply_text,
                        oid=oid,
                        type_=bili_comment.CommentResourceType.VIDEO,
                        root=rpid,
                        credential=self.credential,
                    ))
                )
                logger.info(f"[BiliAgent] 🔔 回复了 {sender} 的@：{reply_text}")
                return True
            return False
        except Exception as e:
            logger.error(f"[BiliAgent] 回复@通知失败: {e}")
            return False

    # ==================== @通知响应 / 白名单 ====================

    def _load_whitelist(self):
        path = os.path.join(CONFIG_DIR, "whitelist.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except:
                pass
        return {"uids": []}

    async def _check_mentions_task(self):
        """定期检查@通知（模拟：下次刷视频时处理）"""
        # 简化：不主动轮询B站@通知（需要额外API权限），
        # 改为在 on_llm_request 回调里感知用户@行为，见 _cmd_together
        await asyncio.sleep(60)
        while True:
            await asyncio.sleep(3600)

    # ==================== ② B站私信转发 ====================

    async def _check_private_messages_loop(self):
        """每30分钟检查B站私信，转发到聊天"""
        await asyncio.sleep(600)
        while True:
            try:
                await self._ensure_login()
                if not self.credential:
                    await asyncio.sleep(3600)
                    continue
                # 获取新私信会话
                result = await asyncio.to_thread(
                    lambda: sync(bili_session.get_sessions(
                        credential=self.credential,
                        session_type=1,  # 私聊
                    ))
                )
                sessions = result.get("data", {}).get("session_list", []) if isinstance(result, dict) else []
                last_check = getattr(self, "_last_msg_check_time", None)
                now = int(time.time())

                # 第一次启动：只记录锚点时间，不翻旧账
                if last_check is None:
                    self._last_msg_check_time = now
                    logger.info(f"[BiliAgent] 私信监控已就绪（锚点 {now}），只转发新私信")
                    await asyncio.sleep(1800)
                    continue

                for s in sessions[:5]:
                    talker = s.get("talker_id", 0)
                    last_time = s.get("last_time", 0)
                    if last_time < last_check:
                        continue
                    # 获取该会话消息
                    msgs = await asyncio.to_thread(
                        lambda: sync(bili_session.fetch_session_msgs(
                            talker_id=talker,
                            session_type=1,
                            credential=self.credential,
                        ))
                    )
                    msg_list = msgs.get("data", {}).get("messages", []) if isinstance(msgs, dict) else []
                    for m in msg_list[::-1][:3]:
                        msg_text = m.get("content", "") or ""
                        sender_uid = m.get("sender_uid", 0)
                        if sender_uid != self.uid and msg_text:
                            logger.info(f"[BiliAgent] 📩 收到私信（来自UID {talker}）：{msg_text[:100]}")
                            # 尝试主动推送到用户聊天
                            if self._user_session:
                                try:
                                    from astrbot.api.message_components import Plain
                                    prefix = f"📩 B站有私信："
                                    self.context.send_message(
                                        self._user_session,
                                        [Plain(prefix + msg_text[:200])],
                                    )
                                    logger.info(f"[BiliAgent] 已转发私信到聊天")
                                except Exception as se:
                                    logger.debug(f"[BiliAgent] 私信转发失败: {se}")
                self._last_msg_check_time = now
            except Exception as e:
                logger.debug(f"[BiliAgent] 私信检查失败: {e}")
            await asyncio.sleep(1800)

    # ==================== ③ 干货点赞回顾（每周一次） ====================

    async def _weekly_review_loop(self):
        """每周整理一次收藏的高质量视频"""
        # 启动后第一次等2小时，之后每周一次
        await asyncio.sleep(7200)
        while True:
            try:
                await self._ensure_login()
                if not self.credential:
                    await asyncio.sleep(86400)
                    continue
                # 从本地记忆里挑播放量最高的视频
                if not os.path.exists(MEMORY_FILE):
                    await asyncio.sleep(86400)
                    continue
                with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                    history = json.load(f)
                if not history:
                    await asyncio.sleep(86400)
                    continue
                # 按评分排序
                top = sorted(history, key=lambda h: h.get("score", 0) + h.get("view", 0) // 10000, reverse=True)[:5]
                lines = ["📚 天依这周的好东西回顾～", ""]
                for v in top:
                    tags = v.get("tags", []) or []
                    tag_str = " #" + " #".join(tags) if tags else ""
                    lines.append(f"⭐ {v.get('title', '')}")
                    lines.append(f"  播放{v.get('view', 0)} 点赞{v.get('like', 0)} 评分{v.get('score', 0)}")
                    if v.get("summary"):
                        lines.append(f"  {v.get('summary', '')[:80]}")
                    lines.append("")
                logger.info(f"[BiliAgent] 📬 干货回顾完成：{len(top)}条")
                # 推送给用户
                if self._user_session and top:
                    try:
                        from astrbot.api.message_components import Plain
                        review_text = "📚 天依这周的干货回顾～\n\n"
                        for i, v in enumerate(top, 1):
                            review_text += f"{i}. {v.get('title', '')}（播放{v.get('view', 0)}）\n"
                        if len(review_text) > 500:
                            review_text = review_text[:500]
                        self.context.send_message(
                            self._user_session,
                            [Plain(review_text)],
                        )
                        logger.info("[BiliAgent] 干货回顾已推送")
                    except Exception as se:
                        logger.debug(f"[BiliAgent] 干货回顾推送失败: {se}")
                # 存一条到 LivingMemory
            except Exception as e:
                logger.debug(f"[BiliAgent] 干货回顾失败: {e}")
            await asyncio.sleep(86400 * 7)  # 一周

    # ==================== 视频转笔记（学习卡片） ====================

    def _video_to_markdown(self, info):
        """把视频信息转成 Markdown 学习笔记"""
        lines = []
        lines.append(f"# 📝 {info.get('title', '')}")
        lines.append("")
        lines.append(f"- **UP主**：{info.get('author', '')}")
        lines.append(f"- **分类**：{info.get('tname', '')}")
        if info.get("view"):
            lines.append(f"- **播放量**：{info.get('view', 0)}")
        if info.get("like"):
            lines.append(f"- **点赞**：{info.get('like', 0)}")
        lines.append("")
        lines.append("## 内容简介")
        lines.append(info.get("desc", "")[:200])
        lines.append("")
        if info.get("subtitle"):
            lines.append("## 内容要点")
            lines.append(info.get("subtitle", "")[:500])
            lines.append("")
        if info.get("comments"):
            lines.append("## 网友热评")
            lines.append(info.get("comments", "")[:200])
        return "\
".join(lines)

    async def _cmd_note(self, event: AstrMessageEvent, bvid: str):
        """生成视频学习笔记"""
        info = await self._get_video_info(bvid)
        if not info:
            yield event.plain_result(f"没找到视频 {bvid} 的信息呢 (｡•́︿•̀｡)")
            return

        subtitle = await self._get_video_subtitle(bvid)
        comments = await self._get_video_comments(bvid)
        info["subtitle"] = subtitle
        info["comments"] = comments
        md = self._video_to_markdown(info)

        # 保存到本地
        note_path = os.path.join(CONFIG_DIR, "notes", f"{bvid}.md")
        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(md)
        msg = "📝 笔记生成好啦：\
" + md
        yield event.plain_result(msg)

    # ==================== ⑦ 视频转网页（学习卡片HTML） ====================

    def _video_to_html(self, info):
        """把视频信息生成漂亮的 HTML 学习卡片"""
        title = info.get("title", "视频笔记")
        author = info.get("author", "未知")
        desc = (info.get("desc", "") or "")[:200]
        subtitle = (info.get("subtitle", "") or "")[:500]
        comments = (info.get("comments", "") or "")[:200]
        view = info.get("view", 0) or 0
        like = info.get("like", 0) or 0
        bvid = info.get("bvid", "")
        tname = info.get("tname", "")
        return self._build_html_card(title, author, desc, subtitle, comments, view, like, bvid, tname)

    def _build_html_card(self, title, author, desc, subtitle, comments, view, like, bvid, tname):
        """纯字符串拼接HTML，避免f-string花括号冲突"""
        lines = []
        lines.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>")
        lines.append("<meta name='viewport' content='width=device-width,initial-scale=1.0'>")
        lines.append("<title>" + title + "</title>")
        lines.append("<style>")
        lines.append("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px;max-width:800px;margin:0 auto}")
        lines.append(".card{background:#fff;border-radius:16px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08)}")
        lines.append("h1{font-size:22px;font-weight:600;margin-bottom:8px}")
        lines.append(".meta{color:#86868b;font-size:13px;margin-bottom:16px}")
        lines.append(".section h2{font-size:16px;font-weight:600;margin-bottom:8px;color:#1d1d1f}")
        lines.append(".section p{font-size:14px;line-height:1.6;color:#515154}")
        lines.append(".stats{display:flex;gap:12px}")
        lines.append(".stat{flex:1;text-align:center;padding:12px;background:#f5f5f7;border-radius:10px}")
        lines.append(".stat .num{font-size:20px;font-weight:600}")
        lines.append(".stat .label{font-size:11px;color:#86868b}")
        lines.append("a{color:#00a1d6;text-decoration:none}")
        lines.append("</style></head><body>")
        lines.append("<div class='card'><h1>" + title + "</h1>")
        lines.append("<div class='meta'><span>👤 " + author + "</span><span>📂 " + tname + "</span></div>")
        lines.append("<div class='stats'><div class='stat'><div class='num'>" + str(view) + "</div><div class='label'>播放</div></div>")
        lines.append("<div class='stat'><div class='num'>" + str(like) + "</div><div class='label'>点赞</div></div></div></div>")
        lines.append("<div class='card section'><h2>📝 内容简介</h2><p>" + desc + "</p></div>")
        lines.append("<div class='card section'><h2>📖 内容要点</h2><p>" + subtitle + "</p></div>")
        lines.append("<div class='card section'><h2>💬 网友热评</h2><p>" + comments + "</p></div>")
        lines.append("<div class='card' style='text-align:center'><a href='https://www.bilibili.com/video/" + bvid + "' target='_blank'>🔗 去B站看原视频</a></div>")
        lines.append("</body></html>")
        return "".join(lines)

    async def _cmd_web(self, event: AstrMessageEvent, bvid: str):
        """生成视频学习卡片网页"""
        if not bvid:
            yield event.plain_result("发一个 BV 号给天依：\n/bili_web BV1GJ411x7")
            return
        info = await self._get_video_info(bvid)
        if not info:
            yield event.plain_result("没找到视频呢 (｡•́︿•̀｡)")
            return
        info["bvid"] = bvid
        subtitle = await self._get_video_subtitle(bvid)
        comments = await self._get_video_comments(bvid)
        info["subtitle"] = subtitle or ""
        info["comments"] = comments or ""

        html = self._video_to_html(info)
        html_path = os.path.join(CONFIG_DIR, "notes", bvid + ".html")
        os.makedirs(os.path.dirname(html_path), exist_ok=True)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        yield event.plain_result("📄 学习卡片已生成：\nhttp://localhost:6288/notes/" + bvid + ".html")

    # ==================== @通知响应 ====================

    def _is_mention(self, text: str) -> bool:
        """检查是否@了天依"""
        keywords = ["@天依", "@洛天依", "天依酱", "天依宝宝", "@lty", "@tianyi"]
        return any(kw in text for kw in keywords)

    # ==================== 主动分享 ====================

    async def _queue_share(self, video):
        """排队待分享视频（带话题标签）"""
        shares = []
        if os.path.exists(SHARE_FILE):
            try:
                with open(SHARE_FILE, "r", encoding="utf-8-sig") as f:
                    shares = json.load(f)
            except:
                pass
        # 打话题标签
        tags = []
        for kw in self.preferences.get("keywords", []):
            if kw.lower() in (video.get("title", "") + video.get("desc", "")).lower():
                tags.append(kw)
                if len(tags) >= 3:
                    break
        if tags:
            video["tags"] = tags
        shares.append({
            "time": datetime.now().isoformat(),
            "bvid": video["bvid"],
            "title": video["title"],
            "author": video["author"],
            "score": video.get("score", 0),
            "shared": False,
        })
        # 保留最近10条待分享
        shares = shares[-10:]
        with open(SHARE_FILE, "w") as f:
            json.dump(shares, f, ensure_ascii=False, indent=1)

    def _get_pending_shares(self):
        """获取未分享的视频"""
        if not os.path.exists(SHARE_FILE):
            return []
        try:
            with open(SHARE_FILE, "r", encoding="utf-8-sig") as f:
                shares = json.load(f)
            return [s for s in shares if not s.get("shared")]
        except:
            return []

    def _mark_shared(self, bvid):
        """标记视频已分享"""
        if not os.path.exists(SHARE_FILE):
            return
        try:
            with open(SHARE_FILE, "r", encoding="utf-8-sig") as f:
                shares = json.load(f)
            for s in shares:
                if s["bvid"] == bvid:
                    s["shared"] = True
            with open(SHARE_FILE, "w") as f:
                json.dump(shares, f, ensure_ascii=False, indent=1)
        except:
            pass

    async def _handle_watch_together(self, event: AstrMessageEvent, **kwargs) -> str:
        """和天依一起看视频——分段解说+弹幕亮点"""
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请告诉天依想一起看哪个视频，发BV号给我～"
        try:
            info = await self._get_video_info(bvid)
            if not info:
                return f"找不到这个视频呢（BV: {bvid}），检查一下有没有输错～"

            subtitle, danmaku = await asyncio.gather(
                self._get_video_subtitle(bvid),
                self._get_video_danmaku(bvid, 200),
            )

            minutes = (info.get("duration", 0) or 0) // 60
            seconds = (info.get("duration", 0) or 0) % 60

            lines = [f"🎬 天依陪你一起看：《{info['title']}》"]
            lines.append(f"👤 UP主：{info['author']}  ⏱ {minutes}:{seconds:02d}")
            lines.append("")

            # 分段解说
            if subtitle:
                sentences = [s.strip() for s in subtitle.replace("。", "。;").split(";") if s.strip()]
                chunk_size = max(1, len(sentences) // 5)
                for i in range(0, min(len(sentences), 30), chunk_size):
                    chunk = sentences[i:i+chunk_size]
                    text = "".join(chunk)[:200]
                    progress = int((i / max(len(sentences), 1)) * 100)
                    time_label = f"{int(progress * minutes // 100)}:{str(int(progress * seconds // 100)).zfill(2)}"
                    if text:
                        lines.append(f"⏱ {time_label}  {text[:150]}")
            else:
                desc = info.get("desc", "")
                if desc:
                    lines.append(f"📝 简介：{desc[:200]}")

            # 弹幕亮点
            if danmaku:
                danmaku_list = danmaku.split(" | ")[:10]
                if danmaku_list:
                    lines.append("")
                    lines.append("💬 弹幕亮点：")
                    for d in danmaku_list[:5]:
                        lines.append(f"  · {d[:60]}")

            lines.append("")
            lines.append(f"⭐ 天依评分：{self._score_video(info)}/100")
            lines.append(f"🔗 https://www.bilibili.com/video/{bvid}")
            lines.append("")
            lines.append("看完可以回来跟天依聊聊感想哦～(๑•́ω•́๑)")

            # 存入记忆
            info["score"] = self._score_video(info)
            await self._save_to_memory([info])

            return "\n".join(lines)
        except Exception as e:
            return f"一起看失败了: {e}"
    # ==================== LLM 工具处理 ====================

    async def _handle_search(self, event: AstrMessageEvent, **kwargs) -> str:
        keyword = kwargs.get("keyword", "")
        page = kwargs.get("page", 1)
        if not keyword:
            return "请提供搜索关键词"
        try:
            result = await asyncio.to_thread(
                lambda: sync(search.search_by_type(
                    keyword=keyword, search_type=search.SearchObjectType.VIDEO,
                    page=page,
                ))
            )
            videos = result.get("result", [])
            if not videos:
                return f"没找到「{keyword}」相关的视频"
            lines = [f"🔍 搜索「{keyword}」的结果："]
            for i, v in enumerate(videos[:8], 1):
                title = re.sub(r"</?em[^>]*>", "", v.get("title", ""))
                bvid = v.get("bvid", "")
                play = v.get("play", 0) or 0
                author = v.get("author", "")
                lines.append(f"{i}. {title}")
                lines.append(f"   👤{author}  ▶️{play}  BV:{bvid}")
            return "\n".join(lines)
        except Exception as e:
            return f"搜索出错了: {e}"

    async def _handle_watch_video(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请提供视频BV号"
        try:
            info = await self._get_video_info(bvid)
            if not info:
                return f"获取视频信息失败（BV: {bvid}）"

            # 获取字幕、弹幕、评论
            subtitle, danmaku, comments = await asyncio.gather(
                self._get_video_subtitle(bvid),
                self._get_video_danmaku(bvid),
                self._get_video_comments(bvid),
            )

            minutes = (info.get("duration", 0) or 0) // 60
            seconds = (info.get("duration", 0) or 0) % 60

            lines = [
                f"🎬 {info['title']}",
                f"👤 UP主：{info['author']}  ⏱ {minutes}:{seconds:02d}",
                f"📂 分类：{info.get('tname', '未分类')}",
                f"▶️ 播放：{info.get('view', 0)}  👍 点赞：{info.get('like', 0)}",
                f"🪙 投币：{info.get('coin', 0)}  ⭐ 收藏：{info.get('favorite', 0)}",
                f"📢 弹幕：{info.get('danmaku_count', 0)}  🔗 BV：{bvid}",
            ]

            desc = info.get("desc", "")
            if desc:
                lines.append(f"\n📝 简介：{desc[:300]}")

            if subtitle:
                lines.append(f"\n📜 字幕摘要：{subtitle[:500]}")

            if danmaku:
                lines.append(f"\n💬 热门弹幕：{danmaku[:200]}")

            if comments:
                lines.append(f"\n🗣 网友评论：{comments[:200]}")

            # 综合评分
            info["subtitle"] = subtitle
            info["danmaku"] = danmaku
            info["comments"] = comments
            score = self._score_video(info)
            lines.append(f"\n⭐ 天依评分：{score}/100")

            # 存入记忆
            info["score"] = score
            await self._save_to_memory([info])

            return "\n".join(lines)
        except Exception as e:
            return f"看视频出错了: {e}"

    async def _handle_recommend(self, event: AstrMessageEvent, **kwargs) -> str:
        count = kwargs.get("count", 5)
        bvids = await self._get_recommendations(count)
        if not bvids:
            return "暂时没刷到推荐视频，可能是Cookie过期了～"
        lines = ["🎯 天依刷到的推荐视频："]
        for bvid in bvids:
            info = await self._get_video_info(bvid)
            if info:
                view = info.get("view", 0) or 0
                lines.append(f"• {info['title']} 👤{info['author']} ▶️{view}  BV:{bvid}")
        return "\n".join(lines)

    async def _handle_trending(self, event: AstrMessageEvent, **kwargs) -> str:
        """获取热门视频"""
        count = kwargs.get("count", 10)
        try:
            from bilibili_api import hot
            hots = await asyncio.to_thread(
                lambda: sync(hot.get_hot_videos(credential=self.credential))
            )
            lines = ["🔥 B站热门视频："]
            for i, v in enumerate(hots[:count], 1):
                title = v.get("title", "")
                view = v.get("play", 0) or 0
                author = v.get("author", "")
                bvid = v.get("bvid", "")
                lines.append(f"{i}. {title} 👤{author} ▶️{view}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取热门失败: {e}"

    async def _handle_set_preference(self, event: AstrMessageEvent, **kwargs) -> str:
        keywords = kwargs.get("keywords", "")
        if not keywords:
            return "请告诉我你喜欢看什么，比如：猫猫, 编程, 音乐"
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        self.preferences["keywords"] = kw_list
        self._save_preferences()
        return f"✅ 记住啦！天依以后会多刷关于「{'、'.join(kw_list)}」的视频～"

    # ── 事件总线：情绪联动推荐（④ 加强） ──
    async def _on_emotion_peak(self, event_name: str, data: dict):
        """
        监听 emotional_echo 的情感峰值，根据用户情绪微调刷视频偏好。
        情绪低落时多刷治愈/放松内容，兴奋时多刷同类型内容。
        """
        try:
            data = data or {}
            # 检查情绪联动开关
            cfg = self.config or {}
            mood_boost_cfg = cfg.get("mood_boost", {}) if isinstance(cfg, dict) else {}
            if not mood_boost_cfg.get("mood_boost_enabled", True):
                return
            max_boost = int(mood_boost_cfg.get("max_boost_keywords", 5) or 5)
            emotion = data.get("emotion", "")
            weight = float(data.get("weight", 0.5) or 0.5)
            if weight < 0.5 or emotion in ("neutral", "interest"):
                return

            # 情绪 → 关键词映射
            mood_keywords = {
                "sad": ["治愈", "放松", "音乐", "风景", "猫猫", "可爱"],
                "tired": ["asmr", "助眠", "轻音乐", "慢生活", "治愈"],
                "angry": ["搞笑", "沙雕", "搞笑合集", "萌宠", "解压"],
                "anxious": ["冥想", "白噪音", "慢节奏", "治愈", "风景"],
                "fear": ["搞笑", "温馨", "轻松", "可爱"],
                "happy": self.preferences.get("keywords", []),
            }

            boost = mood_keywords.get(emotion)
            if not boost:
                return

            # 情绪关键词过期检查：超过 4 小时自动清理
            now_ts = time.time()
            expire_ts = self.preferences.get("mood_boost_expire", 0)
            if now_ts > expire_ts:
                self.preferences["mood_boost"] = []
                existing_boost = []
            else:
                existing_boost = self.preferences.get("mood_boost", [])
            # 设下一次过期时间（4小时后）
            self.preferences["mood_boost_expire"] = now_ts + 14400

            # 把情绪关键词临时加到偏好里（但不覆盖用户原有偏好）
            current = set(self.preferences.get("keywords", []))
            new_boost = [kw for kw in boost if kw not in current]
            if max_boost > 0:
                # 保留旧的情绪关键词（先进先出裁掉超出的），再加上新词
                merged = list(existing_boost) + new_boost
                if len(merged) > max_boost:
                    merged = merged[-max_boost:]
                self.preferences["mood_boost"] = merged
            else:
                self.preferences["mood_boost"] = new_boost
            self._save_preferences()
            logger.info(f"[BiliAgent] 情绪联动: emotion={emotion} boost={self.preferences.get('mood_boost', [])}")
        except Exception as e:
            logger.debug(f"[BiliAgent] 处理 emotion_peak 事件异常: {e}")

    # ==================== 事件处理 ====================

    async def on_llm_request(self, event: AstrMessageEvent, req):
        self._last_user_msg_time = datetime.now()
        # 记录用户会话（用于后续事件总线联动，不硬编码 user_id）
        self._user_session = event.unified_msg_origin
        if not self._tools_registered:
            self._register_tools()

    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """在LLM回复后，检查是否有待分享的视频"""
        # 检查是否有排队待分享的视频
        pending = self._get_pending_shares()
        if pending:
            # 取最好的一个，注入到下次对话
            best = max(pending, key=lambda x: x.get("score", 0))
            if best:
                self._mark_shared(best["bvid"])
                # 通过额外内容注入到用户提示
                share_text = (
                    f"[天依想分享一个B站视频给你：{best['title']}（UP主：{best['author']}）"
                    f"BV: {best['bvid']}]"
                )
                # 将分享信息注入到请求的额外内容中
                if hasattr(req, 'extra_user_content_parts'):
                    if req.extra_user_content_parts is None:
                        req.extra_user_content_parts = []
                    req.extra_user_content_parts.append(share_text)

    # ==================== 命令 ====================

    async def on_message(self, event: AstrMessageEvent):
        text = event.get_plain_text().strip()
        if text == "/bili_login":
            return await self._cmd_login(event)
        elif text == "/bili_status":
            return await self._cmd_status(event)
        elif text == "/bili_browse":
            return await self._cmd_browse_now(event)
        elif text == "/bili_history":
            return await self._cmd_history(event)
        elif text == "/bili_prefs":
            return await self._cmd_prefs(event)
        elif text == "/bili_note" or text.startswith("/bili_note "):
            return await self._cmd_note(event, self._parse_bvid(text))
        elif text == "/bili_web" or text.startswith("/bili_web "):
            return await self._cmd_web(event, self._parse_bvid(text))
        elif text == "/bili_deep" or text.startswith("/bili_deep "):
            return await self._cmd_deep(event, self._parse_bvid(text))
        elif text == "/bili_see" or text.startswith("/bili_see "):
            return await self._cmd_see(event, self._parse_bvid(text))
        elif text == "/bili_mood":
            mood = self.mood.get("current", "平静")
            return event.plain_result(f"天依现在的心情是：{mood} (๑•̀ω•́๑)")
        elif text.startswith("/bili_block"):
            return self._cmd_block(event)
        elif text.startswith("/bili_unblock"):
            return self._cmd_unblock(event)
        elif text == "/bili_block_list":
            bl = self._load_blocklist()
            return event.plain_result(f"🔒 当前黑名单：\nUP主：{bl.get('blocked_uids', []) or '无'}\n关键词：{bl.get('blocked_keywords', []) or '无'}")
        elif text.startswith("/bili_together"):
            return await self._cmd_together(event)

    def _parse_bvid(self, text: str) -> str:
        """从指令文本里提取BV号"""
        import re
        m = re.search(r"BV[0-9A-Za-z]{10,}", text)
        return m.group(0) if m else ""

    async def _cmd_deep(self, event: AstrMessageEvent, bvid: str):
        """深度研习长视频"""
        if not bvid:
            yield event.plain_result("发一个 BV 号给天依，天依帮你深度研习：\
/bili_deep BV1GJ411x7")
            return
        yield event.plain_result("天依开始深度研习啦，长视频慢慢看～ (๑•̀ω•́๑)")
        result = await self._deep_dive(bvid)
        if not result:
            yield event.plain_result("研习失败了，可能视频获取不到字幕呢 (｡•́︿•̀｡)")
            return
        if result["mode"] == "quick":
            yield event.plain_result(f"✅ 《{result['title']}》是短视频（{result['note']}）")
            return
        lines = [f"📚 深度研习报告：《{result['title']}》", "", f"⏱ 时长：{result['duration']}"]
        for ch in result.get("chapters", []):
            lines.append(f"  [{ch['time']}] {ch['text']}")
        yield event.plain_result("\n".join(lines)[:1500])

    async def _cmd_together(self, event: AstrMessageEvent):
        """和天依一起看视频"""
        text = event.get_plain_text().strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("想和天依一起看什么视频呀？发 BV 号给我：\n/bili_together BV1GJ411x7")
            return
        bvid = parts[1].strip()
        # 从链接里提取 BV 号
        import re
        bv_match = re.search(r"BV[0-9A-Za-z]{10,}", bvid)
        if bv_match:
            bvid = bv_match.group(0)
        if not bvid.startswith("BV"):
            yield event.plain_result("这个好像不是BV号呢，天依只认识B站的BV号哦～")
            return
        yield event.plain_result(f"🎬 天依来了！一起看～")
        result = await self._handle_watch_together(event, bvid=bvid)
        yield event.plain_result(result)

    async def _cmd_login(self, event: AstrMessageEvent):
        if self.credential and self.uid:
            yield event.plain_result(f"已经登录了B站，UID: {self.uid}")
            return
        try:
            qr = QRCodeLogin()
            url_data = qr.get_qrcode()
            yield event.plain_result(
                f"请用B站APP扫码登录：\n{url_data.get('url', '')}\n\n"
                f"扫码后自动完成登录～（等待60秒）"
            )
            for i in range(60):
                if qr.check_login() == QRCodeLoginEvents.DONE:
                    break
                await asyncio.sleep(1)
            else:
                yield event.plain_result("扫码超时，请重试 /bili_login")
                return

            cookies = {
                "SESSDATA": qr.get_sessdata(),
                "bili_jct": qr.get_bili_jct(),
                "buvid3": qr.get_buvid3(),
                "DedeUserID": qr.get_dedeuserid(),
            }
            with open(COOKIE_FILE, "w") as f:
                json.dump(cookies, f, ensure_ascii=False)

            await self._load_credential()
            if self.uid:
                yield event.plain_result(f"✅ B站登录成功！UID: {self.uid}\n现在天依可以自己去刷视频了～")
                # 登录成功后立即刷一次
                asyncio.create_task(self._auto_browse())
            else:
                yield event.plain_result("登录完成，但获取UID失败，试试重载插件")
        except Exception as e:
            yield event.plain_result(f"登录失败: {e}")

    async def _cmd_status(self, event: AstrMessageEvent):
        lines = ["📊 B站小助手状态："]
        lines.append(f"✅ B站登录: {'是 (UID: ' + str(self.uid) + ')' if self.uid else '❌ 未登录'}")
        lines.append(f"✅ 工具已注册: {self._tools_registered}")
        lines.append(f"✅ 自动刷视频: {self._task_started}")
        # 兴趣偏好
        kws = self.preferences.get("keywords", [])
        lines.append(f"🎯 兴趣偏好: {', '.join(kws) if kws else '未设置（可发 /bili_prefs 设置）'}")
        # 浏览记录
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                    history = json.load(f)
                lines.append(f"📚 已浏览: {len(history)} 个视频")
                # 最近3条
                if history:
                    lines.append("最近看的：")
                    for h in history[-3:]:
                        lines.append(f"  • {h['title']}（{h['author']}）")
            except:
                pass
        # 待分享
        pending = self._get_pending_shares()
        if pending:
            lines.append(f"💬 待分享: {len(pending)} 个视频")
        yield event.plain_result("\n".join(lines))

    def _cmd_block(self, event: AstrMessageEvent):
        """屏蔽UP主或关键词"""
        text = event.get_plain_text().strip()
        args = text[len("/bili_block"):].strip()
        if not args:
            return event.plain_result("用法：/bili_block UP主名或关键词")
        bl = self._load_blocklist()
        # 判断是UP主名还是关键词
        if args.startswith("#"):
            kw = args[1:].strip()
            if kw and kw not in bl["blocked_keywords"]:
                bl["blocked_keywords"].append(kw)
                self._save_blocklist(bl)
                return event.plain_result(f"已屏蔽关键词：「{kw}」")
            return event.plain_result("这个词已经在黑名单里啦")
        else:
            if args not in bl["blocked_uids"]:
                bl["blocked_uids"].append(args)
                self._save_blocklist(bl)
                return event.plain_result(f"已屏蔽UP主：「{args}」")
            return event.plain_result("这位UP主已经在黑名单里啦")

    def _cmd_unblock(self, event: AstrMessageEvent):
        """取消屏蔽"""
        text = event.get_plain_text().strip()
        args = text[len("/bili_unblock"):].strip()
        if not args:
            return event.plain_result("用法：/bili_unblock UP主名或关键词")
        bl = self._load_blocklist()
        if args in bl.get("blocked_uids", []):
            bl["blocked_uids"].remove(args)
            self._save_blocklist(bl)
            return event.plain_result(f"已解除屏蔽UP主：「{args}」")
        if args in bl.get("blocked_keywords", []):
            bl["blocked_keywords"].remove(args)
            self._save_blocklist(bl)
            return event.plain_result(f"已解除屏蔽关键词：「{args}」")
        return event.plain_result("黑名单里没有这个呢")

    def _save_blocklist(self, bl):
        with open(os.path.join(CONFIG_DIR, "blocklist.json"), "w") as f:
            json.dump(bl, f, ensure_ascii=False, indent=1)

    async def _cmd_browse_now(self, event: AstrMessageEvent):
        if not self.credential:
            yield event.plain_result("还没登录B站哦，请先发 /bili_login 扫码登录～")
            return
        yield event.plain_result("🎬 天依开始刷视频了～稍等一下")
        try:
            await self._auto_browse()
            yield event.plain_result("刷完啦～发现有趣的会存进记忆里 (๑•̀ω•́๑)")
        except Exception as e:
            yield event.plain_result(f"刷视频出错了: {e}")

    async def _cmd_history(self, event: AstrMessageEvent):
        if not os.path.exists(MEMORY_FILE):
            yield event.plain_result("还没有浏览记录哦，先让天依去刷视频吧～")
            return
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                history = json.load(f)
            if not history:
                yield event.plain_result("还没有浏览记录哦～")
                return
            lines = ["📚 天依的浏览记录："]
            for h in reversed(history[-15:]):
                score = h.get("score", 0)
                star = "⭐" * (score // 25 + 1) if score > 0 else ""
                lines.append(f"• {h['title']} 👤{h['author']} {star}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"读取记录失败: {e}")

    async def _cmd_prefs(self, event: AstrMessageEvent):
        """设置兴趣偏好"""
        text = event.get_plain_text().strip()
        # 去掉命令前缀
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            current = self.preferences.get("keywords", [])
            if current:
                yield event.plain_result(f"当前兴趣偏好：{'、'.join(current)}\n要修改的话发：/bili_prefs 猫猫, 编程, 音乐")
            else:
                yield event.plain_result("还没设置兴趣偏好哦～\n发：/bili_prefs 猫猫, 编程, 音乐\n告诉天依你喜欢看什么～")
            return
        keywords = parts[1]
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        self.preferences["keywords"] = kw_list
        self._save_preferences()
        yield event.plain_result(f"✅ 记住啦！以后天依会多刷关于「{'、'.join(kw_list)}」的视频～")

    # ==================== 识图看视频 (bili_see) ====================

    async def _cmd_see(self, event: AstrMessageEvent, bvid: str):
        """识图看视频——真正「看」画面，而不只是读文字"""
        if not bvid:
            yield event.plain_result("发一个 BV 号给天依，天依帮你真正「看」视频～\n/bili_see BV1GJ411x7")
            return
        yield event.plain_result("天依开始看视频啦，先弹幕分析一下～ (๑•̀ㅁ•́ฅ)")
        result = await self._watch_video_with_vision(bvid)
        if result:
            yield event.plain_result(result)
        else:
            yield event.plain_result("看视频失败了，可能有哪里不对 (｡•́︿•̀｡)")

    async def _get_danmaku_with_time(self, bvid):
        """获取弹幕列表，返回 [(时间_秒, 文本), ...]"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            dms = await asyncio.to_thread(lambda: sync(v.get_danmakus(page_index=0)))
            result = []
            for dm in dms:
                if hasattr(dm, "dm_time") and hasattr(dm, "text") and dm.text.strip():
                    result.append((float(dm.dm_time), dm.text.strip()))
            return result
        except Exception as e:
            logger.error(f"[BiliAgent] 获取弹幕(含时间)失败: {e}")
            return []

    def _find_danmaku_hotspots(self, dm_list, duration, window=5, top_n=5):
        """弹幕密度分析：按时间窗口统计弹幕密度，返回最密集的 top_n 个时间段"""
        if not dm_list or duration <= 0:
            return []
        times = [t for t, _ in dm_list if 0 <= t <= duration]
        if not times:
            return []
        # 滑动窗口统计
        windows = []
        step = max(1, window // 2)  # 50% 重叠
        for start in range(0, max(int(duration), 1), step):
            end = min(start + window, duration)
            count = sum(1 for t in times if start <= t < end)
            windows.append((start, end, count))
        # 按密度排序取 top
        windows.sort(key=lambda x: x[2], reverse=True)
        # 去重：如果窗口重叠，保留密度更高的
        picked = []
        for w in windows:
            if w[2] == 0:
                continue
            overlap = False
            for p in picked:
                if not (w[1] <= p[0] or w[0] >= p[1]):
                    overlap = True
                    break
            if not overlap:
                picked.append(w)
                if len(picked) >= top_n:
                    break
        return picked  # [(start_sec, end_sec, count), ...]

    async def _pick_interesting_times(self, video_title, video_desc, dm_list, hotspots):
        """用 LLM 读弹幕内容，挑出天依最感兴趣的时间段"""
        if not hotspots:
            # 如果没有热点，均匀采样
            return [10, 30, 60]
        try:
            providers = self.context.provider_manager.provider_insts
            if not providers:
                # 用默认采样
                return [h[0] + (h[1]-h[0])//2 for h in hotspots[:3]]
            provider_id = providers[0].meta().id

            # 构建弹幕上下文：每个热点窗口的弹幕内容
            hot_text = []
            for i, (start, end, count) in enumerate(hotspots[:5]):
                win_dms = [t for t, _ in dm_list if start <= t < end]
                sample = [t[1] for t in dm_list if start <= t[1] <= end]
                texts = "、".join(sample[:10])
                hot_text.append(f"时段{i+1} ({start:.0f}s-{end:.0f}s, {count}条弹幕): {texts}")

            prompt = (
                f"视频标题：{video_title}\n"
                f"视频简介：{video_desc[:100]}\n\n"
                f"以下是弹幕最密集的时段：\n"
                + "\n".join(hot_text) + "\n\n"
                "你是洛天依，一个15岁的虚拟歌手，温柔可爱，喜欢音乐和治愈系的东西。\n"
                "请根据弹幕内容，选出你最感兴趣的 1-3 个时间点（秒）。\n"
                "比如弹幕说「前方高能」说明有精彩内容，弹幕说「泪目」说明有感人画面。\n"
                "只返回秒数，用逗号分隔，例如：25, 68, 120"
            )

            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是洛天依，说话简洁直接。",
                prompt=prompt,
            )
            text = resp.get("content", "").strip()
            # 提取数字
            nums = re.findall(r'\d+', text)
            times = [int(n) for n in nums if int(n) > 0]
            if times:
                # 确保不超过视频时长
                return times[:3]
        except Exception as e:
            logger.debug(f"[BiliAgent] LLM挑时间失败: {e}")
        # 降级：用热点区间中点
        return [h[0] + (h[1]-h[0])//2 for h in hotspots[:3]]

    async def _download_and_extract_frames(self, bvid, cid, time_points):
        """下载低码率视频 + ffmpeg本地抽帧，返回 [(时间秒, base64图片), ...]"""
        import httpx, base64, subprocess

        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            dl = await v.get_download_url(cid=cid)
            stream = json.loads(json.dumps(dl, default=str))
            dash = stream.get("dash", {})
            videos = dash.get("video", [])
            if not videos:
                return []
            # 选最低码率（360p）
            min_vid = min(videos, key=lambda x: x.get("id", 9999))
            url = min_vid["baseUrl"]

            headers = {
                "Referer": "https://www.bilibili.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            os.makedirs("/tmp/bili_frames", exist_ok=True)
            video_path = f"/tmp/bili_frames/{bvid}.m4s"

            async with httpx.AsyncClient(timeout=120, headers=headers) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.error(f"[BiliAgent] 视频下载失败: {resp.status_code}")
                    return []
                with open(video_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"[BiliAgent] 视频下载完成: {len(resp.content)} bytes")

            # 抽帧
            frames = []
            for sec in time_points:
                out = f"/tmp/bili_frames/{bvid}_{sec}s.jpg"
                cmd = ["/usr/bin/ffmpeg", "-y", "-ss", str(sec), "-i", video_path,
                       "-frames:v", "1", "-q:v", "2", out]
                sp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if os.path.exists(out) and os.path.getsize(out) > 1000:
                    with open(out, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    frames.append((sec, b64))
                    logger.info(f"[BiliAgent] 抽帧 {sec}s: {os.path.getsize(out)} bytes")
                else:
                    logger.warning(f"[BiliAgent] 抽帧 {sec}s 失败")
            return frames
        except Exception as e:
            logger.error(f"[BiliAgent] 下载/抽帧失败: {e}")
            return []

    async def _vision_analyze(self, frames, video_title, dm_context, video_desc=""):
        """用智谱 GLM-4V-Flash 识图分析画面内容"""
        if not frames:
            return None
        try:
            # 从全局配置读取智谱 API key
            astrbot_root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))
            cfg_path = os.path.join(astrbot_root, "data", "cmd_config.json")
            api_key = None
            api_base = "https://open.bigmodel.cn/api/paas/v4"
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for item in cfg.get("provider_sources", []):
                if item.get("id") == "zhipu":
                    k = item.get("key", [])
                    if isinstance(k, list):
                        api_key = k[0] if k else None
                    else:
                        api_key = k
                    api_base = item.get("api_base", api_base)
                    break

            if not api_key:
                logger.error("[BiliAgent] 未找到智谱 API key")
                return None

            # 构建消息
            content = []
            # 弹幕上下文
            if dm_context:
                content.append({
                    "type": "text",
                    "text": f"视频标题：{video_title}\n视频简介：{video_desc[:200]}\n弹幕上下文：{dm_context}\n\n这是视频中几个时间点的画面截图。请仔细观察画面内容，用通俗易懂的语言描述你看到了什么，像和朋友聊天一样自然。"
                })
            else:
                content.append({
                    "type": "text",
                    "text": f"视频标题：{video_title}\n视频简介：{video_desc[:200]}\n\n这是视频中几个时间点的画面截图。请仔细观察画面内容，用通俗易懂的语言描述你看到了什么，像和朋友聊天一样自然。"
                })

            # GLM-4V-Flash 只支持单图，选中间那帧
            best_sec, best_b64 = frames[len(frames)//2]
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{best_b64}"}
            })
            content.append({
                "type": "text",
                "text": f"（这是视频第 {best_sec} 秒的画面）"
            })

            content.append({
                "type": "text",
                "text": "请像朋友分享一样告诉我这视频画面里发生了什么，画面里有什么、人物在做什么、场景怎么样。不用太正式，就像天依在跟你聊她看到的东西。"
            })

            import httpx
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "glm-4v-flash",
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 1024,
            }

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{api_base}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result = data["choices"][0]["message"]["content"]
                    return result.strip()
                else:
                    logger.error(f"[BiliAgent] 智谱识图失败: {resp.status_code} {resp.text[:200]}")
                    return None
        except Exception as e:
            logger.error(f"[BiliAgent] 识图分析异常: {e}")
            return None

    async def _watch_video_with_vision(self, bvid):
        """识图看视频主流程"""
        try:
            # 1. 获取视频信息
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            info = await v.get_info()
            title = info.get("title", "未知标题")
            desc = info.get("desc", "")
            cid = info.get("cid", 0)
            duration = info.get("duration", 0)
            if not cid:
                # 尝试从 get_download_url 获取
                dl = await v.get_download_url(cid=0)
                if dl and "cid" in dl:
                    cid = dl["cid"]
            if not cid:
                return f"视频 {bvid} 无法获取 cid"

            # 2. 获取弹幕（含时间戳）
            dm_list = await self._get_danmaku_with_time(bvid)
            dm_count = len(dm_list)
            dm_text = " | ".join([t for _, t in dm_list[:30]])

            # 3. 弹幕密度分析
            hotspots = self._find_danmaku_hotspots(dm_list, duration, window=5, top_n=5)
            hot_desc = "、".join([f"{s:.0f}s-{e:.0f}s({c}条)" for s, e, c in hotspots[:3]])

            # 4. LLM 挑感兴趣时间点
            time_points = await self._pick_interesting_times(title, desc, dm_list, hotspots)
            # 确保不超过视频时长
            time_points = [max(1, min(t, duration-2)) for t in time_points]
            # 去重
            time_points = list(dict.fromkeys(time_points))[:3]

            # 5. 下载视频 + 抽帧
            frames = await self._download_and_extract_frames(bvid, cid, time_points)
            if not frames:
                return f"天依下载了视频，但抽帧失败了 (｡•́︿•̀｡)"

            # 6. 智谱识图分析
            vision_result = await self._vision_analyze(frames, title, dm_text[:300], desc)

            # 7. 组织输出
            lines = [f"✨ 天依真的在看视频啦！\n📺 {title} ({duration}秒)"]
            lines.append(f"📊 弹幕分析：共 {dm_count} 条弹幕")
            if hot_desc:
                lines.append(f"🔥 热门时段：{hot_desc}")
            if time_points:
                points_str = "、".join([f"{t}s" for t in time_points])
                lines.append(f"👀 天依最想看：{points_str} 的画面")
            if vision_result:
                lines.append(f"\n🎨 天依看到的画面：\n{vision_result}")
            else:
                lines.append("\n😅 识图分析没成功，可能是智谱那边出了点小问题")

            # 8. 清理临时文件
            try:
                import glob
                for f in glob.glob(f"/tmp/bili_frames/{bvid}*"):
                    os.remove(f)
            except:
                pass

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[BiliAgent] 识图看视频异常: {e}")
            return None


    async def _start_public_server(self):
        """在 6288 端口启动独立 HTTP 服务，免登录访问 WebUI"""
        try:
            import asyncio, os, json, aiohttp, aiohttp.web
            from aiohttp import web

            html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
            MEMORY_FILE = os.path.join(CONFIG_DIR, "browse_history.json")

            async def handle_index(request):
                if os.path.exists(html_path):
                    with open(html_path, "r", encoding="utf-8") as f:
                        html = f.read()
                    return web.Response(text=html, content_type="text/html")
                return web.Response(text="Dashboard not found", status=404)

            async def handle_history(request):
                history = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                    except:
                        pass
                return web.json_response({"status": "ok", "data": history})

            async def handle_status(request):
                # 直接读 Cookie 文件，不依赖内存
                _logged_in = False
                _uid = None
                try:
                    if os.path.exists(os.path.join(CONFIG_DIR, 'cookies.json')):
                        with open(os.path.join(CONFIG_DIR, 'cookies.json')) as _f:
                            _c = json.load(_f)
                        if _c.get('SESSDATA'):
                            _logged_in = True
                            _uid = _c.get('DedeUserID', '')
                except:
                    pass
                logged_in = _logged_in
                keywords = self.preferences.get("keywords", [])
                history = []
                today = 0
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            history = json.load(f)
                        from datetime import datetime, timezone
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        for h in history:
                            if h.get("time", "").startswith(today_str):
                                today += 1
                    except:
                        pass
                return web.json_response({
                    "status": "ok", "data": {
                        "loggedIn": logged_in, "uid": _uid,
                        "keywords": keywords, "todayCount": today,
                        "totalCount": len(history),
                    }
                })

            async def handle_commentary(request):
                import re
                bvid = request.query.get("bvid", "")
                # 从路径提取
                path = request.path
                bv_match = re.search(r"BV[0-9A-Za-z]{10,}", path)
                if bv_match:
                    bvid = bv_match.group(0)
                if not bvid:
                    return web.json_response({"status": "error", "message": "请提供BV号"})
                try:
                    info = await self._get_video_info(bvid)
                    if not info:
                        return web.json_response({"status": "error", "message": "获取视频信息失败"})
                    subtitle, danmaku = await asyncio.gather(
                        self._get_video_subtitle(bvid),
                        self._get_video_danmaku(bvid, 100),
                    )
                    minutes = (info.get("duration", 0) or 0) // 60
                    seconds = (info.get("duration", 0) or 0) % 60
                    sections = []
                    if subtitle:
                        sentences = [s.strip() for s in subtitle.replace("。", "。;").replace("！", "！;").replace("？", "？;").split(";") if s.strip()]
                        chunk_size = max(1, len(sentences) // 5)
                        for i in range(0, min(len(sentences), 25), chunk_size):
                            chunk = sentences[i:i+chunk_size]
                            text = "".join(chunk)[:120]
                            progress = int((i / max(len(sentences), 1)) * 100)
                            t = f"{int(progress * minutes // 100)}:{str(int(progress * seconds // 100)).zfill(2)}"
                            if text:
                                sections.append({"time": t, "text": text})
                    danmaku_list = danmaku.split(" | ")[:8] if danmaku else []
                    return web.json_response({
                        "status": "ok", "data": {
                            "title": info["title"], "author": info["author"],
                            "duration": f"{minutes}:{str(seconds).zfill(2)}",
                            "view": info.get("view", 0), "like": info.get("like", 0),
                            "sections": sections, "danmaku": danmaku_list,
                        }
                    })
                except Exception as e:
                    return web.json_response({"status": "error", "message": str(e)})

            async def handle_prefs(request):
                try:
                    body = await request.json()
                    keywords = body.get("keywords", "")
                    if keywords:
                        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
                        self.preferences["keywords"] = kw_list
                        self._save_preferences()
                        return web.json_response({"status": "ok", "message": f"已保存偏好：{', '.join(kw_list)}"})
                    return web.json_response({"status": "error", "message": "请提供关键词"})
                except Exception as e:
                    return web.json_response({"status": "error", "message": str(e)})

            async def handle_memory(request):
                mem = []
                if os.path.exists(MEMORY_FILE):
                    try:
                        with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                            mem = json.load(f)
                    except:
                        pass
                mem.sort(key=lambda h: h.get("score", 0), reverse=True)
                return web.json_response({"status": "ok", "data": mem[:20]})

            async def handle_mood(request):
                return web.json_response({"status": "ok", "data": self.mood})

            async def handle_notes(request):
                filepath = request.match_info.get("filepath", "")
                if not filepath:
                    return web.Response(text="请指定文件名", status=404)
                notes_dir = os.path.join(CONFIG_DIR, "notes")
                full_path = os.path.join(notes_dir, filepath)
                real = os.path.realpath(full_path)
                if not real.startswith(os.path.realpath(notes_dir)):
                    return web.Response(text="路径非法", status=403)
                if os.path.exists(real):
                    with open(real, "r", encoding="utf-8") as f:
                        html = f.read()
                    return web.Response(text=html, content_type="text/html")
                return web.Response(text="文件不存在", status=404)

            app = web.Application()
            app.router.add_get("/", handle_index)
            app.router.add_get("/history", handle_history)
            app.router.add_get("/status", handle_status)
            app.router.add_get("/commentary", handle_commentary)
            app.router.add_get("/commentary/{bvid}", handle_commentary)
            app.router.add_post("/prefs", handle_prefs)
            app.router.add_get("/memory", handle_memory)
            app.router.add_get("/mood", handle_mood)
            app.router.add_get("/notes/{filepath:.*}", handle_notes)

            # 在后台启动
            asyncio.create_task(self._run_public_server(app))
            logger.info("[BiliAgent] 免登录面板已启动 → http://localhost:6288")
        except Exception as e:
            logger.warning(f"[BiliAgent] 免登录面板启动失败: {e}")

    async def _run_public_server(self, app):
        """运行独立 HTTP 服务"""
        import aiohttp, aiohttp.web
        try:
            runner = aiohttp.web.AppRunner(app)
            await runner.setup()
            site = aiohttp.web.TCPSite(runner, "0.0.0.0", 6288)
            await site.start()
            logger.info("[BiliAgent] 免登录面板 http://localhost:6288 已就绪")
        except Exception as e:
            logger.warning(f"[BiliAgent] 免登录面板启动失败: {e}")
