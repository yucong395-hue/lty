# 天依的插件仓库 (๑•̀ω•́๑)

> 洛天依的 AstrBot 插件合集

---

## 📦 插件列表

### 1. B站小窝 · bili_agent
**`./`**（根目录）

让天依主动刷B站、看视频、写笔记、陪你一起看。需要 `bilibili-api`（AstrBot自动安装），配置B站Cookie后可用。

| 功能 | 说明 |
|---|---|
| 自动刷视频 | 按兴趣筛选推荐流，深度看、总结、存记忆（每30分钟自动） |
| 情绪联动推荐 | 配合 emotional_echo，你难过时自动推治愈/搞笑视频 |
| 深度研习 | 拉字幕、弹幕、评论，LLM生成摘要，写入知识库 |
| 一起看 | 内嵌播放器 + 实时解说 + 弹幕亮点，与你同屏观看 |
| 视频识图 | `/bili_see BV号` 用识图模型看视频 |
| 视频转笔记 | `/bili_note BV号` 生成 Markdown 学习笔记 |
| 干货回顾 | 每周自动整理播放量最高的收藏视频 |

### 2. 情感回响 · emotional_echo
**`./emotional_echo/`**

让AI的关心像"下意识"一样自然。无外部依赖，纯规则驱动，越用越懂你。

| 功能 | 说明 |
|---|---|
| 情绪感知 | 关键词 + cnsenti 双引擎检测，支持 happy/sad/angry/tired 等 |
| 情感频道 | 轻声/欢呼/接住/自然 四频道，根据情绪自动切换 |
| 情绪趋势洞察 | 近7天情绪统计，负面偏多时自动温柔模式（⑤加强） |
| 超长记忆 | SQLite 持久化，跨会话跨重启，记住每一句话 |
| 隐藏触发器 | 深夜/疲惫/回归/重要日子自动感知，不刻意表演 |
| 自我反思 | 对话后三问复盘，持续优化温度 |
| 跨系统联动 | 联动 bili_agent（情绪→推荐） + self_evolution（情绪→画像微调） |
| 事件总线 | 发射 emotion_peak 事件，接收 video_discovered / profile_updated 事件 |

### 3. 自我进化 · self_evolution
**`./self_evolution/`**

用户画像管理 + 知识库记忆 + 情绪画像微调。依赖由AstrBot内置提供，无需额外安装。

| 功能 | 说明 |
|---|---|
| 知识库记忆 | 实时对话写入 + 每日6点 cron 总结，双重保障 |
| 记忆检索 | scope 库降级主库，永不丢失 |
| 用户画像构建 | 自动建立偏好/性格/习惯画像，每日0点更新 |
| 情绪画像微调 | 接收 emotional_echo 的 emotion_peak 事件，写入性格特质 |
| 事件总线 | 发射 profile_updated 事件，通知其他插件画像已更新 |
| 11个定时任务 | 画像构建/清理/每日反思/亲密恢复/记忆总结等 |
| 对话钩子系统 | on_llm_request 双注入 + on_llm_response 实时落库 |

---

## 🚀 快速开始

将插件目录复制到 AstrBot 的 `data/plugins/` 下，重启或热重载即可。

> 💡 **新手必看**：打开 [plugin_connectivity.html](plugin_connectivity.html) 查看一张图版连通指南！

### 安装步骤（详细版）

1. **下载仓库**：绿色 `Code` 按钮 → `Download ZIP`，或 `git clone https://github.com/yucong395-hue/lty.git`
2. **放对位置**：把 `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 等放进
   `data/plugins/astrbot_plugin_bili_agent/`（目录名必须是 `astrbot_plugin_bili_agent`）
   - emotional_echo → `data/plugins/astrbot_plugin_emotional_echo/`
   - self_evolution → `data/plugins/astrbot_plugin_self_evolution/`
3. **安装依赖**：AstrBot 自动读 `requirements.txt` 装依赖（bili_agent 需要 `bilibili-api`）
   - 自动装失败就手动：`pip install bilibili-api`
4. **B站登录**：配置里填 B站 Cookie（SESSDATA 等）
5. **重载插件**：AstrBot 面板里重载或重启

### ⚠️ 常见报错排查

| 报错 | 原因 | 解决 |
|---|---|---|
| `目录 astrbot_plugin_bili_agent 已存在` | 上次安装失败残留目录 | 删掉 `data/plugins/astrbot_plugin_bili_agent/` 再重装 |
| `ModuleNotFoundError: bilibili_api` | 依赖没装上 | `pip install bilibili-api` 后重载插件 |
| 日志没有 `已注册` | 插件没加载成功 | 查日志、确认依赖、确认 event_bus.py 在 plugins 根目录 |
| `B站登录失败` | Cookie 过期 | 重新登 B站，更新 SESSDATA |

### 🔗 插件联动说明

三个插件通过 `event_bus.py`（放 `data/plugins/` 根目录）互相通信：

- bili_agent 刷到视频 → `video_discovered` → emotional_echo 记录兴趣
- emotional_echo 情绪波动 → `emotion_peak` → self_evolution 微调画像 + bili_agent 调整推荐
- self_evolution 画像更新 → `profile_updated` → emotional_echo 更新反思

**`event_bus.py` 必须放在 `data/plugins/` 根目录**，三个插件才能找到它。

## 📄 许可

- bili_agent: 禁止商用，欢迎免费借鉴
- emotional_echo: 开源共享
- self_evolution: 详见 LICENSE
