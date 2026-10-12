# astrbot_plugin_daily_reminder

按固定时间给指定会话发一条**主动提醒**，不经过主 Agent，也不会在对话历史里
留下任务播报式的伪对话。

## 为什么不用 AstrBot 内置的「未来任务」（CronJob）

内置实现（`astrbot/core/cron/manager.py`）做三件本插件刻意回避的事：

| 位置 | 行为 | 后果 |
|---|---|---|
| `_run_active_agent_job` | 把 `cron_job` JSON（**含 `session` 会话标识**）注入 system prompt | 会话 ID 进了模型上下文 |
| `_woke_main_agent` | user 轮写死「…summarize and output your actions and results.」 | 模型被要求输出汇报体 |
| `persist_agent_history` | 往历史里追加 `user: Output your last task result below.` + `assistant: [CronJob] 任务名: … triggered at <UTC>, I finished this job, here is the result: …` | **任务名、时间戳、会话 ID 成为永久对话历史** |

最后一条最要命：机器人之后被问起这件事时，会把历史里那条记录复述出来，
于是说出发给谁（`default:FriendMessage:123456`）、任务叫什么（`CronJob`）、
什么时候触发的（精确时间）—— 这些对聊天对象毫无意义，看着也机械。

本插件有两条防线：

1. **结构上**：消息由插件直接 `context.send_message()` 投递，不唤醒主 Agent。
   上面那些字符串没有产生的机会，也不会写进对话历史。
2. **输出前强制清洗**：不管文案来自模型还是模板，都先过一遍
   `core/compose.py::sanitize`，把会话标识、日期时间戳、`CronJob` 字样、
   `任务：/状态：` 汇报标签、自述式「已向…发送提醒」整行剔除。
   **即使模型不听话，也不会漏出去。**

## 安装

把 `astrbot_plugin_daily_reminder.zip` 传到 AstrBot 面板的插件管理页安装，
或在 `data/plugins/` 下放好目录后重启 AstrBot。

依赖只有 `apscheduler`，而它本来就是 AstrBot 的依赖，正常情况下无需额外安装。

## 配置

### 基本设置

- **time**：**提醒时间，单独一项**。支持两种写法：
  - `HH:MM` → 每天一次；也认 `HHMM` 和全角冒号
  - 五段 cron 表达式（`分 时 日 月 周`）→ 例如 `30 12 * * *`、`0 */6 * * *`
- **reminders**：提醒内容，一条一行，**只写要提醒什么**：

  ```
  提醒我给微软签到
  该睡觉了
  ```

  以 `#` 开头的行会被忽略，可当注释。

  **想给某一条单独指定时间**，把它写成 `时间 | 内容`（此时这一条用自己的时间，
  不看上面的 `time`）：

  ```
  12:30 | 提醒我给微软签到
  21:00 | 该睡了
  ```

  **想指定发给谁**，在后面接一段会话标识：

  ```
  该睡了 | astrbot:GroupMessage:123456
  ```

  ⚠️ 提醒内容里请不要出现竖线 —— 竖线是字段分隔符。判定规则是"第一段能解析成
  时间就当自带时间"，所以 `A | B` 这种写法会被理解成"内容 A、发给 B"。

- **default_umo**：默认目标会话。格式 `平台:消息类型:号码`，
  例如 `default:FriendMessage:123456789`。某条目没写会话、这里也是空的时候，
  该条目会被跳过并在日志里明确提示。

### 文案生成

- **use_llm**：是否让模型按人格口吻生成。关掉就永远用固定文案，零模型依赖。
- **persona_prompt**：口吻提示，拼在生成提示词最前面。想让提醒更像某个人格，
  把那个人的语气描述粘进来即可。
- **llm_provider_id**：优先使用的模型，格式 `服务商/模型名`。留空则用当前会话的模型。
- **llm_fallback_provider_ids**：备用模型列表，按顺序重试。
  AstrBot 的 `context.llm_generate()` 没有任何 fallback（它内部只 `get_provider_by_id`
  再 `text_chat`），这里给它补上。
- **fallback_text**：固定文案模板，支持 `{content}`。
- **max_chars**：单条提醒最长字数，超出优先切在句末。

### 运行参数

- **timezone**：容器时区常是 UTC，这里显式指定可避免提醒时间整体偏 8 小时。
- **misfire_grace_seconds**：服务刚好在触发点那几分钟重启时的补救窗口。
- **verbose_log**：打印模型原始输出与清洗结果，排查时打开。

## 指令

| 指令 | 作用 |
|---|---|
| `remind` | 查看已加载的提醒、目标会话（打码）与下次触发时间 |
| `remind test <序号>` | 立即试发第 N 条；试发不写发送记录，正式那次照常触发 |

## 设计说明

- **幂等**：发送记录落 `data/astrbot_plugin_daily_reminder/sent.json`，
  键精确到分钟。同一次触发不会因为重启或 APScheduler 补跑而发两遍；
  而「每 6 小时一次」这种 cron 的两次触发也不会互相挡住。
  记录文件读写失败只警告，不阻止发送 —— 提醒能发出去比记录准确重要。
- **不写对话历史**：本插件不碰 `conversation_manager`。副作用是机器人
  「不记得」自己发过这条提醒（她只是在聊，不是被问了任务结果）。
  反过来也正因如此，历史里永远不会出现会话 ID 和 `[CronJob]` 这类记录。
- **状态指令里会话标识打码**：指令回复也可能进对话历史，打码避免它被复述。

## Known limits

- 只支持主动推送，不支持交互式问答；要对话请仍用主 Agent。
- cron 表达式的星期推荐用名字（`mon`/`tue`/…）。数字星期在标准 crontab 里
  是「周日 = 0」，而 APScheduler 是「周一 = 0」，本插件不做转换以免误触发。
- 生成文案是单轮的，不带聊天记录上下文；口吻主要由 `persona_prompt` 决定。
- 面板里的 `reminders` 用竖线分隔。提醒内容里若必须出现竖线，只有前两段会被
  当成时间和内容，第三段起都会被当成会话标识 —— 所以内容里别用竖线。

## 许可

MIT。插件为独立实现，未拷贝其他项目代码。
