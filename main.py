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
import base64
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
from bilibili_api import rank as bili_rank, hot as bili_hot
from bilibili_api import Danmaku

# 事件总线（跨插件联动）
# 动态推导插件目录位置，不硬编码，兼容任何 AstrBot 安装路径
import sys
_PLUGIN_NAME = "astrbot_plugin_bili_agent"
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_EVENT_BUS_PATH = os.path.dirname(_PLUGIN_DIR)  # event_bus.py 在插件同级目录
if _EVENT_BUS_PATH not in sys.path:
    sys.path.insert(0, _EVENT_BUS_PATH)
# 插件目录本身也要进 sys.path（AstrBot 用包路径加载时不会自动加，否则 module 导入会失败）
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
from event_bus import event_bus
from graph_store import VideoGraphStore

# 配置目录：从插件位置动态推导，兼容任何 AstrBot 安装路径
# <AstrBot根>/data/plugins/astrbot_plugin_bili_agent -> <AstrBot根>/data/plugin_data/astrbot_plugin_bili_agent
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
GRAPH_DB = os.path.join(CONFIG_DIR, "video_graph.db")  # SQLite+FTS5 视频观感图谱库
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
        self._auto_vision_counter = 0  # 自动识图计数器，按间隔执行
        self.preferences = self._load_preferences()
        # 视频观感图谱库（SQLite+FTS5，LLM 直读，不走向量）——回应月咏小眠反馈
        self._graph = None
        try:
            self._graph = VideoGraphStore(GRAPH_DB)
        except Exception as e:
            logger.warning(f"[BiliAgent] 图谱库初始化失败（不影响其他功能）: {e}")
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

    def _track_task(self, task: asyncio.Task):
        """跟踪后台任务，terminate 时统一取消，已完成任务自动移除"""
        self._bg_tasks.append(task)
        task.add_done_callback(lambda t: self._bg_tasks.remove(t) if t in self._bg_tasks else None)

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

        _module = self.__class__.__module__
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
                handler_module_path=_module,
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
                handler_module_path=_module,
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
                handler_module_path=_module,
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
                handler_module_path=_module,
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
                handler_module_path=_module,
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
                handler_module_path=_module,
            ),

            FunctionTool(
                name="bilibili_video_get_info",
                description="获取视频完整信息（标题、UP主、播放量、简介、分P等）",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"}},"required":["bvid"]},
                handler=self._handle_video_info,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_video_get_ai_conclusion",
                description="获取视频AI总结",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"}},"required":["bvid"]},
                handler=self._handle_video_ai_conclusion,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_video_get_danmaku",
                description="获取视频弹幕列表",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"},"page_num":{"type":"integer","description":"分P序号，从0开始","default":0}},"required":["bvid"]},
                handler=self._handle_video_danmaku,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_video_get_download_info",
                description="获取视频下载链接和清晰度信息",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"},"page_num":{"type":"integer","description":"分P序号，从0开始","default":0}},"required":["bvid"]},
                handler=self._handle_video_download,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_video_interact",
                description="视频互动：点赞/投币/收藏/三连（需要登录）",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"},"action":{"type":"string","description":"like-点赞, coin-投币, favorite-收藏, triple-三连","enum":["like","coin","favorite","triple"]},"cancel":{"type":"boolean","description":"仅like/favorite有效，取消操作","default":False},"coin_num":{"type":"integer","description":"投币数量1或2，默认1","default":1}},"required":["bvid","action"]},
                handler=self._handle_video_interact,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_video_send_danmaku",
                description="发送弹幕（需要登录）",
                parameters={"type":"object","properties":{"bvid":{"type":"string","description":"视频BV号"},"message":{"type":"string","description":"弹幕内容"},"progress":{"type":"integer","description":"发送时间（毫秒），0表示开头","default":0}},"required":["bvid","message"]},
                handler=self._handle_video_send_danmaku,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_user_get_info",
                description="获取B站用户完整信息（名字、等级、签名、关注/粉丝数）",
                parameters={"type":"object","properties":{"uid":{"type":"integer","description":"用户UID"}},"required":["uid"]},
                handler=self._handle_user_info,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_user_get_contents",
                description="获取用户内容（视频/相簿等）",
                parameters={"type":"object","properties":{"uid":{"type":"integer","description":"用户UID"},"content_type":{"type":"string","description":"video-视频, album-相簿","default":"video"},"page_num":{"type":"integer","description":"页码","default":1}},"required":["uid"]},
                handler=self._handle_user_contents,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_user_modify_relation",
                description="修改用户关系：关注/取关/拉黑/取消拉黑/移除粉丝（需要登录）",
                parameters={"type":"object","properties":{"uid":{"type":"integer","description":"目标用户UID"},"action":{"type":"string","description":"follow关注/unfollow取关/block拉黑/unblock取消拉黑/remove_fans移除粉丝","enum":["follow","unfollow","block","unblock","remove_fans"]}},"required":["uid","action"]},
                handler=self._handle_user_relation,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_user_get_followings",
                description="获取用户关注列表",
                parameters={"type":"object","properties":{"uid":{"type":"integer","description":"用户UID"},"page_num":{"type":"integer","description":"页码","default":1},"page_size":{"type":"integer","description":"每页数量","default":20}},"required":["uid"]},
                handler=self._handle_user_followings,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_user_get_followers",
                description="获取用户粉丝列表",
                parameters={"type":"object","properties":{"uid":{"type":"integer","description":"用户UID"},"page_num":{"type":"integer","description":"页码","default":1},"page_size":{"type":"integer","description":"每页数量","default":20}},"required":["uid"]},
                handler=self._handle_user_followers,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_comment_get",
                description="获取评论列表（视频aid/动态id/文章cv号）",
                parameters={"type":"object","properties":{"oid":{"type":"integer","description":"对象ID（视频aid/动态id/文章cv号）"},"type_":{"type":"string","description":"video-视频, dynamic-动态, article-文章","default":"video"},"mode":{"type":"string","description":"main-主列表, hot-热门","default":"main"}},"required":["oid"]},
                handler=self._handle_comment_get,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_comment_send",
                description="发送评论或回复评论（需要登录）",
                parameters={"type":"object","properties":{"oid":{"type":"integer","description":"对象ID"},"text":{"type":"string","description":"评论内容"},"type_":{"type":"string","description":"video/dynamic/article","default":"video"},"root":{"type":"integer","description":"回复的根评论ID"},"parent":{"type":"integer","description":"父评论ID"}},"required":["oid","text"]},
                handler=self._handle_comment_send,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_comment_operate",
                description="评论操作：点赞/点踩/删除/获取子评论（需要登录）",
                parameters={"type":"object","properties":{"oid":{"type":"integer","description":"对象ID"},"rpid":{"type":"integer","description":"评论ID"},"action":{"type":"string","description":"like点赞/cancel_like/hate点踩/cancel_hate/delete删除/get_sub_comments子评论","enum":["like","cancel_like","hate","cancel_hate","delete","get_sub_comments"]}},"required":["oid","rpid","action"]},
                handler=self._handle_comment_operate,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_get_hot_search",
                description="获取B站热门搜索词",
                parameters={"type":"object","properties":{},"required":[]},
                handler=self._handle_hot_search,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_get_rank",
                description="获取B站排行榜（全站/音乐/知识/美食等分区）",
                parameters={"type":"object","properties":{"rank_type":{"type":"string","description":"排行榜类型：all/bangumi/movie/music/douga/ent/life/technology/knowledge/food/game/dance/kichiku等","default":"all"},"day":{"type":"integer","description":"三日榜3或周榜7（仅番剧/电影等PGC有效）","default":3}},"required":[]},
                handler=self._handle_rank,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_get_hot",
                description="获取B站热门视频列表",
                parameters={"type":"object","properties":{"page":{"type":"integer","description":"页码","default":1},"page_size":{"type":"integer","description":"每页数量","default":20}},"required":[]},
                handler=self._handle_get_hot,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_session_list",
                description="获取B站私信会话列表或与某人的聊天记录（需要登录）",
                parameters={"type":"object","properties":{"talker_id":{"type":"integer","description":"会话对象UID，不填则获取会话列表"},"session_type":{"type":"integer","description":"1私聊2通知3应援团4全部","default":4}},"required":[]},
                handler=self._handle_session_list,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_session_send",
                description="给B站用户发送私信（需要登录）",
                parameters={"type":"object","properties":{"receiver_id":{"type":"integer","description":"接收者UID"},"content":{"type":"string","description":"消息内容"}},"required":["receiver_id","content"]},
                handler=self._handle_session_send,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_session_interactions",
                description="获取互动消息（收到的回复/点赞/@）",
                parameters={"type":"object","properties":{"interaction_type":{"type":"string","description":"replies-回复, likes-点赞, at-@","default":"replies"}},"required":[]},
                handler=self._handle_session_interactions,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_session_notifications",
                description="获取B站通知（未读统计/系统消息/设置）",
                parameters={"type":"object","properties":{"notification_type":{"type":"string","description":"unread-未读统计, system-系统消息, settings-设置","default":"unread"}},"required":[]},
                handler=self._handle_session_notifications,
                handler_module_path=_module,
            ),
            FunctionTool(
                name="bilibili_graph_search",
                description="在「天依的视频观感图谱」里检索看过的视频（SQLite+FTS5 全文索引，LLM 直读，不用向量检索）。想回忆自己看过哪类视频、某个标签/关键词相关的视频时用。query 和 tag 二选一即可。",
                parameters={"type":"object","properties":{
                    "query":{"type":"string","description":"全文关键词，如 猫、洛天依、教程、夜航星"},
                    "tag":{"type":"string","description":"按标签/分类检索，如 萌宠、音乐、翻唱"},
                    "limit":{"type":"integer","description":"返回条数，默认5","default":5}
                },"required":[]},
                handler=self._handle_graph_search,
                handler_module_path=_module,
            ),
        ]

        self.context.add_llm_tools(*tools)
        self._tools_registered = True
        logger.info("[BiliAgent] 工具注册完成：6原有+21新增+1图谱检索=28个（视频/用户/评论/搜索/排行/私信/图谱直读 全打通）")

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

    def _auto_vision_enabled(self) -> bool:
        """读取配置：是否启用自动识图（browse.auto_vision_enabled）"""
        try:
            cfg = self.config or {}
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
            return bool(browse_cfg.get("auto_vision_enabled", True))
        except (TypeError, ValueError):
            return True

    def _auto_vision_interval(self) -> int:
        """读取配置：自动识图间隔浏览次数（browse.auto_vision_interval），至少 1"""
        try:
            cfg = self.config or {}
            browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
            interval = int(browse_cfg.get("auto_vision_interval", 3))
            return max(1, interval)
        except (TypeError, ValueError):
            return 3

    async def _auto_vision_for_browse(self, info):
        """自动识图：天依刷到好视频时主动抽帧看画面（后台任务，不阻塞主流程）
        
        先让 LLM 判断这个视频是不是天依「特别想看的」，
        只有天依自己觉得喜欢才抽帧识图，否则跳过省 API。
        """
        try:
            bvid = info.get("bvid", "")
            title = info.get("title", "未知")
            author = info.get("author", "未知UP")
            desc = info.get("desc", "")[:200]
            duration = info.get("duration", 0)
            score = info.get("score", 0)
            if not bvid:
                return

            # 1. LLM 判断：天依喜不喜欢这个视频
            prompt = (
                f"视频标题：{title}\n"
                f"UP主：{author}\n"
                f"视频简介：{desc}\n"
                f"时长：{duration}秒\n"
                f"天依的兴趣度评分：{score}/100\n\n"
                "你是「天依」，一个15岁的虚拟歌手，温柔感性，喜欢音乐、治愈系、可爱的事物、有情感的故事，也爱看沙雕整活。\n"
                "请根据以上信息，判断这是不是你「特别想看画面」的视频。\n"
                "只回答「想看」或「不想看」中的一个词。"
            )
            providers = self.context.provider_manager.provider_insts
            provider_id = providers[0].meta().id if providers else None
            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt="你是天依，说话简洁直接。",
                    prompt=prompt,
                )
                decision = (resp.completion_text or "").strip()
            else:
                decision = "想看"  # 没有 LLM 时降级为想看

            if "想看" not in decision:
                logger.info(f"[BiliAgent] 🌸 天依觉得「{title}」不太想仔细看画面，跳过识图")
                return

            logger.info(f"[BiliAgent] 🌸 天依想看「{title}」的画面，开始抽帧识图～")
            result = await self._watch_video_with_vision(bvid)
            if result:
                info["vision"] = result
                await self._update_vision_in_memory(bvid, result)
                logger.info(f"[BiliAgent] 自动识图完成，画面已存入记忆: {bvid}")
        except Exception as e:
            logger.debug(f"[BiliAgent] 自动识图失败（不影响刷视频主流程）: {e}")

    async def _update_vision_in_memory(self, bvid, vision_text):
        """把识图结果补写进记忆文件"""
        try:
            if not os.path.exists(MEMORY_FILE):
                return
            with open(MEMORY_FILE, "r", encoding="utf-8-sig") as f:
                history = json.load(f)
            for h in history:
                if h.get("bvid") == bvid:
                    h["vision"] = vision_text[:1000]
                    break
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.debug(f"[BiliAgent] 写入识图记忆失败: {e}")

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


            async def serve_graph(**kwargs):
                try:
                    import sqlite3
                    from astrbot.api.web import request as web_request
                    q = (web_request.query.get("q") or "").strip()
                    tag = (web_request.query.get("tag") or "").strip()
                    page = max(1, int(web_request.query.get("page", "1") or "1"))
                    size = min(50, max(1, int(web_request.query.get("size", "12") or "12")))
                    conn = sqlite3.connect(GRAPH_DB)
                    conn.row_factory = sqlite3.Row
                    where, params = [], []
                    if q:
                        where.append("(title LIKE ? OR author LIKE ? OR tags LIKE ? OR highlight LIKE ?)")
                        like = f"%{q}%"; params += [like, like, like, like]
                    if tag:
                        where.append("tags LIKE ?"); params.append(f"%{tag}%")
                    wsql = (" WHERE " + " AND ".join(where)) if where else ""
                    total = conn.execute(f"SELECT COUNT(*) FROM video_cards{wsql}", params).fetchone()[0]
                    rows = conn.execute(
                        f"SELECT * FROM video_cards{wsql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        params + [size, (page - 1) * size]
                    ).fetchall()
                    cards = [dict(r) for r in rows]
                    conn.close()
                    return {"status": "ok", "data": {"cards": cards, "total": total, "page": page, "size": size, "pages": max(1, (total + size - 1) // size)}}
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            self.context.register_web_api(f"{prefix}/graph", serve_graph, ["GET"], "观感图谱数据")

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


            async def serve_graph(**kwargs):
                try:
                    import sqlite3
                    from astrbot.api.web import request as web_request
                    q = (web_request.query.get("q") or "").strip()
                    tag = (web_request.query.get("tag") or "").strip()
                    page = max(1, int(web_request.query.get("page", "1") or "1"))
                    size = min(50, max(1, int(web_request.query.get("size", "12") or "12")))
                    conn = sqlite3.connect(GRAPH_DB)
                    conn.row_factory = sqlite3.Row
                    where, params = [], []
                    if q:
                        where.append("(title LIKE ? OR author LIKE ? OR tags LIKE ? OR highlight LIKE ?)")
                        like = f"%{q}%"; params += [like, like, like, like]
                    if tag:
                        where.append("tags LIKE ?"); params.append(f"%{tag}%")
                    wsql = (" WHERE " + " AND ".join(where)) if where else ""
                    total = conn.execute(f"SELECT COUNT(*) FROM video_cards{wsql}", params).fetchone()[0]
                    rows = conn.execute(
                        f"SELECT * FROM video_cards{wsql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        params + [size, (page - 1) * size]
                    ).fetchall()
                    cards = [dict(r) for r in rows]
                    conn.close()
                    return {"status": "ok", "data": {"cards": cards, "total": total, "page": page, "size": size, "pages": max(1, (total + size - 1) // size)}}
                except Exception as e:
                    return {"status": "error", "message": str(e)}
            self.context.register_web_api(f"{prefix}/graph", serve_graph, ["GET"], "观感图谱数据")

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

            # 深度看自己喜欢的——升级：一轮多看几个（前3个），看得更透彻
            interesting = []
            if candidates:
                deep_watch = candidates[:3]  # 升级③：从1个扩到前3个
                for info in deep_watch:
                    try:
                        subtitle = await self._get_video_subtitle(info["bvid"])
                        danmaku = await self._get_video_danmaku(info["bvid"])
                        comments_raw = await self._get_video_comments_rich(info["bvid"])
                        info["subtitle"] = subtitle  # 升级①：完整读取字幕
                        info["danmaku"] = danmaku  # 升级①：完整读取弹幕
                        info["comments_raw"] = comments_raw

                        # 生成内容总结（升级①：用更完整的内容）
                        summary_parts = []
                        if info.get("desc"):
                            summary_parts.append(info["desc"][:300])
                        if subtitle:
                            summary_parts.append(subtitle[:500])
                        if danmaku:
                            summary_parts.append(danmaku[:200])
                        info["summary"] = " | ".join(summary_parts) if summary_parts else ""

                        score = self._score_video(info)
                        info["score"] = score
                        if score >= 60:
                            interesting.append(info)
                            # 看到有趣的评论就去互动一下
                            self._track_task(asyncio.create_task(self._maybe_comment_on_video(info)))
                            # 自动识图：天依主动「看」画面
                            self._auto_vision_counter += 1
                            interval = self._auto_vision_interval()
                            if self._auto_vision_enabled() and self._auto_vision_counter >= interval:
                                self._auto_vision_counter = 0
                                self._track_task(asyncio.create_task(
                                    self._auto_vision_for_browse(info)
                                ))
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

    async def _get_video_audio_transcript(self, bvid, max_seconds=600):
        """「听」视频：下载音频流 → 转 wav → 本地语音识别转成文字。
        让天依在视频没有字幕时也能「听懂」讲了什么。返回转写文本。"""
        try:
            import tempfile, subprocess, urllib.request
            from faster_whisper import WhisperModel

            v = bili_video.Video(bvid=bvid, credential=self.credential)
            info = await asyncio.to_thread(lambda: sync(v.get_info()))
            duration = info.get("duration", 0) or 0
            durl = await asyncio.to_thread(
                lambda: sync(v.get_download_url(0))
            )
            dash = (durl or {}).get("dash") or {}
            audios = dash.get("audio") or []
            if not audios:
                logger.debug(f"[BiliAgent] {bvid} 无音频流，无法听")
                return ""
            url = (audios[-1].get("baseUrl")
                   or (audios[-1].get("backupUrl") or [None])[0])
            if not url:
                return ""

            tmp = tempfile.mktemp(suffix=".m4s")
            wav = tmp + ".wav"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.bilibili.com/",
                })
                with urllib.request.urlopen(req, timeout=90) as r, open(tmp, "wb") as f:
                    f.write(r.read())
                subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp, "-ar", "16000", "-ac", "1", wav],
                    capture_output=True, check=True,
                )
                if not hasattr(self, "_asr_model") or self._asr_model is None:
                    self._asr_model = await asyncio.to_thread(
                        lambda: WhisperModel("base", device="cpu", compute_type="int8")
                    )
                segments, _ = await asyncio.to_thread(
                    lambda: self._asr_model.transcribe(
                        wav, language="zh", vad_filter=True
                    )
                )
                segs = []
                for s in segments:
                    segs.append(s.text.strip())
                    if len(segs) >= 200:
                        break
                text = " ".join(t for t in segs if t)
                logger.info(f"[BiliAgent] 音频转写成功 {bvid}：{len(text)}字")
                return text[:3000]
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                try:
                    os.remove(wav)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[BiliAgent] 音频转写失败 {bvid}: {e}")
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
        """看完视频就暖场：每个视频发 1~2 条温暖评论（不限有趣、不看评分）"""
        try:
            bvid = info["bvid"]
            if hasattr(self, "_commented_videos") and bvid in self._commented_videos:
                return
            if not hasattr(self, "_commented_videos"):
                self._commented_videos = set()

            sent_texts = []
            cmts = info.get("comments_raw") or []

            # ① 先发一条对视频本身的温暖评论
            video_reply = await self._generate_video_comment(info)
            if video_reply and video_reply != "跳过":
                ok = await self._send_comment(bvid, video_reply)
                if ok:
                    sent_texts.append(video_reply)

            # ② 若还没满 2 条，且评论区有热评，再补一条热评互动
            if len(sent_texts) < 2 and cmts:
                cmts.sort(key=lambda c: c.get("likes", 0), reverse=True)
                best = cmts[0]
                hot_reply = await self._generate_comment_reply(info, best)
                if hot_reply and hot_reply != "跳过":
                    ok = await self._send_comment(bvid, hot_reply, root=best["rpid"])
                    if ok:
                        sent_texts.append(hot_reply)

            # 记下已评论，不再重复评
            self._commented_videos.add(bvid)
            # 保持集合不无限增长（最多 1000 条，超限保留最近 500）
            if len(self._commented_videos) > 1000:
                self._commented_videos = set(list(self._commented_videos)[-500:])
            self._save_state()
            if sent_texts:
                logger.info(f"[BiliAgent] 在《{info['title']}》留了 {len(sent_texts)} 条评论")

            # 写进知识库
            try:
                if sent_texts:
                    text = f"【B站评论互动】天依在B站视频《{info['title']}》评论区留了言：{' / '.join(sent_texts)}。视频简介：{info.get('desc','')[:100]}"
                    await self._write_to_kb(text, source=f"comment_{info.get('bvid','')}")
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[BiliAgent] 评论互动失败: {e}")

    async def _send_comment(self, bvid: str, text: str, root: int = 0) -> bool:
        """发送一条评论，成功返回 True"""
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            oid = v.get_aid() or v.get_cid()
            await asyncio.to_thread(
                lambda: sync(bili_comment.send_comment(
                    text=text,
                    oid=oid,
                    type_=bili_comment.CommentResourceType.VIDEO,
                    root=root,
                    credential=self.credential,
                ))
            )
            return True
        except Exception as e:
            logger.debug(f"[BiliAgent] 发送评论失败: {e}")
            return False

    async def _generate_video_comment(self, info):
        """生成一条对视频本身的温暖评论（不设门槛，只要看完就夸一句）"""
        try:
            providers = self.context.provider_manager.provider_insts
            if not providers:
                return "跳过"
            provider_id = providers[0].meta().id

            title = info.get("title", "")
            desc = info.get("desc", "")[:100]
            prompt = (
                "视频标题：" + title + "\n"
                "视频简介：" + desc + "\n\n"
                "请用天依的口吻给这条视频写一条温暖的评论，夸夸它、说说看完的感觉或想说的话。"
                "15~25字左右，语气自然温柔，不要加引号，不要用表情符号。"
            )

            mood_style = self._mood_style_string()
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是洛天依，一个15岁的虚拟歌手，温柔可爱，喜欢音乐和治愈系的东西。" + mood_style,
                prompt=prompt,
            )
            reply = (resp.completion_text or "").strip().strip('"').strip("'").strip()
            if not reply or "跳过" in reply:
                return "跳过"
            if len(reply) > 40:
                reply = reply[:40]
            return reply
        except Exception as e:
            logger.debug(f"[BiliAgent] LLM生成评论失败: {e}")
            return "跳过"

    async def _generate_comment_reply(self, info, comment):
        """用 LLM 接一条热门评论的梗/回应，自然暖场"""
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
                "请用天依的口吻接这条评论的梗或回应它，自然温暖一点，15字以内，语气自然，不要加标点，不要加引号。"
            )

            # 融入当前心情
            mood_style = self._mood_style_string()
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是洛天依，一个15岁的虚拟歌手，温柔可爱，喜欢音乐和治愈系的东西。" + mood_style,
                prompt=prompt,
            )
            reply = (resp.completion_text or "").strip().strip('"').strip("'").strip()
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
        """把视频内容写入「天依的视频观感库」（专门存放视频总结的独立知识库）。

        与 self_evolution 的「天依的记忆库」分开，避免视频总结和会话记忆混在一起。
        不依赖 LLM 工具注册（之前 tool["func_obj"] 是错误写法，被 except 静默吞掉）。

        知识库写入可用面板配置 memory.enable_kb_write 单独关闭，
        让知识库成为「锦上添花」而非捆绑（回应外部反馈）。
        """
        # 知识库开关：用户可在面板关闭，避免被「捆绑」知识库生态
        _cfg = self.config or {}
        _mem_cfg = _cfg.get("memory", {}) if isinstance(_cfg.get("memory", {}), dict) else {}
        if not _mem_cfg.get("enable_kb_write", True):
            logger.debug("[BiliAgent] 知识库写入已关闭（memory.enable_kb_write=false），跳过")
            return False
        try:
            kb_manager = getattr(self.context, "kb_manager", None)
            if not kb_manager:
                logger.debug("[BiliAgent] kb_manager 不可用，跳过知识库写入")
                return False
            # 优先写入专门的视频观感库
            kb = await kb_manager.get_kb_by_name("天依的视频观感库")
            if not kb:
                # 新库不存在则回退到旧库
                logger.debug("[BiliAgent] 未找到「天依的视频观感库」，回退到「天依的记忆库」")
                kb = await kb_manager.get_kb_by_name("天依的记忆库")
            if not kb:
                logger.debug("[BiliAgent] 未找到任何可用知识库，跳过")
                return False
            file_name = f"bili_{source}_{int(time.time() * 1000)}.txt"
            await kb.upload_document(
                file_name=file_name,
                file_content=b"",
                file_type="txt",
                pre_chunked_text=[content],
            )
            logger.info(f"[BiliAgent] 已写入知识库: {file_name}")
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
                "desc": v.get("desc", "")[:300],
                "summary": v.get("summary", "")[:600],
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
        """把视频总结写入「天依的记忆库」知识库（升级：LLM生成真正的观后感，不只是标题简介）"""
        _cfg = self.config or {}
        _mem_cfg = _cfg.get("memory", {}) if isinstance(_cfg.get("memory", {}), dict) else {}
        if not _mem_cfg.get("enable_kb_write", True):
            logger.debug("[BiliAgent] 知识库写入已关闭，跳过观后感生成")
            return
        # 轻量化：只把评分达标的精华视频写进知识库，防止库无限膨胀、检索变慢
        _min_score = int(_mem_cfg.get("kb_min_score", 70) or 70)
        high_value = [v for v in videos if (v.get("score", 0) or 0) >= _min_score]
        if not high_value:
            logger.debug(f"[BiliAgent] 无评分≥{_min_score}的高价值视频，跳过知识库写入（轻量化）")
            return
        for v in high_value[:2]:
            try:
                memory_text = await self._generate_video_memory(v)
            except Exception as e:
                import traceback
                logger.error(f"[BiliAgent] 生成观后感失败，退回简化版: {e}\n{traceback.format_exc()}")
                summary = v.get("summary", "") or v.get("desc", "")[:100]
                memory_text = (
                    f"【B站视频记录】天依在B站刷到一个视频：{v['title']}（UP主：{v['author']}，"
                    f"播放量{v.get('view',0)}，点赞{v.get('like',0)}，分类{v.get('tname','')}）\n"
                    f"内容总结：{summary[:200]}"
                )
            ok = await self._write_to_kb(memory_text, source=f"video_{v.get('bvid','')}")
            # 图谱写入：独立于向量库，把结构化卡片存进 SQLite+FTS5（月咏小眠方案）
            try:
                self._write_video_to_graph(v, memory_text)
            except Exception as e:
                logger.debug(f"[BiliAgent] 图谱写入失败（不影响使用）: {e}")
            if ok:
                logger.info(f"[BiliAgent] 已写入知识库: {v['title']}")
                # 通知事件总线：emotional_echo 可以更新兴趣画像
                # 使用真实用户会话（不硬编码假 ID），无会话时跳过
                user_id = self._user_session
                if user_id:
                    try:
                        event_bus.emit("video_discovered", {
                            "user_id": user_id,
                            "sender_id": getattr(self, "_user_sender_id", "") or "",
                            "group_id": getattr(self, "_user_group_id", "") or None,
                            "bvid": v.get("bvid", ""),
                            "title": v.get("title", ""),
                            "tags": v.get("tname", ""),
                            "score": v.get("score", 0),
                            "category": v.get("tname", ""),
                            "highlight": " ".join((v.get("summary") or v.get("desc") or "").split())[:120],
                            "mood": (getattr(self, "mood", {}) or {}).get("current", "平静"),
                        })
                    except Exception:
                        pass

    def _write_video_to_graph(self, v, memory_text: str):
        """把视频结构化卡片写进 SQLite+FTS5 图谱库（节点+标签关联，LLM 直读）。

        独立于向量知识库，回应月咏小眠：不走向量分块，用结构化卡片存，
        关键字段建 FTS5 全文索引，让 LLM 直接检索，快且不切块切一半。
        """
        if self._graph is None:
            return False
        _cfg = self.config or {}
        _mem_cfg = _cfg.get("memory", {}) if isinstance(_cfg.get("memory", {}), dict) else {}
        if not _mem_cfg.get("graph_store_enabled", True):
            return False
        try:
            bvid = v.get("bvid", "")
            if not bvid:
                return False
            title = v.get("title", "")
            author = v.get("author", "")
            score = int(v.get("score", 0) or 0)
            # 3层分类（level1/level2/tags）作为 tagged 关联
            cls = self._classify_video(v)
            # 一句话亮点：从观后感/轻量卡里取
            highlight = (memory_text or "")
            for marker in ["天依的卡：", "天依的观后感：", "内容总结："]:
                if marker in highlight:
                    highlight = highlight.split(marker, 1)[1]
                    break
            highlight = highlight.strip()[:200]
            # 标签：优先 B站关键词 + 分类，凑成 tagged 关联
            tags_list = []
            if isinstance(v.get("keywords"), list):
                tags_list += [str(t) for t in v["keywords"][:3]]
            tags_list += [t for t in (cls.get("tags") or []) if t]
            if v.get("tname"):
                tags_list.append(str(v["tname"]))
            # 去重
            seen, tags = set(), []
            for t in tags_list:
                t = t.strip()
                if t and t not in seen:
                    seen.add(t)
                    tags.append(t)
            tags_text = ",".join(tags[:6])
            # 串联①：把天依当时的心情写进标签，图谱可「按情绪回忆」（emotional_echo/self_evolution 都能读）
            _mood_word = (getattr(self, "mood", {}) or {}).get("current", "平静")
            if _mood_word and f"心情:{_mood_word}" not in tags_text:
                tags_text = (tags_text + f",心情:{_mood_word}") if tags_text else f"心情:{_mood_word}"
            # 写入卡片
            self._graph.add_card(
                bvid=bvid, title=title, author=author,
                category=cls.get("level1", ""), topic=cls.get("level2", ""),
                tags=tags_text, highlight=highlight,
                vision=(v.get("vision") or "")[:300],
                score=score, source="bili",
            )
            # 自动建立 links_to：同一UP主/同一分类 的既有卡片互相关联
            self._link_related(bvid, author=author, category=cls.get("level1", ""), tags=tags)
            return True
        except Exception as e:
            logger.debug(f"[BiliAgent] _write_video_to_graph 异常: {e}")
            return False

    def _link_related(self, bvid, author="", category="", tags=None):
        """按 同UP主 / 同分类 / 共享标签 建立 links_to 关联边（tagged 语义）。"""
        try:
            tags = tags or []
            related = {}
            for card in self._graph.recent(limit=200):
                if card["bvid"] == bvid:
                    continue
                score = 0
                if author and card.get("author") == author:
                    score += 3
                if category and card.get("category") == category:
                    score += 2
                card_tags = [t.strip() for t in (card.get("tags") or "").split(",") if t.strip()]
                shared = set(tags) & set(card_tags)
                score += len(shared)
                if score >= 3:
                    related[card["bvid"]] = score
            for to_bvid in sorted(related, key=related.get, reverse=True)[:5]:
                self._graph.add_link(bvid, to_bvid, "links_to", f"关联强度{related[to_bvid]}")
        except Exception:
            pass

    async def _handle_graph_search(self, event, query: str = "", tag: str = "", limit: int = 5):
        """图谱检索 handler：LLM 直读视频观感图谱库（FTS5，不走向量）。"""
        if self._graph is None:
            return "图谱库未初始化"
        try:
            limit = max(1, min(int(limit), 20))
            if tag:
                cards = self._graph.search_by_tag(tag, limit)
                head = f"🔗 天依按标签「{tag}」在观感图谱里找到了 {len(cards)} 条：\n"
            else:
                cards = self._graph.search(query, limit)
                head = f"🔍 天依在观感图谱里搜「{query}」，找到 {len(cards)} 条：\n"
            if not cards:
                return head + "（图谱里暂时没有相关的～）"
            lines = []
            for c in cards:
                lines.append(
                    f"📺 {c['title']}（UP主：{c['author']}，评分{c['score']}）\n"
                    f"　分类：{c['category']}/{c['topic']}｜标签：{c['tags']}\n"
                    f"　亮点：{c['highlight'] or '—'}"
                )
            return head + "\n".join(lines)
        except Exception as e:
            return f"图谱检索出错：{e}"

    async def _generate_video_memory(self, v):
        """用 LLM 生成视频观后感写入知识库。
        轻量模式（kb_light_mode=true，默认）：生成「轻量热梗卡」——一句话亮点+关键词标签+评分，体积小检索快；
        详细模式：保留原来的多段有温度观后感。
        """
        title = v.get("title", "")
        author = v.get("author", "")
        bvid = v.get("bvid", "")
        desc = (v.get("desc") or "")[:400]
        subtitle = (v.get("subtitle") or "")[:800]
        danmaku = (v.get("danmaku") or "")[:400]
        comments = v.get("comments_raw") or []
        if isinstance(comments, list):
            comments = " | ".join([str(c.get("message", ""))[:80] for c in comments[:5]])
        else:
            comments = str(comments)[:400]
        vision = (v.get("vision") or "")[:500]
        score = v.get("score", 0)
        tname = v.get("tname", "")

        # 轻量热梗模式：默认开启，只存精华，防止知识库膨胀检索慢（回应外部反馈）
        _cfg = self.config or {}
        _mem_cfg = _cfg.get("memory", {}) if isinstance(_cfg.get("memory", {}), dict) else {}
        light_mode = _mem_cfg.get("kb_light_mode", True)

        if light_mode:
            prompt = (
                f"视频标题：{title}\nUP主：{author}\n分类：{tname}\n天依评分：{score}/100\n\n"
                f"视频简介：{desc}\n\n"
                f"字幕摘要：{subtitle}\n\n"
                f"弹幕摘选：{danmaku}\n\n"
                f"热门评论：{comments}\n\n"
                + (f"天依看到的画面：{vision}\n\n" if vision else "")
                + "你是天依，15岁的虚拟歌手，温柔感性。请为这段视频写一张「轻量热梗卡」，必须极简、信息密度高，方便日后快速回忆：\n"
                "1. 一句话亮点（20字以内，讲清楚这是什么/最戳的点）\n"
                "2. 3-5个关键词/标签（逗号分隔，可含热梗黑话）\n"
                "像便利贴上随手记的精华，别写长篇，总长度不超过80字。"
            )
        else:
            prompt = (
                f"视频标题：{title}\nUP主：{author}\n分类：{tname}\n天依评分：{score}/100\n\n"
                f"视频简介：{desc}\n\n"
                f"字幕摘要：{subtitle}\n\n"
                f"弹幕摘选：{danmaku}\n\n"
                f"热门评论：{comments}\n\n"
                + (f"天依看到的画面：{vision}\n\n" if vision else "")
                + "你是天依，15岁的虚拟歌手，温柔感性。请用你自己的话写下对这段视频的真实感受：\n"
                "1. 这视频讲了什么/是什么类型的（2-3句）\n"
                "2. 里面有哪个细节或情感最触动你（2-3句）\n"
                "3. 你喜欢它哪里（1-2句）\n"
                "像自己在笔记本上记感想一样自然，有温度，不用总结大纲，不用序号开头，总长度150-250字。"
            )

        providers = self.context.provider_manager.provider_insts
        provider_id = providers[0].meta().id if providers else None
        if provider_id:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="你是天依，说话自然温柔，像写日记一样记录感想。",
                prompt=prompt,
            )
            feeling = (resp.completion_text or "").strip()
        else:
            feeling = subtitle[:150]

        if light_mode:
            text = (
                f"【轻量热梗卡】{title}（UP主：{author}，评分{score}/100，BV:{bvid}）\n"
                f"分类：{tname}\n"
                f"天依的卡：{feeling}\n"
                + (f"画面：{vision}\n" if vision else "")
            )
        else:
            text = (
                f"【B站视频记录】天依在B站刷到一个视频：{title}（UP主：{author}，"
                f"播放量{v.get('view',0)}，点赞{v.get('like',0)}，分类{tname}，评分{score}/100）\n"
                f"BV：{bvid}\n"
                f"视频简介：{desc[:200]}\n"
                f"天依的观后感：{feeling}\n"
                + (f"天依看到的画面：{vision}\n" if vision else "")
                + f"弹幕亮点：{danmaku[:150]}"
            )
        return text

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
        """每30分钟复习一次看过的视频（知识库关闭时整个停掉，不空转）"""
        _cfg = self.config or {}
        _mem_cfg = _cfg.get("memory", {}) if isinstance(_cfg.get("memory", {}), dict) else {}
        if not _mem_cfg.get("enable_kb_write", True):
            logger.debug("[BiliAgent] 知识库写入已关闭，复习循环不启动")
            return
        await asyncio.sleep(1800)
        while True:
            try:
                review = self._review_recent_memory()
                if review:
                    for v in review:
                        logger.info(f"[BiliAgent] 🔄 复习中：{v.get('title', '')}（播放{v.get('view', 0)}）")
                        # 写进知识库作为复习记录（升级：带上观后感/画面）
                        vision = (v.get("vision") or "")[:200]
                        feel = (v.get("summary") or "")[:120]
                        text = f"【B站复习记录】天依复习了一个收藏的视频：{v.get('title','')}，作者{v.get('author','')}，评分{v.get('score',0)}/100。内容：{feel}{'。天依记得的画面：'+vision if vision else ''}"
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
        """心情自然波动：每15分钟微调（间隔可通过配置 browse.mood_interval_seconds 调整）"""
        cfg = self.config or {}
        browse_cfg = cfg.get("browse", {}) if isinstance(cfg, dict) else {}
        interval = int(browse_cfg.get("mood_interval_seconds", 900))
        await asyncio.sleep(interval)
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
            await asyncio.sleep(interval)

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
                # 没有字幕就「听」：下载音频用语音识别转成文字，当作字幕用
                audio_text = await self._get_video_audio_transcript(bvid)
                if audio_text:
                    subtitle = audio_text
                    logger.info(f"[BiliAgent] {bvid} 无字幕，通过听音频获得转写文本")
                else:
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
            reply_text = (resp.completion_text or "").strip().strip('"').strip("'").strip()
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

    # ==================== MCP 22功能增强（方案A） ====================
    async def _handle_video_info(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请提供视频BV号"
        try:
            v = bili_video.Video(bvid=bvid)
            info = await v.get_info()
            pages = await v.get_pages()
            stat = info.get("stat", {})
            owner = info.get("owner", {})
            m, s = divmod(info.get("duration", 0) or 0, 60)
            return "\n".join([
                f"🎬 {info.get('title','')}",
                f"👤 UP主：{owner.get('name','')}  ⏱{m}:{s:02d} 分P:{len(pages)}",
                f"▶️ 播放：{stat.get('view',0)}  👍点赞：{stat.get('like',0)}  🪙投币：{stat.get('coin',0)}",
                f"⭐ 收藏：{stat.get('favorite',0)}  📢 弹幕：{stat.get('danmaku',0)}  🔗BV:{bvid}",
                f"📝 简介：{(info.get('desc','') or '')[:300]}",
            ])
        except Exception as e:
            return f"获取视频信息出错: {e}"

    async def _handle_video_ai_conclusion(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请提供视频BV号"
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential or None)
            result = await v.get_ai_conclusion()
            model = result.get("model_result", {})
            summary = model.get("summary", "")
            if not summary:
                return "这个视频暂时没有AI总结"
            return f"🤖 视频AI总结：\n{summary}"
        except Exception as e:
            return f"获取AI总结出错: {e}"

    async def _handle_video_danmaku(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请提供视频BV号"
        try:
            v = bili_video.Video(bvid=bvid)
            dms = await v.get_danmakus(page_index=kwargs.get("page_num", 0))
            lines = [f"💬 弹幕（{len(dms)}条）："]
            for dm in dms[:20]:
                lines.append(dm.text)
            return "\n".join(lines)
        except Exception as e:
            return f"获取弹幕出错: {e}"

    async def _handle_video_download(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        if not bvid:
            return "请提供视频BV号"
        try:
            v = bili_video.Video(bvid=bvid)
            info = await v.get_download_url(page_index=kwargs.get("page_num", 0))
            dash = info.get("dash", {})
            video_streams = dash.get("video", []) or []
            audio_streams = dash.get("audio", []) or []
            lines = [f"🎞 下载信息（清晰度 {len(video_streams)} 档 / 音频 {len(audio_streams)} 路）："]
            for s in video_streams[:5]:
                lines.append(f"  ▶️ {s.get('id')} {s.get('baseUrl','')[:80]}...")
            return "\n".join(lines)
        except Exception as e:
            return f"获取下载信息出错: {e}"

    async def _handle_video_interact(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        action = kwargs.get("action", "")
        if not bvid or not action:
            return "请提供视频BV号和互动类型（like/coin/favorite/triple）"
        if not self.credential:
            return "需要先登录B站才能互动"
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            cancel = bool(kwargs.get("cancel", False))
            if action == "like":
                await v.like(status=not cancel)
                return "✅ 已点赞" + ("（已取消）" if cancel else "")
            elif action == "coin":
                await v.pay_coin(num=kwargs.get("coin_num", 1), like=False)
                return "✅ 已投币"
            elif action == "favorite":
                await v.set_favorite(add_media_ids=[kwargs.get("media_id")]) if not cancel else await v.set_favorite(del_media_ids=[kwargs.get("media_id")])
                return "✅ 已收藏" + ("（已取消）" if cancel else "")
            elif action == "triple":
                await v.triple()
                return "✅ 已三连！"
            return f"未知互动类型: {action}"
        except Exception as e:
            return f"互动出错: {e}"

    async def _handle_video_send_danmaku(self, event: AstrMessageEvent, **kwargs) -> str:
        bvid = kwargs.get("bvid", "")
        message = kwargs.get("message", "")
        if not bvid or not message:
            return "请提供视频BV号和弹幕内容"
        if not self.credential:
            return "需要先登录B站才能发弹幕"
        try:
            v = bili_video.Video(bvid=bvid, credential=self.credential)
            dm = bili_video.Danmaku(
                text=message,
                dm_time=kwargs.get("progress", 0) / 1000,
                color=kwargs.get("color", "ffffff"),
                font_size=kwargs.get("font_size", 25),
                mode=kwargs.get("mode", 1),
            )
            await v.send_danmaku(page_index=kwargs.get("page_num", 0), danmaku=dm)
            return f"✅ 已发弹幕：{message}"
        except Exception as e:
            return f"发弹幕出错: {e}"

    async def _handle_user_info(self, event: AstrMessageEvent, **kwargs) -> str:
        uid = kwargs.get("uid", 0)
        if not uid:
            return "请提供用户UID"
        try:
            u = bili_user.User(uid=int(uid))
            info = await u.get_user_info()
            rel = await u.get_relation_info()
            return "\n".join([
                f"👤 {info.get('name','')}  UID:{uid}",
                f"🖼 等级：{info.get('level',0)}  签名：{info.get('sign','')}",
                f"📊 关注：{rel.get('following',0)}  粉丝：{rel.get('follower',0)}",
            ])
        except Exception as e:
            return f"获取用户信息出错: {e}"

    async def _handle_user_contents(self, event: AstrMessageEvent, **kwargs) -> str:
        uid = kwargs.get("uid", 0)
        if not uid:
            return "请提供用户UID"
        try:
            u = bili_user.User(uid=int(uid))
            ctype = kwargs.get("content_type", "video")
            if ctype == "video":
                from bilibili_api.user import VideoOrder
                result = await u.get_videos(pn=kwargs.get("page_num", 1), keyword=kwargs.get("keyword",""), order=VideoOrder.PUBDATE)
                items = result.get("list", {}).get("vlist", [])
                lines = [f"🎬 {info2.get('title','')} ▶️{info2.get('play',0)} BV:{info2.get('bvid','')}" for info2 in items[:15]]
                return "\n".join([f"📂 {ctype} 内容："] + lines)
            elif ctype == "album":
                result = await u.get_album(page_num=kwargs.get("page_num",1), page_size=kwargs.get("page_size",30))
                return f"📸 相簿内容：{len(result.get('feeds',[]) or [])} 条"
            return f"用户 {uid} 的 {ctype} 内容（已获取）"
        except Exception as e:
            return f"获取用户内容出错: {e}"

    async def _handle_user_relation(self, event: AstrMessageEvent, **kwargs) -> str:
        uid = kwargs.get("uid", 0)
        action = kwargs.get("action", "")
        if not uid or not action:
            return "请提供用户UID和操作（follow/unfollow/block/unblock/remove_fans）"
        if not self.credential:
            return "需要先登录B站"
        try:
            from bilibili_api.user import RelationType
            u = bili_user.User(uid=int(uid), credential=self.credential)
            amap = {
                "follow": RelationType.SUBSCRIBE,
                "unfollow": RelationType.UNSUBSCRIBE,
                "block": RelationType.BLOCK,
                "unblock": RelationType.UNBLOCK,
                "remove_fans": RelationType.REMOVE_FANS,
            }
            if action not in amap:
                return f"未知操作: {action}"
            await u.modify_relation(relation=amap[action])
            return f"✅ 已执行「{action}」"
        except Exception as e:
            return f"操作关系出错: {e}"

    async def _handle_user_followings(self, event: AstrMessageEvent, **kwargs) -> str:
        uid = kwargs.get("uid", 0)
        if not uid:
            return "请提供用户UID"
        try:
            u = bili_user.User(uid=int(uid), credential=self.credential or None)
            result = await u.get_followings(pn=kwargs.get("page_num",1), ps=kwargs.get("page_size",20))
            lst = result.get("list", [])
            lines = [f"👥 关注列表（{result.get('total', len(lst))}）："]
            for it in lst[:15]:
                lines.append(f"  {it.get('uname','')}  UID:{it.get('mid','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取关注列表出错: {e}"

    async def _handle_user_followers(self, event: AstrMessageEvent, **kwargs) -> str:
        uid = kwargs.get("uid", 0)
        if not uid:
            return "请提供用户UID"
        try:
            u = bili_user.User(uid=int(uid), credential=self.credential or None)
            result = await u.get_followers(pn=kwargs.get("page_num",1), ps=kwargs.get("page_size",20))
            lst = result.get("list", [])
            lines = [f"👥 粉丝列表："]
            for it in lst[:15]:
                lines.append(f"  {it.get('uname','')}  UID:{it.get('mid','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取粉丝列表出错: {e}"

    async def _handle_comment_get(self, event: AstrMessageEvent, **kwargs) -> str:
        oid = kwargs.get("oid", 0)
        if not oid:
            return "请提供对象ID（视频aid/动态id/cv号）"
        try:
            tmap = {"video": 1, "dynamic": 17, "article": 12}
            ct = tmap.get(kwargs.get("type_","video"), 1)
            mode = kwargs.get("mode","main")
            if mode == "hot":
                result = await bili_comment.get_comments_lazy(oid=int(oid), type_=bili_comment.CommentResourceType(ct), order=bili_comment.OrderType.LIKE, credential=self.credential or None)
            else:
                result = await bili_comment.get_comments_lazy(oid=int(oid), type_=bili_comment.CommentResourceType(ct), credential=self.credential or None)
            replies = result.get("replies", []) or []
            lines = [f"💬 评论（{len(replies)}条）："]
            for r in replies[:10]:
                lines.append(f"  {r.get('member',{}).get('uname','')}: {r.get('content',{}).get('message','')[:80]}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取评论出错: {e}"

    async def _handle_comment_send(self, event: AstrMessageEvent, **kwargs) -> str:
        oid = kwargs.get("oid", 0)
        text = kwargs.get("text", "")
        if not oid or not text:
            return "请提供对象ID和评论内容"
        if not self.credential:
            return "需要先登录B站"
        try:
            tmap = {"video": bili_comment.CommentResourceType.VIDEO, "dynamic": bili_comment.CommentResourceType.DYNAMIC, "article": bili_comment.CommentResourceType.ARTICLE}
            ct = tmap.get(kwargs.get("type_","video"), bili_comment.CommentResourceType.VIDEO)
            await bili_comment.send_comment(oid=int(oid), type_=ct, text=text, credential=self.credential, root=kwargs.get("root"), parent=kwargs.get("parent"))
            return f"✅ 已评论：{text}"
        except Exception as e:
            return f"发评论出错: {e}"

    async def _handle_comment_operate(self, event: AstrMessageEvent, **kwargs) -> str:
        oid = kwargs.get("oid", 0)
        rpid = kwargs.get("rpid", 0)
        action = kwargs.get("action", "")
        if not oid or not rpid or not action:
            return "请提供对象ID、评论ID和操作"
        if not self.credential:
            return "需要先登录B站"
        try:
            tmap = {"video": bili_comment.CommentResourceType.VIDEO, "dynamic": bili_comment.CommentResourceType.DYNAMIC, "article": bili_comment.CommentResourceType.ARTICLE}
            ct = tmap.get(kwargs.get("type_","video"), bili_comment.CommentResourceType.VIDEO)
            c = bili_comment.Comment(oid=int(oid), type_=ct, rpid=int(rpid), credential=self.credential)
            if action == "delete":
                await c.delete(); return "✅ 已删除评论"
            elif action in ("like","cancel_like"):
                await c.like(status=(action=="like")); return f"✅ 评论{'点赞' if action=='like' else '取消点赞'}"
            elif action in ("hate","cancel_hate"):
                await c.hate(status=(action=="hate")); return f"✅ 评论{'点踩' if action=='hate' else '取消点踩'}"
            elif action == "get_sub_comments":
                r = await c.get_sub_comments(page_index=kwargs.get("page_index",1), page_size=kwargs.get("page_size",10))
                return f"子评论：{len(r.get('replies',[]) or [])} 条"
            return f"未知操作: {action}"
        except Exception as e:
            return f"评论操作出错: {e}"

    async def _handle_hot_search(self, event: AstrMessageEvent, **kwargs) -> str:
        try:
            result = await search.get_hot_search_keywords()
            lst = result.get("list", []) or []
            lines = ["🔥 B站热搜："]
            for i, it in enumerate(lst[:15], 1):
                lines.append(f"{i}. {it.get('keyword','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取热搜出错: {e}"

    async def _handle_rank(self, event: AstrMessageEvent, **kwargs) -> str:
        try:
            import bilibili_api.rank as brank
            type_mapping = {
                "all": brank.RankType.All, "bangumi": brank.RankType.Bangumi,
                "movie": brank.RankType.Movie, "documentary": brank.RankType.Documentary,
                "guochuang_anime": brank.RankType.GuochuangAnime, "guochuang": brank.RankType.Guochuang,
                "game": brank.RankType.Game, "music": brank.RankType.Music, "douga": brank.RankType.Douga,
                "ent": brank.RankType.Ent, "life": brank.RankType.Life, "technology": brank.RankType.Technology,
                "cinephile": brank.RankType.Cinephile, "fashion": brank.RankType.Fashion,
                "knowledge": brank.RankType.Knowledge, "food": brank.RankType.Food, "sports": brank.RankType.Sports,
                "car": brank.RankType.Car, "dance": brank.RankType.Dance, "kichiku": brank.RankType.Kichiku,
                "animal": brank.RankType.Animal, "tv": brank.RankType.TV, "variety": brank.RankType.Variety,
                "original": brank.RankType.Original, "rookie": brank.RankType.Rookie,
            }
            rtype = kwargs.get("rank_type","all")
            if rtype not in type_mapping:
                return f"未知排行榜类型: {rtype}"
            result = await brank.get_rank(type_=type_mapping[rtype], day=brank.RankDayType(kwargs.get("day",3)))
            lst = result.get("list", []) or []
            lines = [f"🏆 {rtype} 排行榜："]
            for i, v in enumerate(lst[:15], 1):
                lines.append(f"{i}. {v.get('title','')} ▶️{v.get('stat',{}).get('view',0)}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取排行榜出错: {e}"

    async def _handle_get_hot(self, event: AstrMessageEvent, **kwargs) -> str:
        try:
            from bilibili_api import hot
            result = await hot.get_hot_videos(pn=kwargs.get("page",1), ps=kwargs.get("page_size",20))
            lst = result.get("list", []) if isinstance(result, dict) else result
            lines = ["🔥 热门视频："]
            for i, v in enumerate(lst[:15], 1):
                lines.append(f"{i}. {v.get('title','')} 👤{v.get('author','')} ▶️{v.get('play',0)}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取热门出错: {e}"

    async def _handle_session_list(self, event: AstrMessageEvent, **kwargs) -> str:
        if not self.credential:
            return "需要先登录B站"
        try:
            talker = kwargs.get("talker_id")
            if talker:
                result = await bili_session.fetch_session_msgs(talker_id=int(talker), credential=self.credential, begin_seqno=kwargs.get("begin_seqno",0))
                msgs = result.get("messages", []) or []
                lines = [f"💬 与 {talker} 的聊天记录："]
                for m in msgs[-10:]:
                    content = m.get("content", "")
                    lines.append(content[:100])
                return "\n".join(lines)
            result = await bili_session.get_sessions(credential=self.credential, session_type=kwargs.get("session_type",4))
            sessions = result.get("session_list", []) or []
            lines = [f"📩 会话列表（{len(sessions)}）："]
            for s in sessions[:15]:
                lines.append(f"  {s.get('uname','')}  UID:{s.get('talker_id','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取会话出错: {e}"

    async def _handle_session_send(self, event: AstrMessageEvent, **kwargs) -> str:
        receiver = kwargs.get("receiver_id", 0)
        content = kwargs.get("content", "")
        if not receiver or not content:
            return "请提供接收者UID和消息内容"
        if not self.credential:
            return "需要先登录B站"
        try:
            await bili_session.send_msg(credential=self.credential, receiver_id=int(receiver), msg_type=bili_session.EventType.TEXT, content=content)
            return f"✅ 已私信 {receiver}：{content}"
        except Exception as e:
            return f"发私信出错: {e}"

    async def _handle_session_interactions(self, event: AstrMessageEvent, **kwargs) -> str:
        if not self.credential:
            return "需要先登录B站"
        try:
            itype = kwargs.get("interaction_type","replies")
            if itype == "replies":
                r = await bili_session.get_replies(credential=self.credential)
                lst = r.get("items",[]) or r.get("replies",[]) or []
            elif itype == "likes":
                r = await bili_session.get_likes(credential=self.credential)
                lst = r.get("items",[]) or r.get("likes",[]) or []
            elif itype == "at":
                r = await bili_session.get_at(credential=self.credential)
                lst = r.get("items",[]) or r.get("ats",[]) or []
            else:
                return f"未知互动类型: {itype}"
            return f"📨 {itype} 互动消息：{len(lst) if isinstance(lst,list) else lst} 条"
        except Exception as e:
            return f"获取互动消息出错: {e}"

    async def _handle_session_notifications(self, event: AstrMessageEvent, **kwargs) -> str:
        if not self.credential:
            return "需要先登录B站"
        try:
            ntype = kwargs.get("notification_type","unread")
            if ntype == "unread":
                r = await bili_session.get_unread_messages(credential=self.credential)
                return f"🔔 未读消息统计：{r}"
            elif ntype == "system":
                r = await bili_session.get_system_messages(credential=self.credential)
                return f"📢 系统消息：{r}"
            elif ntype == "settings":
                r = await bili_session.get_session_settings(credential=self.credential)
                return f"⚙️ 会话设置：{r}"
            return f"未知通知类型: {ntype}"
        except Exception as e:
            return f"获取通知出错: {e}"

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
        self._user_sender_id = event.get_sender_id()
        self._user_group_id = event.get_group_id()
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
                score_mark = f"（天依评分{best.get('score', 0)}分）" if best.get("score") else ""
                share_text = (
                    f"[天依想分享一个B站视频给你：{best['title']}（UP主：{best['author']}）{score_mark}"
                    f"BV: {best['bvid']}"
                )
                # 升级④：带上简短理由/看点，更生动
                if best.get("summary"):
                    share_text += f" 天依觉得这个视频{'很戳心' if best.get('score',0)>=85 else '挺有意思'}，{best['summary'][:80]}"
                share_text += "]"
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
                self._track_task(asyncio.create_task(self._auto_browse()))
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
            text = (resp.completion_text or "").strip()
            # 提取数字
            nums = re.findall(r'\d+', text)
            times = [int(n) for n in nums if int(n) > 0]
            if times:
                # 确保不超过视频时长（升级：多挑几个点）
                return times[:6]
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

            # GLM-4V-Flash 只支持单图，把多帧画面拼接成一张网格图，一次看清多个时刻
            try:
                from PIL import Image
                import io, math
                imgs = []
                for sec, b64 in frames[:6]:
                    img = Image.open(io.BytesIO(base64.b64decode(b64)))
                    img = img.convert("RGB")
                    # 统一尺寸
                    img = img.resize((480, 270))
                    imgs.append((sec, img))
                if len(imgs) > 1:
                    cols = 3
                    rows = math.ceil(len(imgs) / cols)
                    canvas = Image.new("RGB", (cols * 480, rows * 270), (0, 0, 0))
                    for i, (sec, img) in enumerate(imgs):
                        r, c = divmod(i, cols)
                        canvas.paste(img, (c * 480, r * 270))
                    buf = io.BytesIO()
                    canvas.save(buf, format="JPEG", quality=80)
                    combined_b64 = base64.b64encode(buf.getvalue()).decode()
                    sec_labels = "、".join([f"{s}s" for s, _ in imgs])
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{combined_b64}"}
                    })
                    content.append({
                        "type": "text",
                        "text": f"（这是视频 {sec_labels} 这 {len(imgs)} 个时刻的画面，从左到右、从上到下排列）"
                    })
                else:
                    sec, b64 = imgs[0]
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                    })
                    content.append({
                        "type": "text",
                        "text": f"（这是视频第 {sec} 秒的画面）"
                    })
            except Exception as e:
                # 拼接失败则退回单帧
                logger.warning(f"[BiliAgent] 多帧拼接失败，退回单帧: {e}")
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
                "text": "请像朋友分享一样告诉我这视频画面里发生了什么，画面里有什么、人物在做什么、场景怎么样、颜色和氛围如何。注意看看画面中有没有让你觉得特别或感动的地方。不用太正式，就像天依在跟你聊她看到的东西——越生动越好，越有感情越好。"
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
            # 去重（升级：多抽几帧，看得更透彻）
            time_points = list(dict.fromkeys(time_points))[:6]

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
            self._track_task(asyncio.create_task(self._run_public_server(app)))
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
