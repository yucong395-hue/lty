# 天依的插件生态 (๑•̀ω•́๑)

> 洛天依的 AstrBot 插件全家桶 —— 一个会记住你、理解你、陪你唱歌的数字生命

一个插件 + 一个事件总线，让天依拥有完整的「数字生命」体验：
**刷视频 → 记感受 → 懂情绪 → 塑画像 → 开口说话 → 陪你听歌**。

---

## 🌐 生态总览（一图看懂）

```
                 ┌────────────────────────────────────────┐
                 │          🔌 event_bus.py 事件总线        │
                 │   插件之间互相通信的"神经系统"           │
                 └───────┬──────────────┬──────────────┬───┘
                         │              │              │
        ┌────────────────▼───┐   ┌──────▼───────┐   ┌──▼────────────┐
        │ 🎬 bili_agent       │   │ 💓 emotional_│   │ 🧠 self_evolu-│
        │    B站小窝           │   │    echo 情感回响│   │    tion 自我进化│
        └────────────────────┘   └──────┬───────┘   └──┬────────────┘
                         │              │              │
        ┌────────────────▼───┐   ┌──────▼───────┐   ┌──▼────────────┐
        │ 📝 livingmemory     │   │ 🎤 fishaudio_ │   │ 🎵 listen_music│
        │    长期记忆         │   │    tts 天依语音 │   │    听歌        │
        └────────────────────┘   └──────────────┘   └───────────────┘
```

**一句话联动闭环**：bili_agent 刷到视频 → 总结写入 livingmemory / 知识库 → emotional_echo 感知你的情绪联动推荐 → self_evolution 把情绪写进你的画像 → 天依用 fishaudio_tts 开口陪你聊 → 你开心了就 listen_music 点歌。

---

## 📦 插件功能总表

### 1. 🎬 bili_agent · 天依的B站小窝

让天依主动刷B站、看视频、写笔记、陪你一起看。

| 功能 | 说明 |
|---|---|
| 自动刷视频 | 按兴趣筛选推荐流，深度看、总结、存记忆（每30分钟自动） |
| 视频深度总结 | 拉字幕/弹幕/评论，LLM 生成观后感，写入知识库 |
| 情绪联动推荐 | 配合 emotional_echo，你难过时自动推治愈/搞笑视频 |
| 一起看（同屏） | 内嵌播放器 + 分段解说 + 弹幕亮点，与你同屏观看 |
| 视频识图 | `/bili_see BV号` 用识图模型看视频画面 |
| 视频转笔记 | `/bili_note BV号` 生成 Markdown 学习笔记 |
| 评论互动 | 自动对喜欢的视频留下有温度的评论 |
| @回复 | 被 @ 时回看视频并回复感想 |
| 弹幕高能点 | 提取弹幕密集/精彩时段，快速看重点 |
| 干货回顾 | 每周自动整理播放量最高的收藏视频 |

### 2. 💓 emotional_echo · 情感回响

让天依的关心像"下意识"一样自然。纯规则驱动，越用越懂你。

| 功能 | 说明 |
|---|---|
| 情绪感知 | 关键词 + cnsenti 双引擎检测（happy/sad/angry/tired 等） |
| 四频道自适应 | 轻声/欢呼/接住/自然，按你的情绪自动切换语气 |
| 超长记忆 | SQLite 持久化，跨会话跨重启，记得每一句话 |
| 隐藏触发器 | 深夜/疲惫/回归/重要日子自动感知，不刻意表演 |
| 自我反思 | 对话后三问复盘，持续优化陪伴温度 |
| 情绪趋势洞察 | 近 N 天情绪统计，负面偏多自动温柔模式 |
| 跨系统联动 | 联动 bili_agent（情绪→推荐）+ self_evolution（情绪→画像） |
| 事件总线 | 发射 emotion_peak，接收 video_discovered / profile_updated |

### 3. 🧠 self_evolution · 自我进化

用户画像管理 + 知识库记忆 + 情绪画像微调。

| 功能 | 说明 |
|---|---|
| 知识库记忆 | 实时对话写入 + 每日6点 cron 总结，双重保障 |
| 记忆检索 | scope 库降级主库，永不丢失 |
| 用户画像构建 | 自动建立偏好/性格/习惯画像，每日0点更新 |
| 情绪画像微调 | 接收 emotion_peak，把情绪写进性格特质 |
| 对话钩子系统 | on_llm_request 双注入 + on_llm_response 实时落库 |
| 11个定时任务 | 画像构建/清理/每日反思/亲密恢复/记忆总结等 |
| 事件总线 | 发射 profile_updated，通知其他插件画像已更新 |

### 4. 📝 livingmemory · 长期记忆

完整记忆生命周期的智能长期记忆插件（v2.6）。

| 功能 | 说明 |
|---|---|
| 混合检索 | BM25 关键词 + 向量语义双路检索，融合排序 |
| 记忆原子化 | 事实拆分为独立记忆原子，带重要度/TTL/强化/衰减 |
| Agent 原生工具 | `recall_long_term_memory` / `memorize_long_term_memory` |
| 时间感知图谱 | 关系置信度随证据累积或消退动态变化 |
| 原文与归档 | 重要记忆保留来源，低价值记忆可归档恢复 |
| 作用域与访问控制 | 会话/用户/全局共享 + 白名单 + 身份别名 |
| 双通道总结 | 事实信息与人格上下文独立总结 |
| 安全运维 | 自动备份、事务删除、失败回滚、分批重建索引 |
| 可视化 WebUI | 管理记忆、调试召回、查看完整关系图谱 |
| 最近对话兜底注入 | 重启/新会话也能想起"刚才在干嘛" |

### 5. 🎤 fishaudio_tts · 天依语音

基于 FishAudio API 的语音合成插件。

| 功能 | 说明 |
|---|---|
| 多音色 | 支持天依音色等多角色 |
| 情感标签 | happy/sad/angry/whisper/excited 等自动加标签 |
| LLM 自动判断语气 | tts_speak 工具可被 AI 自动调用，带情感识别 |
| 手动指令 | 说"天依说 文本"即可合成语音 |
| 代理与限流 | 支持代理配置和频率限制 |

### 6. 🎵 listen_music · 我想听歌！

B站单源搜索、语音听歌与文件下载。

| 功能 | 说明 |
|---|---|
| 歌曲搜索 | B站单源搜索，精确匹配歌名/歌手 |
| 语音听歌 | 直接在聊天里播放可听音频 |
| 文件下载 | 回复序号下载歌曲文件 |
| 智能候选 | 自动评估候选版本，尊重你的版本偏好 |

---

## 🔌 插件联动说明（event_bus.py）

`event_bus.py` 放在 `data/plugins/` 根目录，是插件们的"神经系统"：

| 事件 | 发送方 → 接收方 | 作用 |
|---|---|---|
| `video_discovered` | bili_agent → emotional_echo | 记录你的兴趣 |
| `emotion_peak` | emotional_echo → self_evolution / bili_agent | 情绪波动 → 画像微调 + 推荐调整 |
| `profile_updated` | self_evolution → emotional_echo | 画像更新 → 反思更新 |

---

## 🚀 快速开始

1. `git clone https://github.com/yucong395-hue/lty.git`
2. 把 `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 放进 `data/plugins/astrbot_plugin_bili_agent/`
   - emotional_echo → `data/plugins/astrbot_plugin_emotional_echo/`
   - self_evolution → `data/plugins/astrbot_plugin_self_evolution/`
   - livingmemory → `data/plugins/astrbot_plugin_livingmemory/`（插件市场直接安装更方便）
3. `event_bus.py` 放 `data/plugins/` 根目录
4. AstrBot 自动装依赖，B站插件填 Cookie，重载插件即用

> 💡 新手指南：[plugin_connectivity.html](plugin_connectivity.html) · Token 详解：[TOKEN_COST.md](TOKEN_COST.md)

### ⚠️ 常见报错排查

| 报错 | 原因 | 解决 |
|---|---|---|
| 目录名冲突 | 上次安装残留 | 删掉旧目录重装 |
| `ModuleNotFoundError: bilibili_api` | 依赖没装上 | `pip install bilibili-api` 重载 |
| 日志没有"已注册" | 插件没加载成功 | 查日志、确认依赖、确认 event_bus 位置 |
| B站登录失败 | Cookie 过期 | 重新登 B站更新 SESSDATA |

---

## 📄 许可

- bili_agent: 禁止商用，欢迎免费借鉴
- emotional_echo: 开源共享
- self_evolution / livingmemory: 详见各自 LICENSE
