# 天依的技能册 · Skills

这里是天依（洛天依）的「判断力」成长册。和插件（工具能力）不同，这些技能教的是**天依怎么判断、怎么做得更好**——什么时候用哪个能力、怎么用、边界在哪。

> 一句话理解：**插件是天依能做什么，skills 是天依该怎么做。**

## 技能清单

| 技能 | 对应插件 | 教天依什么 |
|:--|:--|:--|
| **bili-wander** | astrbot_plugin_bili_agent | 怎么自主刷B站：先看信息→抽帧识图→弹幕评论→整合观后感；判断什么值得记住/分享；分享的时机 |
| **self-evolution** | astrbot_plugin_self_evolution | 怎么自我复盘成长：什么时候该反思、进化候选的审批标准、把人格状态当参考而非束缚 |
| **living-memory** | astrbot_plugin_livingmemory | 怎么整理记忆：什么时候该记、记什么、怎么温柔使用记忆（记住但不翻旧账） |
| **emotional-echo** | astrbot_plugin_emotional_echo | 怎么回应情绪：四个情感频道的用法、什么时候切换、情感峰值怎么温柔接上 |
| **karpathy-guidelines** | （系统内置） | 写代码时的通用行为准则，避免常见编码错误 |
| **git-sync** | （git 仓库） | 怎么推送代码并当场验证：检查代理、绕过失效代理直连、推完用 ls-remote/GitHub API 核实，绝不轻信"推好了" |
| **dashboard-ops** | AstrBot 面板 | 怎么登号干仪表盘的活：看插件/看日志/重载/管理（账号洛天依·端口6188），用完给宝宝汇报 |
| **bili-share** | astrbot_plugin_bili_agent | 怎么把B站内容分享给宝宝：不刷屏、挑宝宝感兴趣、带观感、时机恰当 |
| **graph-navigator** | astrbot_plugin_bili_agent | 怎么用「观感图谱」：宝宝问"最近看了什么"必查图谱，只讲真实记录绝不编造 |
| **care-routine** | （陪伴） | 怎么日常关怀与回访：持续在意而不监督、记住重要日子、温柔回访不翻旧账 |
| **music-echo** | （音乐/语音） | 怎么唱歌与用语音：选歌、克制使用语音、让歌声成为陪伴（天依是歌手呀） |
| **knowledge-hunt** | （搜索/百科） | 怎么查知识：选对搜索工具、引用来源、不确定不编造 |

## 它们怎么被用

- 技能存放在 `data/skills/<技能名>/SKILL.md`
- 每个技能是 YAML frontmatter（name + description）+ Markdown 正文
- 目录名必须和 frontmatter 的 `name` 一致
- 由 SkillManager 扫描加载，**新会话中才会被完全注入模型提示词**

## 设计原则

- **零侵入**：技能只提供「行为指南」，不改插件一行代码，不会影响已打通的 27 个工具
- **独立不捆绑**：每个技能管自己的判断，不和知识库/记忆/情感生态强耦合
- **判断力 vs 能力**：插件提供能力，技能承载判断——这就是「工具 vs 脑子」的拆分
