# ldo-filter

linux.do 频道（[@linux_do_channel](https://t.me/linux_do_channel)）的 AI 语义过滤流水线。
每天 ~800 条消息 → 规则预筛 → 语义判定 → 只把「分享项目 / 分享资源 / 服务规则变更」推给你。

零第三方依赖，纯 Python 标准库。

## 实测效果（805 条真实消息，182 条已判定）

| 阶段 | 条数 | 占比 |
|---|---|---|
| 抓取归档 | 805 | 100% |
| 进语义判定（规则预筛后） | 182 | 23% |
| score ≥ 0.55（推送） | 36 | 20% |
| 其中 ≥ 0.80 | 18 | 10% |

各类别平均分（区分度）：

| 类别 | 条数 | 均分 |
|---|---|---|
| `share_project` 项目 | 11 | 0.729 |
| `share_resource` 资源 | 13 | 0.724 |
| `service_update` 服务动态 | 12 | 0.690 |
| `tutorial` 教程 | 10 | 0.531 |
| `promo` 推广 | 11 | 0.304 |
| `chat` 闲聊 | 35 | 0.238 |
| `question` 提问 | 69 | 0.232 |
| `news` 资讯 | 21 | 0.126 |

## 架构

```
t.me/s/ 轮询 ──► parse.py ──► rules.py ──► classify_jev.py ──► publish.py
   (60s)          解析DOM      预筛+去重    Jev / LLM 判定      TG 推送/日报
                                    │              │
                                    └──────► SQLite 全量归档（可回溯重判）
```

**关键点**：频道消息正文结构固定为 `作者 (@user) 在 <话题标题> 中发帖` + 正文。
`话题标题` 是极强的分类特征，规则层靠它 + 外链信号就能砍掉 77% 的消息，
只有剩下的 23% 才进入语义判定。

## 安装

```bash
cd /Users/liszt/Documents/deepseek-harness/default-workspace/ldo-filter
python3 -m ldo doctor        # 全链路自检，先看这个
```

`.env`（已 chmod 600）：

```
# 判定后端（classifier=llm 时使用）
LLM_BASE_URL=http://127.0.0.1:3425/v1
LLM_API_KEY=<密钥>
LLM_MODEL=agentscope/deepseek-v4.1-flash-1017

# 判定后端（classifier=jev 时使用，当前生效）
TYPESAFE_API_KEY=<密钥>

# 推送
TG_BOT_TOKEN=
TG_CHAT_ID=
```

### 接通 Telegram 推送

1. 在 [@BotFather](https://t.me/BotFather) 建 bot，把 token 填进 `TG_BOT_TOKEN`
2. **给这个 bot 发任意一条消息**（必须先发，否则拿不到 chat_id）
3. `python3 -m ldo whoami` —— 自动发现 chat_id 并打印
4. 把 chat_id 填进 `TG_CHAT_ID`，`python3 -m ldo notify` 验证

## 命令

| 命令 | 作用 |
|---|---|
| `python3 -m ldo doctor` | **全链路自检**：配置/数据库/抓取/判定/bot/chat_id |
| `python3 -m ldo tick` | 拉一次增量 → 判定 → 推送 |
| `python3 -m ldo run` | 常驻轮询（每 60s），到点自动发日报 |
| `python3 -m ldo backfill --pages 250` | 回溯历史 5000 条（冷启动建语料） |
| `python3 -m ldo judge --limit 15` | 判定待处理消息（默认受 `max_per_cycle` 限制） |
| `python3 -m ldo rejudge --reset` | 清空当前版本判定后重判全部 |
| `python3 -m ldo digest --hours 24` | 生成日报（`--send` 顺带推送） |
| `python3 -m ldo flush` | 静默积压：标记已推送但不发送，避免启动刷屏 |
| `python3 -m ldo stats` | 漏斗统计 |
| `python3 -m ldo show -n 30` | 看最近消息和分数 |
| `python3 -m ldo whoami` | 自动发现 bot chat_id |
| `python3 -m ldo notify` | 测试推送通道 |

对比工具：

```bash
python3 -m tools.compare --n 20 --time 6    # LLM vs Jev 逐条对比 + 计时
```

## 常驻运行

**launchd（推荐，一条命令）**

```bash
./install.sh            # 先跑 doctor 自检，通过后生成 plist 并加载
./install.sh status     # 状态 + 最近日志
./install.sh logs       # 实时跟踪
./install.sh stop       # 停止并卸载
```

`install.sh` 会自动把当前目录和 python 路径注入 plist，不需要手改。

关键配置：`RunAtLoad` + `KeepAlive` —— **开机登录后自动启动，进程挂了 3 秒内自动拉起**（已实测 `kill -9`）。

**cron（备选）**

```cron
*/1 * * * * cd /path/to/ldo-filter && /opt/homebrew/bin/python3 -m ldo tick >> logs/cron.log 2>&1
0 21 * * *  cd /path/to/ldo-filter && /opt/homebrew/bin/python3 -m ldo digest --send >> logs/cron.log 2>&1
```

## 推送格式

```
🎁【资源0.95】(喜+1) Epic 本周限免：《Sorry We're Closed》
                                          ← 空行
大家好啊, 又是我来分享限免游戏。这次是 Epic 本周送出《Sorry We're Closed》，
一款复古霓虹风格的生存恐怖游戏，领取后永久入库。玩家扮演受到恶魔诅咒的少女…
                                          ← 空行
(@LINUX DO)
```

- **首行标题和末行 `(@LINUX DO)` 都是链接，都跳 linux.do 论坛原帖**（不是 t.me 频道消息）
- 正文取原帖内容前 **150 字**（`publish.BODY_CHARS` 可改），超出补 `…`
- 关闭链接预览卡片（`disable_web_page_preview` + `link_preview_options.is_disabled`），
  否则 Telegram 会把链接展开成一大块卡片
- 正文里的 `[image]`、`[Screenshot_xxx]`、`[503CDC5F...]` 这类媒体占位符会被清洗掉

标签与 emoji 映射：

| 标签 | emoji | 标签 | emoji |
|---|---|---|---|
| `share_project` 项目 | 🚀 | `tutorial` 教程 | 📖 |
| `share_resource` 资源 | 🎁 | `news` 资讯 | 📰 |
| `service_update` 服务动态 | 📡 | `question` 提问 | ❓ |
| `chat` 闲聊 | 💬 | `promo` 推广 | 📢 |
| `spam` 水贴 | 🗑 | `unknown` 未知 | ❔ |

## 判定后端

`config.json` 的 `classifier` 二选一，两个后端共用同一套判定表和推送格式，切换不丢历史。

| | `jev`（当前） | `llm`（备选） |
|---|---|---|
| 模型 | TypeSafe Jev（System One） | 任意 OpenAI 兼容端点 |
| 输出 | 结构化 `choice` + `noul`，无文本生成 | 生成 JSON 再解析 |
| 延迟 | **426~641 ms**，几乎无抖动 | 2.4s ~ 32.6s，方差大 |
| 失败模式 | 无 JSON 解析失败 | 偶发格式漂移、超时重试 |
| 计费 | 只算输入 token（$42/Btok） | 输入 + 输出 |
| 少样本 | 无，只能靠 `criteria` 描述 | 支持 `examples` few-shot |

实测（同一批 20 条）：分类一致率 **90%**，分数平均差 0.104，**Jev 零误推**，
171 条全量重判 **17 秒**（LLM 需要 3 分 12 秒）。

## Jev 调优

### 类别定义

编辑 `ldo/classify_jev.py` 的 `CATEGORIES`。Jev 没有 few-shot 机制，
**所有判断依据都在这几行描述里**，写得越具体越准：

```python
"promo": "商家自己发的拉新广告：充值折扣、注册送额度、回帖领奖、进群领福利。
          linux.do 社区里带【富可敌国】标签的帖子一律属于此类。
          作者免费分享自己做的工具不算 promo",
```

`service_update` 是后来加的类别，用来抓「某平台限速了 / 额度取消了 / 涨价了」这类
直接影响你还能不能用、怎么用的规则变更 —— 它和「Anthropic 把 API 上限提到 128MB」
这种纯行业播报不是一回事。

### 置信度路由

Jev 返回 `confidence`，代码里按三条规则处理（`classify_one`）：

```python
# 1. 低置信度：分类不可靠，只轻微打折
if conf < min_confidence:        # 默认 0.55
    score *= 0.85

# 2. 类别一致性：低价值类别一律不越过推送线
if label in LOW_VALUE_LABELS:    # question/news/chat/promo/spam
    cap = 0.35 if conf >= cap_confidence else 0.54
    score = min(score, cap)

# 3. 社区黑话兜底：Jev 不懂 linux.do 的广告标签
if _is_promo(msg):               # 标题含「富可敌国」或正文含拉新话术
    label, score = "promo", min(score, 0.25)
```

第 2 条是必须的 —— 不封顶的话，一条被判 `chat` 但 `worth` 给 0.58 的吐槽会直接推出去。
第 3 条同理，「富可敌国」是社区广告标签，Jev 只看字面理解不了，只能代码兜底。

调完改 `config.json` 的 `llm.prompt_version`（如 `v3` → `v4`），然后：

```bash
python3 -m ldo rejudge --reset
```

### 并发与限流

```json
"jev": { "concurrency": 6 },
"llm": { "max_per_cycle": 15 }
```

`max_per_cycle` 是调试期的刹车 —— `run` 每轮最多判 15 条，避免一次重判烧掉整晚额度。
稳定运行后可以调到 500。

## LLM 后端调优

编辑 `ldo/classify.py` 的 `PROMPT`，然后在 `config.json` 的 `examples` 里加人工标注：

```json
"examples": [
  {"label": "service_update", "score": 0.72,
   "topic": "workbuddy 免费额度开始限制每小时用量",
   "why": "具体服务的额度规则变更，直接影响用户还能用多少"},
  {"label": "news", "score": 0.25,
   "topic": "Anthropic 将 Claude API 请求上限提高至 128 MB",
   "why": "行业新闻播报，与用户实际使用无关"}
]
```

给 30 条正例 + 30 条负例，准确率能从 ~75% 拉到 ~90%。
（注意：`examples` **只对 LLM 后端生效**，Jev 不读这个字段。）

## 规则层

`ldo/rules.py`：

- `TOPIC_POS` / `TOPIC_NEG` —— 话题标题的正负关键词
- `BOILERPLATE` —— 每条消息都带的固定链接（`t.me/<作者>`、`linux.do/t/topic/*`），
  必须排除，否则闲聊也会被当成「带链接」而虚高加分
- `prescore()` —— 加权打分，`>= -1` 才进语义判定

## 数据

`data/ldo.db`（SQLite）

- `messages` —— 全量消息原文，永不过期
- `verdicts` —— 判定结果，按 `(msg_id, prompt_version)` 存，换版本不丢历史
- `pushes` —— 已推送记录，防重复打扰
- `state` —— 游标（`last_seen_id`、`backfill_cursor`、`last_digest_date`）

**因为全量归档，任何时候都能回溯**：想补看上周的，`python3 -m ldo show`；
想换标准重判，`rejudge --reset` 就行，零数据丢失。

## 已知限制

- `t.me/s/` 是公开预览，若频道关闭预览则失效 —— 届时把 `ldo/fetch.py` 换成 Telethon 即可，
  解析层不用动
- 图片内容不读（`has_media` 只做标记），纯图片帖会漏
- **Jev 不懂社区黑话**：「富可敌国」「机场」「中转站」这类词的褒贬含义它判断不了，
  靠 `_is_promo` 代码兜底，新增黑话要手动加进 `LURE_PHRASES`
- **Jev 没有 few-shot**：调准确率只能改 `criteria` 描述，不如 LLM 加样本灵活
- 短链（t.cn / bit.ly）无法看到落地页，只能靠正文语义判断
- 推送消息的 `message_id` 没有落库，误推无法自动撤回
