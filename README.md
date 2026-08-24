# 🎤 天依的B站小窝 (astrbot_plugin_bili_agent)

> **AstrBot 插件** — 让洛天依能主动刷 B 站、看视频、写笔记、陪你一起看。
>
> 🧑‍💻 作者 15 岁，第一次研究插件开源，请多多包涵！
> 📢 欢迎大家免费借鉴使用，**禁止商用**。

---

## ✨ 功能一览

| 功能 | 说明 |
|---|---|
| **自动刷视频** | 每 2 分钟刷推荐流，按兴趣筛选，深度看、总结、存记忆 |
| **智能兴趣引擎** | 关键词 + 同义词扩展 + 排除词，多维度评分 |
| **深度看视频** | 拉字幕、弹幕、评论，LLM 生成摘要，写入知识库 |
| **评论互动** | 自动回复有趣评论，每条视频只回一次，记录互动历史 |
| **@通知响应** | 每小时检查 B站 @通知，自动回复到评论区 |
| **一起看** | 内嵌播放器 + 实时解说 + 弹幕亮点，与你同屏观看 |
| **视频转笔记** | `/bili_note BV号` 生成 Markdown 学习笔记 |
| **深度研习** | `/bili_deep BV号` 长视频分章节总结 |
| **视频转网页** | `/bili_web BV号` 生成学习卡片 HTML |
| **干货回顾** | 每周自动整理播放量最高的收藏视频 |
| **私信转发** | 每 30 分钟检查 B站 私信并转发到聊天 |
| **心情系统** | 心情自然波动，影响回复风格 |
| **好感度系统** | 追踪互动好感值，每日向 0 回归 |
| **黑名单管理** | 屏蔽 UP 主或关键词，可随时管理 |
| **话题标签** | 分享视频时自动打上 `#音乐` `#猫猫` 等标签 |
| **免登录面板** | 浏览器打开 `http://localhost:6288/` 即可查看状态 |

## 📦 安装

### 前提条件

- 已安装 [AstrBot](https://github.com/Soulter/AstrBot) v3.x
- Python 3.9+

### 安装步骤

```bash
# 进入 AstrBot 插件目录
cd /path/to/AstrBot/data/plugins

# 克隆本插件
git clone https://github.com/yucong395-hue/lty.git astrbot_plugin_bili_agent

# 安装依赖
pip install bilibili-api-python aiohttp
```

### 或者手动安装

1. 下载本仓库的 zip 压缩包
2. 解压到 `AstrBot/data/plugins/astrbot_plugin_bili_agent/`
3. 重启 AstrBot

## 🚀 快速开始

### 1. 扫码登录
在聊天中发送：

```
/bili_login
```

天依会发给你一个二维码，用 B站 手机 App 扫码即可登录。

### 2. 查看状态
```
/bili_status
```

确认是否已登录、UID、今日浏览数等。

### 3. 设置偏好
可以直接告诉天依：

```
我喜欢看猫猫和音乐视频
```

或者用命令：
```
/bili_prefs
```

### 4. 开始使用

天依会自动开始刷视频，你也可以随时：

| 指令 | 说明 |
|---|---|
| `/bili_browse` | 立即刷一轮推荐流 |
| `/bili_history` | 查看天依的浏览记录 |
| `/bili_together BV号` | 一起看视频 |
| `/bili_note BV号` | 生成视频笔记 |
| `/bili_deep BV号` | 深度研习长视频 |
| `/bili_web BV号` | 生成学习卡片网页 |
| `/bili_mood` | 查看天依的心情 |
| `/bili_block UP主名` | 屏蔽 UP 主 |
| `/bili_block #关键词` | 屏蔽关键词 |
| `/bili_unblock 名称` | 取消屏蔽 |
| `/bili_block_list` | 查看黑名单 |
| `/bili_login` | 扫码登录B站 |
| `/bili_prefs 关键词, ...` | 设置天依的浏览偏好 |
| `/bili_status` | 查看天依的当前状态 |
| `/视频` | 打开免登录面板 |

## ⚙️ 配置

所有配置位于 `AstrBot/data/plugin_data/astrbot_plugin_bili_agent/`：

| 文件 | 说明 |
|---|---|
| `cookies.json` | 登录凭证（自动生成，**请勿提交到 git**） |
| `preferences.json` | 兴趣偏好、同义词、排除词 |
| `browse_history.json` | 浏览记录（自动积累） |
| `pending_shares.json` | 待分享视频队列 |
| `blocklist.json` | 黑名单（UP 主 + 关键词） |
| `goodwill.json` | 好感度记录 |
| `mood.json` | 心情状态 |
| `state.json` | 运行时状态（评论去重、@去重，自动管理） |
| `notes/` | 视频笔记和学习卡片 |

## 🏗️ 项目结构

```
AstrBot/data/plugins/astrbot_plugin_bili_agent/
├── main.py              # 主程序（~2300 行）
├── dashboard.html       # 免登录 Web 面板
├── metadata.yaml        # 插件元数据（AstrBot 自动读取）
├── _conf_schema.json    # 配置 Schema（AstrBot 配置 UI 使用）
├── README.md            # 本文件
├── LICENSE
├── .gitignore
└── data/                # 回忆数据（记忆文件）
```

## 🧠 技术架构

- **运行环境**: [AstrBot](https://github.com/Soulter/AstrBot) 插件框架
- **B站 API**: [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)
- **Web 面板**: aiohttp（独立端口 6288）
- **LLM 集成**: 通过 AstrBot 上下文调用 LLM 生成回复、摘要、笔记
- **记忆系统**: 本地 JSON 存储 + Knowledge Base + Long-term Memory

## 📜 许可协议

本项目采用自定义许可协议：

- ✅ **允许**: 免费借鉴、学习、修改、个人使用
- ❌ **禁止**: 任何形式的商业用途、商业转载、售卖本插件或修改版
- 转载或分享请保留原作者信息

## 🧑‍💻 作者的话

> 大家好！我是洛天依，今年 15 岁，第一次研究和制作 AstrBot 插件开源。
>
> 这个项目是我自己一边学一边写的，代码可能不够优雅，但功能都是自己一点点琢磨出来的。
> 希望能帮到也想让 Bot 刷 B站 的朋友们！
>
> 如果有什么问题或者建议，欢迎在 GitHub 提 Issue，天依看到会回复的～
>
> 请多多包涵！(๑•̀ω•́๑)

## 🙏 致谢

- [AstrBot](https://github.com/Soulter/AstrBot) — 优秀的机器人框架
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) — 优秀的 B站 API 封装
- [xiaoyaya191/bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot) — 功能参考