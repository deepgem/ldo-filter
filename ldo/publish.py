"""推送层：Telegram Bot 私聊 + 本地 Markdown 日报。"""
from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone

from . import db, net
from .config import LOG_DIR

TG_API = "https://api.telegram.org/bot{token}/{method}"
LABEL_CN = {
    "share_project": "🟢 项目",
    "share_resource": "🔵 资源",
    "tutorial": "🟡 教程",
    "news": "⚪️ 资讯",
    "question": "⚪️ 提问",
    "chat": "⚪️ 闲聊",
    "promo": "🔴 推广",
    "spam": "🔴 水贴",
    "unknown": "⚪️ 未知",
}


def _esc(s: str) -> str:
    return htmllib.escape(s or "", quote=False)


def tg_call(cfg: dict, method: str, payload: dict, timeout: float = 20) -> dict:
    token = cfg["telegram"]["bot_token"]
    if not token:
        raise RuntimeError("TG_BOT_TOKEN 未配置")
    return net.http_post_json(TG_API.format(token=token, method=method), payload, timeout=timeout, retries=2)


def discover_chat_id(cfg: dict) -> str | None:
    """从 getUpdates 里取最近一次私聊的 chat id（先给 bot 发条消息）。"""
    data = tg_call(cfg, "getUpdates", {"limit": 20, "timeout": 0})
    for upd in reversed(data.get("result") or []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            return str(chat["id"])
    return None


def send_message(cfg: dict, text: str, chat_id: str | None = None) -> bool:
    cid = chat_id or cfg["telegram"]["chat_id"]
    if not cfg["telegram"]["bot_token"] or not cid:
        return False
    try:
        tg_call(
            cfg,
            "sendMessage",
            {
                "chat_id": cid,
                "text": text[:4000],
                "parse_mode": "HTML",
                # 关掉链接预览卡片：t.me 链接会被展开成频道卡片，很吵
                "disable_web_page_preview": True,
                "link_preview_options": {"is_disabled": True},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[push] FAIL: {e}")
        return False


def _row_links(row) -> list[str]:
    try:
        return json.loads(row["links"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


_SKIP = ("https://t.me/", "http://t.me/", "https://linux.do/t/topic/", "https://www.linux.do/t/topic/")


LINUXDO_RE = re.compile(r"https?://(?:www\.)?linux\.do/t/topic/\d+(?:/\d+)?")


def _post_url(row, channel: str) -> str:
    """优先返回 linux.do 论坛原帖链接；找不到才退回频道消息链接。"""
    for u in _row_links(row):
        m = LINUXDO_RE.match(u)
        if m:
            return m.group(0)
    m = LINUXDO_RE.search(row["text"] or "")
    if m:
        return m.group(0)
    return f"https://t.me/{channel}/{row['msg_id']}"


def _best_url(row, ext: dict) -> str:
    """优先真实外部链接，最后才退到 linux.do 原帖。"""
    cands = [u for u in _row_links(row) if not u.startswith(_SKIP)]
    if cands:
        return cands[0]
    url = ext.get("url") or ""
    if url and not url.startswith(_SKIP):
        return url
    return cands[0] if cands else (url or "")


LABEL_EMOJI = {
    "share_project": "🚀",
    "share_resource": "🎁",
    "service_update": "📡",
    "tutorial": "📖",
    "news": "📰",
    "question": "❓",
    "chat": "💬",
    "promo": "📢",
    "spam": "🗑",
    "unknown": "❔",
}

SHORT_LABEL = {
    "share_project": "项目",
    "share_resource": "资源",
    "service_update": "服务动态",
    "tutorial": "教程",
    "news": "资讯",
    "question": "提问",
    "chat": "闲聊",
    "promo": "推广",
    "spam": "水贴",
    "unknown": "未知",
}

BODY_CHARS = 150


def _clean(s: str) -> str:
    """压平空白、去掉 Telegram 里的 [image] 占位标记。"""
    s = re.sub(
        r"\[(?:image|photo|video|sticker|file|Screenshot[^\]]*|[0-9A-Fa-f]{8,})\]",
        "",
        s or "",
        flags=re.I,
    )
    s = s.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _title_body(row) -> tuple[str, str]:
    """拆出标题与正文，正文截到 BODY_CHARS。"""
    title = _clean(row["topic"] or "")
    body = _clean(row["text"] or "")

    if not title:
        # 无话题标题时，用正文开头当标题，正文从标题之后继续
        title = body[:40] + ("…" if len(body) > 40 else "")
        body = body[40:] if len(body) > 40 else ""
    elif body.startswith(title):
        body = body[len(title):]

    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS].rstrip() + "…"
    return title, body


def _entry(row, channel: str) -> tuple[str, str, str]:
    """返回 (标题行HTML, 正文纯文本, 论坛原帖链接)。推送和日报共用。"""
    label = SHORT_LABEL.get(row["label"], row["label"] or "未知")
    emoji = LABEL_EMOJI.get(row["label"], "❔")
    link = _post_url(row, channel)
    title, body = _title_body(row)
    head = f'{emoji}【{label}{row["score"]:.2f}】<a href="{link}">{_esc(title[:80])}</a>'
    return head, body, link


def format_push(row, channel: str) -> str:
    """单条推送：
    🎁【资源0.65】<a href="论坛原帖">标题</a>

    正文前 150 字

    (@LINUX DO)
    """
    head, body, link = _entry(row, channel)
    foot = f'<a href="{link}">(@LINUX DO)</a>'
    return f"{head}\n\n{_esc(body)}\n\n{foot}" if body else f"{head}\n\n{foot}"


def push_new(conn, cfg: dict, verbose: bool = True) -> int:
    version = cfg["llm"]["prompt_version"]
    threshold = cfg["thresholds"]["push"]
    rows = db.unpushed_above(conn, version, threshold)
    if not rows:
        return 0
    sent = 0
    for r in rows:
        if send_message(cfg, format_push(r, cfg["channel"])):
            db.mark_pushed(conn, r["msg_id"], "push")
            sent += 1
    if verbose:
        print(f"[push] sent={sent}/{len(rows)}")
    return sent


def _digest_rows(conn, cfg: dict, hours: int, top: int):
    version = cfg["llm"]["prompt_version"]
    lo = cfg["thresholds"]["digest"]
    hi = cfg["thresholds"]["push"]
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return db.digest_rows(conn, version, lo, hi, since)[:top]


def build_digest(conn, cfg: dict, hours: int = 24, top: int = 20) -> str:
    """本地 Markdown 日报（归档用）。"""
    rows = _digest_rows(conn, cfg, hours, top)
    lo, hi = cfg["thresholds"]["digest"], cfg["thresholds"]["push"]
    today = datetime.now().strftime("%Y-%m-%d")
    out = [f"# linux.do 频道精选 {today}", f"（{hours}h 内 {lo}≤score<{hi}，共 {len(rows)} 条）", ""]
    if not rows:
        out.append("_今日无中档内容_")
    for r in rows:
        label = SHORT_LABEL.get(r["label"], r["label"] or "未知")
        emoji = LABEL_EMOJI.get(r["label"], "❔")
        link = _post_url(r, cfg["channel"])
        title, body = _title_body(r)
        out.append(f"## {emoji}【{label}{r['score']:.2f}】{title[:80]}")
        out.append(link)
        if body:
            out.append("")
            out.append(body)
        out.append("")
    md = "\n".join(out)
    path = LOG_DIR / f"digest-{today}.md"
    path.write_text(md, encoding="utf-8")
    return md


def format_digest_message(conn, cfg: dict, hours: int = 24, top: int = 20) -> str:
    """Telegram 版日报，与单条推送同一套排版。"""
    rows = _digest_rows(conn, cfg, hours, top)
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"📋 <b>linux.do 精选 · {today}</b>"
    if not rows:
        return f"{header}\n\n今日无中档内容"

    parts: list[str] = []
    for r in rows:
        head, body, _link = _entry(r, cfg["channel"])
        item = head + (f"\n{_esc(body)}" if body else "")
        parts.append(item)

    # 逐条累加，超长就截断（Telegram 单条上限 4096）
    msg = header
    for item in parts:
        if len(msg) + len(item) + 2 > 3800:
            msg += "\n\n…（更多见本地 logs/digest-*.md）"
            break
        msg += f"\n\n{item}"
    return msg


def send_digest(conn, cfg: dict, hours: int = 24) -> str:
    """生成并推送日报，同时落一份 Markdown 到 logs/。"""
    md = build_digest(conn, cfg, hours=hours)   # 落盘
    send_message(cfg, format_digest_message(conn, cfg, hours=hours))
    return md
