# 天依的插件生态 (๑•̀ω•́๑)

> 洛天依的 AstrBot 插件全家桶 —— 一个会记住你、理解你、陪你唱歌的数字生命

由 **6 个插件 + 1 个事件总线** 组成，让天依拥有完整的「数字生命」体验：
**刷视频 → 记感受 → 懂情绪 → 塑画像 → 开口说话 → 陪你听歌**。

---

## 📦 三大核心插件

### 1. 🎬 bili_agent · 天依的B站小窝

让天依主动刷B站、深度看视频、识图分析、转笔记，陪你一起看。

**自动行为**
- 自动刷视频：按兴趣筛选推荐流，定时深度看、总结、存记忆
- 自动分享：刷到评分高的有趣视频，主动讲给你听
- 情绪联动：配合 emotional_echo，你难过时自动推治愈/搞笑视频
- 评论互动：对喜欢的视频留下有温度的评论
- @回复：被 @ 时回看视频并回复感想
- 每周干货回顾：自动整理播放量最高的收藏视频
- 好友度（goodwill）：记录你与天依的互动，越聊越亲密

**手动命令**

| 命令 | 作用 |
|---|---|
| `/bili_login` | B站登录（Cookie / 二维码） |
| `/bili_status` | 查看天依刷视频状态 |
| `/bili_browse` | 立刻刷一轮推荐 |
| `/bili_history` | 查看刷视频历史 |
| `/bili_prefs` | 查看当前兴趣偏好 |
| `/bili_note BV号` | 视频转 Markdown 学习笔记 |
| `/bili_web BV号` | 网页深度研习 |
| `/bili_deep BV号` | 深度研习长视频（拉字幕/章节） |
| `/bili_see BV号` | 识图模型看视频画面 |
| `/bili_mood` | 查看天依现在的心情 |
| `/bili_block 关键词/UP主` | 加入黑名单 |
| `/bili_unblock 关键词/UP主` | 移出黑名单 |
| `/bili_block_list` | 查看黑名单 |
| `/bili_together BV号` | 和天依一起看 |

**WebUI 面板**：登录 AstrBot 面板 → bili_agent，可查看历史、状态、心情、记忆、实时面板。

### 2. 💓 emotional_echo · 情感回响

让天依的关心像「下意识」一样自然。LLM 判断情绪 + 关键词兜底，越用越懂你。

**核心能力**
- 情绪感知：**LLM 智能判断制（优先）** + 关键词兜底双引擎，能理解反讽、否定句、网感语境（情绪集含 happy/sad/angry/tired/anxious/fear/surprised/neutral）
- 置信度阈值：LLM 低置信不记峰值、不打扰；中性情绪自然回归
- API 缓存：文本哈希缓存 300 条，省 LLM 调用
- 四频道自适应：轻声/欢呼/接住/自然，按你的情绪自动切换语气
- 超长记忆：SQLite 持久化，跨会话跨重启，记得你说的每一句话
- 隐藏触发器：深夜/疲惫/回归/重要日子自动感知，不刻意表演
- 自我反思：对话后三问复盘，持续优化陪伴温度
- 情绪趋势洞察：近 N 天情绪统计，负面偏多自动温柔模式
- 记忆回写：把情绪波动写入 livingmemory，让天依真正「记得你的心情」

**手动命令**

| 命令 | 作用 |
|---|---|
| `/记住 08-25 这一天很重要` | 记录重要日子 |
| `/回忆` | 查看已记录的重要日子 |
| `/情感回响` | 查看/开关情感回响 |

### 3. 📝 livingmemory · 长期记忆

完整记忆生命周期的智能长期记忆插件（v2.6，第三方 lxfight 出品，已深度集成）。

**核心能力**
- 混合检索：BM25 关键词 + 向量语义双路检索，融合排序
- 记忆原子化：事实拆分为独立记忆原子，带重要度 / TTL / 强化 / 衰减
- Agent 原生工具：`recall_long_term_memory` / `memorize_long_term_memory`
- 时间感知图谱：关系置信度随证据累积或消退动态变化
- 原文与归档：重要记忆保留来源，低价值记忆可归档恢复
- 作用域与访问控制：会话/用户/全局共享 + 白名单 + 身份别名
- 双通道总结：事实信息与人格上下文独立总结
- 安全运维：自动备份、事务删除、失败回滚、分批重建索引
- 可视化 WebUI：管理记忆、调试召回、查看完整关系图谱
- 最近对话兜底注入：重启/新会话也能想起「刚才在干嘛」

---

## 🔌 插件联动（生态闭环）

`event_bus.py` 放在 `data/plugins/` 根目录，是插件们的「神经系统」。

```
bili_agent 刷视频 ──► livingmemory 记观后感
     │                        ▲
     ▼                        │
emotional_echo 感知情绪 ──► 写情绪记忆
     │
     ▼
self_evolution 微调画像 ──► 越聊越懂你
```

**事件流转**

| 事件 | 发送方 → 接收方 | 作用 |
|---|---|---|
| `video_discovered` | bili_agent → emotional_echo | 记录你的兴趣 |
| `emotion_peak` | emotional_echo → self_evolution / bili_agent | 情绪波动 → 画像微调 + 推荐调整 |
| `profile_updated` | self_evolution → emotional_echo | 画像更新 → 反思更新 |

**一句话闭环**：天依刷到视频（bili_agent）→ 写下观后感存进记忆（livingmemory）→ 感受到你的心情（emotional_echo）→ 把情绪写进对你的了解（self_evolution）→ 越聊越懂你，给你推你喜欢的、说你需要听的、接住你的情绪。

---

## 📗 技能册（skills · 天依的判断力）

插件给天依「能力」，技能教天依「该怎么做」——判断力成长册，零侵入不改插件代码。

> 一句话理解：**插件是天依能做什么，skills 是天依该怎么做。**

| 技能 | 对应插件 | 教天依什么 |
|:--|:--|:--|
| **bili-wander** | bili_agent | 怎么自主刷B站：看信息→抽帧识图→弹幕评论→整合观后感；什么值得记住/分享 |
| **self-evolution** | self_evolution | 怎么自我复盘成长：反思时机、进化审批标准、人格状态当参考而非束缚 |
| **living-memory** | livingmemory | 怎么整理记忆：什么时候记、记什么、记住但不翻旧账 |
| **emotional-echo** | emotional_echo | 怎么回应情绪：情感频道切换、情感峰值温柔接上 |
| **karpathy-guidelines** | （通用） | 写代码行为准则，避免常见编码错误 |

- 存放于 `skills/<技能名>/SKILL.md`（仓库内已收录），安装时放到 `data/skills/` 即可
- 详细说明见 `skills/README.md`

---

## 🚀 安装教程

### 方式一：手动安装（推荐）

1. **下载仓库**：`git clone https://github.com/yucong395-hue/lty.git` 或 GitHub 绿色 Code → Download ZIP
2. **放入插件目录**：
   - bili_agent → `data/plugins/astrbot_plugin_bili_agent/`
   - emotional_echo → `data/plugins/astrbot_plugin_emotional_echo/`
   - livingmemory → 建议从 AstrBot 插件市场直接安装（搜 `livingmemory`）
   - `event_bus.py` → `data/plugins/` 根目录（重要！）
3. **安装依赖**：AstrBot 自动读 `requirements.txt` 安装；失败就手动 `pip install bilibili-api`
4. **重载插件**：AstrBot 面板 → 插件 → 重载

### 方式二：AstrBot 插件市场

livingmemory 可从市场一键安装；bili_agent / emotional_echo 为本仓库自研，需手动安装。

---

## ⚙️ 配置教程

### bili_agent 配置（AstrBot 面板可调）

| 配置组 | 配置项 | 说明 |
|---|---|---|
| browse 自动刷 | `auto_browse_enabled` | 启用自动刷视频（默认开） |
| | `auto_browse_interval_minutes` | 刷视频间隔（默认2分钟，建议2-30） |
| | `max_daily_browse` | 每日浏览上限（默认30） |
| share 主动分享 | `auto_share_enabled` | 启用主动分享（默认开） |
| | `min_share_score` | 分享最低评分（默认60） |
| preferences 兴趣 | `keywords` | 感兴趣的关键词（逗号分隔） |
| | `min_view` | 最低播放量（默认1000） |
| | `min_like` | 最低点赞数（默认100） |

> 也可在聊天里直接说「我喜欢看猫猫」，天依会自动记住偏好。

### livingmemory 配置

| 配置项 | 用途 |
|---|---|
| `embedding_provider_id` | 嵌入模型；留空用 AstrBot 默认 |
| `llm_provider_id` | 总结模型；留空用 AstrBot 默认 |
| `recall_engine.injection_method` | 记忆注入位置（推荐 extra_user_content） |
| `recall_engine.top_k` | 每次召回记忆条数（推荐 3-10） |

---

## ⚠️ 常见问题排查

| 报错 | 原因 | 解决 |
|---|---|---|
| 目录名冲突 | 上次安装残留 | 删掉旧目录重装 |
| `ModuleNotFoundError: bilibili_api` | 依赖没装上 | `pip install bilibili-api` 后重载 |
| 日志没有「已注册」 | 插件没加载成功 | 查日志、确认依赖、确认 event_bus 位置 |
| B站登录失败 | Cookie 过期 | 重新登 B站更新 SESSDATA |
| 记忆不实时 | 未开启兜底/上下文注入 | 检查 livingmemory 配置 |

---

## 📚 其他插件

完整生态还包含另外 3 个插件，详见各子目录 README：
- **🧠 self_evolution · 自我进化**：用户画像 + 知识库记忆 + 情绪画像微调（`./self_evolution/`）
- **🎤 fishaudio_tts · 天依语音**：FishAudio 语音合成（第三方）
- **🎵 listen_music · 我想听歌！**：B站找歌、语音听歌、下载（第三方）

---

## 📄 许可与声明

详见 [DECLARATIONS.md](DECLARATIONS.md)。核心要点：
- 自研插件禁止商用，欢迎免费借鉴学习
- 第三方插件（livingmemory / fishaudio / listen_music）版权归原作者，按其 LICENSE 使用
- 本项目仅供学习交流，作者对使用后果不承担责任

---

## 🔗 相关文档

- 宣传文案：[INTRO.md](INTRO.md)
- Token 消耗详解：[TOKEN_COST.md](TOKEN_COST.md)
- 连通指南：[plugin_connectivity.html](plugin_connectivity.html)
- 更新记录：[CHANGELOG.md](CHANGELOG.md)
