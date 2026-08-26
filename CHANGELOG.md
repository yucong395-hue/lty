# CHANGELOG

所有重要变更都会记录在这个文件中。

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布] 2026-08-26

### 修复（7 轮联查共 15 个问题）

#### 硬编码路径（换环境不再踩坑）
- **bili_agent**：`_EVENT_BUS_PATH` 从写死的 `/root/AstrBot/data/plugins` 改为从插件位置动态推导，兼容任何 AstrBot 安装路径
- **emotional_echo**：event_bus 导入路径从插件自身目录改为父目录（event_bus.py 实际所在位置）
- **emotional_echo**：LivingMemory 联动路径 3 处硬编码 `/root/AstrBot` 改为动态推导（`_memory_paths`、知识库查询、情绪记忆写入）

#### 跨插件联动（事件总线真正打通）
- **emotional_echo**：删除插件目录内私藏的 event_bus.py 副本，三插件共用 `/data/plugins/event_bus.py` 同一单例
- **bili_agent / emotional_echo**：事件广播增加 `sender_id + group_id` 透传
- **self_evolution**：`_on_emotion_peak` 优先使用标准会话解析（sender_id/group_id），不再依赖 `unified_msg_origin` 解析，修复私聊画像写进 `private_webchat:...` 读不到的问题
- **self_evolution**：修复更新检查指向原作者仓库，`update_notify_repo` 及相关 4 处（config.py / _conf_schema.json / metadata.yaml / README.md）统一指向 `yucong395-hue/lty`

#### 并发与数据安全
- **self_evolution**：`upsert_fact` 读-改-写加 `asyncio.Lock`，防止情绪峰值与正常对话并发写画像丢更新
- **bili_agent**：三个后台任务（评论、自动刷、公共服务器）统一由 `_track_task` 管理，terminate 时全部取消
- **bili_agent / self_evolution**：冷启动与卸载时清理后台任务与事件总线注册（on/off 配对）

#### 内存与资源
- **bili_agent**：`_commented_videos` 加 1000 条上限（保留最近 500 条），防止无限增长
- **bili_agent**：`mood_boost` 情绪关键词 4 小时自动过期，不再永久残留
- **self_evolution**：情绪峰值冷却字典 128 条上限 + 1 小时自动清理，防止多用户场景膨胀

#### 配置与行为
- **bili_agent**：刷视频 user_id 从硬编码测试残留改为真实会话，无会话时跳过联动
- **bili_agent**：`_mood_loop` 间隔从写死 900 秒改为可配置 `browse.mood_interval_seconds`（默认 900），同步补齐 `_conf_schema.json` 配置项并修正嵌套读取路径
- **self_evolution**：写画像加 5 分钟冷却 + 128 条上限
- **bili_agent**：pending_shares 队列保留最近 10 条，避免无限堆积

### 安全
- 复查无 SQL 注入（全部参数化查询）、无 eval/exec、公共面板 notes 接口有路径遍历防护

## 说明

- 仓库地址：https://github.com/yucong395-hue/lty
- 三个插件需配合根目录 `event_bus.py` 使用，联通后才具备完整联动能力
