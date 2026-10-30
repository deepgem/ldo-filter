# AGENTS.md

给 AI agent 的操作说明。**开始任何改动前先读完本文**，这里记的都是踩过的坑。

项目功能说明看 [README.md](README.md)，本文只讲「怎么安全地改它」。

---

## 1. 这是个什么项目

linux.do 频道的消息过滤器。60 秒轮询 `t.me/s/` → 规则预筛 → Jev 语义判定 → 高分推送到 Telegram。

**当前状态：正在生产运行。** 任何改动都可能影响一个活着的服务。

---

## 2. 红线（改动前必须确认）

### 2.1 `.env` 永不进 git

含 LLM key、TYPESAFE key、bot token。已在 `.gitignore`，但**提交前仍要验证**：

```bash
git diff --cached --name-only | grep -E "^\.env" && echo "危险"
```

`.env` 已 `chmod 600`，不要改成 644。

### 2.2 `data/` 和 `logs/` 永不进 git

`data/ldo.db` 含全部频道消息原文（含用户昵称），`logs/` 含运行记录。已在 `.gitignore`。

### 2.3 不要擅自全量重判

`rejudge --reset` 会清空当前版本的**全部**判定再跑一遍。虽然 Jev 只要 17 秒，但它会：
- 触发新推送（分数变化后，原本不达标的消息可能越过 0.55）
- 让 `pushes` 表失去意义

**调试期一律加 `--limit`**，或者直接 `python3 -m ldo judge --limit 15`。

### 2.4 不要改 `prompt_version` 之外的判定历史

`verdicts` 表主键是 `(msg_id, version)`。改判定标准时**必须**同时改 `config.json` 的 `llm.prompt_version`（`v3` → `v4`），否则新判定会覆盖旧记录，失去对比能力。

### 2.5 服务在跑，改代码必须重启

Python 已把模块加载进内存，`git checkout` 不会影响运行中的进程。

```bash
launchctl kickstart -k gui/$(id -u)/com.ldo.filter
```

不重启 = 还在跑旧逻辑，而且 `logs/run.log` 会让你误以为改动生效了。

---

## 3. 标准改动流程

```bash
cd /Users/liszt/Documents/deepseek-harness/default-workspace/ldo-filter

# 1. 改代码
# 2. 语法检查
python3 -m compileall -q ldo tools

# 3. 小样本验证（绝不要直接跑全量）
python3 -m ldo judge --limit 5

# 4. 重启服务
launchctl kickstart -k gui/$(id -u)/com.ldo.filter

# 5. 确认没崩
sleep 10 && tail -10 logs/run.log

# 6. 提交
git add -A && git commit -m "说明改了什么、为什么"
```

**第 3 步和第 5 步不能省。** 这个项目改坏了不会立刻报错，只会在推送时表现为「漏推」或「刷屏」。

---

## 4. 代码结构

```
ldo/
├── config.py        配置加载（config.json + .env → dict）
├── net.py           urllib 封装，重试 + gzip + 退避
├── db.py            SQLite schema 与读写
├── parse.py         t.me/s/ HTML 解析（自带 mini DOM）
├── rules.py         L1 预筛：话题关键词加权 + simhash 去重
├── classify.py      后端 A：通用 LLM，生成 JSON
├── classify_jev.py  后端 B：TypeSafe Jev，结构化返回（当前生效）
├── publish.py       推送 + 日报
└── cli.py           命令行入口，_judge() 负责后端分派
```

**改判定逻辑优先改 `classify_jev.py`**，因为 `config.json` 的 `classifier` 当前是 `jev`。

---

## 5. 两个后端的差异（容易踩）

| | `jev`（当前） | `llm`（备选） |
|---|---|---|
| 配置 | `classify_jev.py` 的 `CATEGORIES`（**源码里的字典，不在 config.json**） | `classify.py` 的 `PROMPT` + `config.json` 的 `examples` |
| 少样本 | **不支持**，`examples` 对它完全无效 | 支持 |
| 返回 | `choice` + `noul` + `confidence` | 生成的 JSON |

**最常见的错误**：改 `config.json` 的 `examples` 后发现判定没变化 —— 因为当前跑的是 Jev，它不读 `examples`。要调 Jev 必须改 `CATEGORIES` 的 `criteria` 描述。

---

## 6. Jev 的三条后处理规则（`classify_one`）

这三条是必须的，删了会立刻出问题：

```python
# 1. 低置信度打折
if conf < min_confidence:      # 默认 0.55
    score *= 0.85

# 2. 低价值类别封顶（不越过推送线）
if label in LOW_VALUE_LABELS:  # question/news/chat/promo/spam
    cap = 0.35 if conf >= cap_confidence else 0.54
    score = min(score, cap)

# 3. 社区黑话兜底
if _is_promo(msg):             # 标题含「富可敌国」或正文含拉新话术
    label, score = "promo", min(score, 0.25)
```

- 规则 2 不加 → 被判 `chat` 但 `worth` 给 0.58 的吐槽会推出去
- 规则 3 不加 → 商家广告被当成「分享资源」

**新增社区黑话**（`机场`、`中转站` 之类）要手动加进 `LURE_PHRASES`，Jev 理解不了这些词的感情色彩。

---

## 7. 数据库

```sql
messages (msg_id PK, posted_at, author, topic, text, links, has_media, simhash, prescore)
verdicts (msg_id, version, model, label, score, tags, reason, extracted)  -- PK(msg_id,version)
pushes   (msg_id, channel, pushed_at)  -- PK(msg_id,channel)
state    (key, value)  -- last_seen_id / backfill_cursor / last_digest_date
```

- 当前是**单频道**设计，`msg_id` 是主键。多频道要改成 `(channel, msg_id)` 复合主键
- `pushes` 只记 `msg_id`，**没有 `message_id`**，所以误推无法自动撤回
- 想看某条为什么没推：`sqlite3 data/ldo.db "select * from messages where msg_id=XXX"` 看 `prescore`

---

## 8. 常用调试命令

```bash
python3 -m ldo doctor              # 全链路自检，改完先跑这个
python3 -m ldo stats               # 漏斗统计
python3 -m ldo show -n 30          # 最近消息和分数
python3 -m ldo judge --limit 5     # 只判 5 条（调试专用）
python3 -m tools.compare --n 10    # LLM vs Jev 逐条对比 + 计时
python3 -m ldo flush               # 静默积压，防止启动刷屏
```

`tools/compare.py` 是验证判定改动效果的首选工具 —— 它不写库，纯对比，零副作用。

---

## 9. 常见任务

### 加一个新的内容类别

1. `classify_jev.py` 的 `CATEGORIES` 加键值
2. `publish.py` 的 `SHORT_LABEL` 和 `LABEL_EMOJI` 加映射
3. 如果属于低价值类别，加进 `LOW_VALUE_LABELS`
4. `config.json` 的 `prompt_version` 升版本
5. `python3 -m ldo judge --limit 10` 验证
6. 确认无误后再考虑 `rejudge --reset`

### 调推送阈值

只改 `config.json` 的 `thresholds.push`，**不需要重判**（分数不变，只是筛选线变了）。
改完重启服务即可。

### 加频道（尚未支持）

需要改五处：`is_dup()` 的去重范围、`state` 的游标键、`_post_url()` 的论坛链接规则、推送署名、`config.json` 的 `channel` 字段。表结构也要改成复合主键。

---

## 10. 已知陷阱

| 陷阱 | 后果 |
|---|---|
| 改完不 `kickstart` | 跑的还是旧代码，日志看起来正常 |
| 改 `examples` 调 Jev | 完全无效，Jev 不读这个字段 |
| 改 `prompt_version` 忘了 | 新判定覆盖旧记录，失去 A/B 对比 |
| `rejudge --reset` 不加 `--limit` | 全量重判，可能触发一批意外推送 |
| 只测「该推的推了」 | 还要测「不该推的没推」——误推比漏推更烦人 |
| 改 `BOILERPLATE` 时手滑 | 每条消息都带的 `t.me/<作者>` 链接会被当成分享信号，闲聊全部虚高 |

---

## 11. 环境

```
Python 3.15（/opt/homebrew/bin/python3），零第三方依赖
服务 label: com.ldo.filter
plist: ~/Library/LaunchAgents/com.ldo.filter.plist
日志: logs/run.log（正常）、logs/run.err.log（异常）
仓库: https://github.com/deepgem/ldo-filter (private)
基线: tag v0.1.0
```

**不要引入第三方依赖**。这个项目刻意保持纯标准库，为了 Docker 镜像小、部署零门槛。如果确实需要，先和用户确认。
