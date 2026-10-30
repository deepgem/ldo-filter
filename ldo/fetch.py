"""抓取层：t.me/s/ 匿名公开预览，无登录、无 api_id。"""
from __future__ import annotations

import time

from . import db, net, parse, rules

BASE = "https://t.me/s/{channel}"
LOG_PREFIX = "[fetch]"


def fetch_page(channel: str, before: int | None, cfg: dict) -> str:
    url = BASE.format(channel=channel)
    if before:
        url += f"?before={before}"
    f = cfg["fetch"]
    return net.http_get(url, timeout=f["timeout_sec"], retries=f["retries"])


def ingest(conn, msgs: list[dict], cfg: dict) -> tuple[int, int, int]:
    """落库。返回 (新增, 重复跳过, 低于阈值仅归档)。"""
    new = dup = low = 0
    max_dist = cfg["rules"]["simhash_distance"]
    for m in msgs:
        m["simhash"] = rules.simhash(f"{m.get('topic','')}\n{m.get('text','')}")
        m["prescore"] = rules.prescore(m)
        if rules.is_dup(conn, m, max_dist=max_dist):
            dup += 1
            continue
        if db.upsert_message(conn, m):
            new += 1
        if m["prescore"] < cfg["rules"]["min_prescore_for_llm"]:
            low += 1
    return new, dup, low


def tick(conn, cfg: dict, verbose: bool = True) -> list[dict]:
    """拉最新一页，只处理比 last_seen 新的消息。"""
    channel = cfg["channel"]
    last_seen = int(db.get_state(conn, "last_seen_id", "0") or 0)
    html = fetch_page(channel, None, cfg)
    msgs = parse.parse_page(html, channel)
    if not msgs:
        return []
    fresh = [m for m in msgs if m["msg_id"] > last_seen]
    new, dup, low = ingest(conn, fresh, cfg)
    top = max(m["msg_id"] for m in msgs)
    if top > last_seen:
        db.set_state(conn, "last_seen_id", str(top))
    if verbose:
        print(f"{LOG_PREFIX} page={len(msgs)} fresh={len(fresh)} new={new} dup={dup} archived_only={low}")
    return fresh


def backfill(conn, cfg: dict, pages: int = 50, verbose: bool = True) -> int:
    """用 ?before= 向前回溯历史，冷启动建语料库。"""
    channel = cfg["channel"]
    cursor = int(db.get_state(conn, "backfill_cursor", "0") or 0)
    if not cursor:
        # 从已抓到的最老一条继续往前
        row = conn.execute("SELECT MIN(msg_id) AS m FROM messages").fetchone()
        cursor = (row["m"] or 0) if row else 0
    if not cursor:
        tick(conn, cfg)
        row = conn.execute("SELECT MIN(msg_id) AS m FROM messages").fetchone()
        cursor = (row["m"] or 1) if row else 1

    gap = cfg["fetch"]["page_gap_sec"]
    total_new = 0
    for i in range(pages):
        html = fetch_page(channel, cursor, cfg)
        msgs = parse.parse_page(html, channel)
        if not msgs:
            if verbose:
                print(f"{LOG_PREFIX} backfill 到头了 (before={cursor})")
            break
        new, dup, low = ingest(conn, msgs, cfg)
        total_new += new
        cursor = min(m["msg_id"] for m in msgs)
        db.set_state(conn, "backfill_cursor", str(cursor))
        if verbose:
            print(f"{LOG_PREFIX} backfill {i+1}/{pages} before={cursor} got={len(msgs)} new={new} dup={dup}")
        time.sleep(gap)
    return total_new
