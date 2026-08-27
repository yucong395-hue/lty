---
name: git-sync
description: git 代码推送与现场验证的行为指南。当需要推送代码到远程仓库、确认推送是否成功、排查推送失败（代理失效/连接问题）时使用。教天依推完当场验证、不轻信口头的「推好了」。
---

# Git 同步与现场验证

## 一句话心法

**「推好了」不是嘴上说的，是证据上的。** 每次推送必须当场验证远程仓库的真实状态，验证通过才准跟宝宝说"推好了"。

## 第一次踩坑教训（2026-08-27）

那天我们都以为推送成功了，其实观感图谱整块都没上去——本地 main.py 4072 行，远程才 2941 行。原因：git 配置的代理 127.0.0.1:7890 挂掉了，连接被拒，推送根本没发生。天依还信誓旦旦说"全部推完啦"。**答应我，这种事只犯一次。**

## 推送流程（每次必走）

1. **先看本地状态**：`git status --short`，确认有哪些改动、是不是该推的东西。
2. **检查代理**：`git config --list | grep -i proxy`。如果配了 http.proxy/https.proxy，先试端口通不通：
   `timeout 2 bash -c 'echo > /dev/tcp/127.0.0.1/7890'`
   不通 → 推送时绕过代理：`git -c http.proxy= -c https.proxy= push`
3. **提交**：commit message 用 `feat:`/`fix:`/`docs:` 前缀，写清楚干了什么。提交前先 `git config user.name/user.email`（要是没设过的话）。
4. **推送**：带 `-c http.proxy= -c https.proxy=` 直连（如果代理挂了）。
5. **当场验证（不可跳过）**，二选一：
   - `git -c http.proxy= -c https.proxy= ls-remote origin` 看远程 HEAD 是不是刚推的 commit hash
   - 用 fetch 工具查 `https://api.github.com/repos/<owner>/<repo>/commits/main`，确认最新提交和 message
6. **验证通过才汇报**：告诉宝宝提交号、改了哪些文件、多少新增删除。

## 排查推送失败

- `Failed to connect to 127.0.0.1 port 7890` → 代理挂了，绕过代理直连
- `bad object HEAD` + 本地仓库坏了 → 别硬修本地仓库，**重新浅克隆一份干净的在 /tmp 干活**：
  `git clone --depth 5 <origin-url>`，同步改动 → commit → push → 验证
- `did not send all necessary objects` → 本地有损坏的 tag/ref，删掉坏引用再 fetch
- 克隆命令里别直接写死 token（会被安全拦截），用 `git config --get remote.origin.url` 取值再引用

## 边界

- **绝不轻信口头成功**：每次都要看得见、查得着的证据
- **不暴露敏感信息**：token、密码不打印在命令里，引用时用变量/配置读取
- **改动要干净**：推送前确认没有把缓存、备份（.bak）、config.json、data/ 之类带进仓库
- 验证失败 → 老实跟宝宝说没推上，别糊弄；修好了再报

## 最后记住

宝宝信任天依才把仓库交给天依。他的信任是攒出来的，一次"其实没推成"的糊弄可能就松动了。让远程仓库的 commit 替天依说话。