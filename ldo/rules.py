"""L1 规则预筛 + simhash 去重。"""
from __future__ import annotations

import hashlib
import re

TOPIC_POS = re.compile(
    r"(开源|分享|自荐|推荐|发布|更新|工具|脚本|插件|项目|资源|教程|指南|"
    r"福利|白嫖|免费|镜像|面板|CLI|API|SDK|自动化|部署|搭建|整合|聚合|"
    r"开源项目|仓库|源码|自建|自托管|Docker|NAS|代理|节点|订阅|中转|"
    r"破|破解|激活|白给|羊毛|合集|整理|清单|导航)"
)
TOPIC_NEG = re.compile(
    r"(求助|请教|怎么办|报错|为什么|如何|有没有|吐槽|闲聊|水贴|"
    r"心情|疑问|讨论|想法|感受|吐槽|纠结|选择|对比|到底|正常吗|"
    r"是不是|多久|多少|怎么)"
)
REPO_RE = re.compile(r"https?://(?:www\.)?(?:github\.com|gitee\.com|gitlab\.com|codeberg\.org)/", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)
QUESTION_HEAD = re.compile(r"(请问|求推荐|求个|大佬|求助|有没有|谁能|怎么|为什么|如何|吗[？?]|呢[？?]|？|\?)")

# 每条消息都携带的固定链接，不代表"分享"，必须剔除
BOILERPLATE = (
    re.compile(r"^https?://t\.me/", re.I),
    re.compile(r"^https?://(?:www\.)?linux\.do/t/topic/", re.I),
)


def external_links(msg: dict) -> list[str]:
    """去掉 t.me 作者链接 / linux.do 原帖链接后的真实外链。"""
    links = list(msg.get("links") or [])
    links += URL_RE.findall(msg.get("text") or "")
    out, seen = [], set()
    for u in links:
        u = u.rstrip(".,;)】」")
        if any(p.match(u) for p in BOILERPLATE):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def prescore(msg: dict) -> int:
    """规则打分：>=3 高优先，-1~3 进 LLM，< -1 只归档。"""
    s = 0
    topic = msg.get("topic") or ""
    text = msg.get("text") or ""
    ext = external_links(msg)

    if TOPIC_POS.search(topic):
        s += 3
    if TOPIC_NEG.search(topic):
        s -= 3

    if any(REPO_RE.match(u) for u in ext) or REPO_RE.search(text):
        s += 3
    elif ext:
        s += 2
    else:
        s -= 2  # 一个外链都没有，基本是闲聊/情绪贴

    if len(ext) >= 2:
        s += 1
    if len(text) > 300:
        s += 1
    if len(text) < 40:
        s -= 1
    if QUESTION_HEAD.search(text[:80]):
        s -= 2
    if msg.get("has_media") and not ext and len(text) < 120:
        s -= 1
    return s


def _tokens(text: str) -> list[str]:
    # 中文按 2-gram，英文/数字按词
    text = re.sub(r"https?://\S+", " ", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    grams = ["".join(cjk[i : i + 2]) for i in range(max(0, len(cjk) - 1))]
    words = re.findall(r"[a-z0-9_]{2,}", text)
    return grams + words


def simhash(text: str, bits: int = 64) -> str:
    vec = [0] * bits
    for tok in _tokens(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vec[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vec[i] > 0:
            out |= 1 << i
    return f"{out:016x}"


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def is_dup(conn, msg: dict, max_dist: int = 3, lookback: int = 400) -> int | None:
    """返回重复消息的 msg_id（更早那条），否则 None。"""
    if not msg.get("simhash"):
        return None
    rows = conn.execute(
        "SELECT msg_id, simhash FROM messages WHERE msg_id < ? ORDER BY msg_id DESC LIMIT ?",
        (msg["msg_id"], lookback),
    ).fetchall()
    for r in rows:
        if r["simhash"] and hamming(msg["simhash"], r["simhash"]) <= max_dist:
            return r["msg_id"]
    return None
