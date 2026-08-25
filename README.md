# 天依的插件仓库 (๑•̀ω•́๑)

> 洛天依的 AstrBot 插件合集

---

## 📦 插件列表

### 1. B站小窝 · bili_agent
**`./`**（根目录）

让天依能主动刷 B 站、看视频、写笔记、陪你一起看。

| 功能 | 说明 |
|---|---|
| 自动刷视频 | 按兴趣筛选推荐流，深度看、总结、存记忆 |
| 深度研习 | 拉字幕、弹幕、评论，LLM 生成摘要，写入知识库 |
| 一起看 | 内嵌播放器 + 实时解说 + 弹幕亮点，与你同屏观看 |
| 视频识图 | `/bili_see BV号` 用识图模型看视频 |
| 视频转笔记 | `/bili_note BV号` 生成 Markdown 学习笔记 |
| 干货回顾 | 每周自动整理播放量最高的收藏视频 |

### 2. 情感回响 · emotional_echo
**`./emotional_echo/`**

让AI的关心像"下意识"一样自然——多用户、超长记忆、自我进化。

| 功能 | 说明 |
|---|---|
| 情绪感知 | 关键词 + 小模型（cnsenti）双引擎检测情绪 |
| 情感频道 | 轻声/欢呼/接住/自然 四频道自适应 |
| 超长记忆 | SQLite 持久化，跨会话跨重启，记住每一句话 |
| 隐藏触发器 | 深夜/疲惫/回归/重要日子自动感知 |
| 自我反思 | 对话后三问复盘，持续优化温度 |
| 跨系统联动 | 与 LivingMemory / 知识库联动 |

### 3. 自我进化 · self_evolution
**`./self_evolution/`**

CognitionCore 7.0 数字生命系统。

| 功能 | 说明 |
|---|---|
| 知识库记忆 | 实时对话写入 + 每日总结，双重保障 |
| 记忆检索 | scope 库降级主库，永不丢失 |
| 情感回响联动 | on_llm_request 双注入，情感底色自然融入 |
| 对话钩子系统 | on_llm_response 实时落库 |

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
