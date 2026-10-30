"""命令行入口。"""
from __future__ import annotations

import argparse
import sys
import time

from . import classify, classify_jev, db, fetch, parse, publish
from .config import load_config


def _judge(conn, cfg: dict, limit: int, verbose: bool = True) -> int:
    """按 config.classifier 选择判定后端。"""
    backend = cfg.get("classifier", "llm")
    if backend == "jev":
        if not cfg.get("jev", {}).get("api_key"):
            print("[judge] classifier=jev 但 TYPESAFE_API_KEY 为空，回退 llm")
            return classify.run_pending(conn, cfg, limit=limit, verbose=verbose)
        return classify_jev.run_pending(conn, cfg, limit=limit, verbose=verbose)
    return classify.run_pending(conn, cfg, limit=limit, verbose=verbose)


def cmd_tick(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    fresh = fetch.tick(conn, cfg)
    if fresh and not args.no_llm:
        _judge(conn, cfg, limit=cfg["llm"].get("max_per_cycle", 500))
        publish.push_new(conn, cfg)


def cmd_run(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    interval = cfg["poll_interval_sec"]

    token_ok = bool(cfg["telegram"]["bot_token"])
    chat_ok = bool(cfg["telegram"]["chat_id"])
    print(f"[run] 频道={cfg['channel']} 轮询={interval}s 阈值={cfg['thresholds']['push']}")
    if not (token_ok and chat_ok):
        print(
            "[run] ⚠️  推送未启用："
            + ("TG_BOT_TOKEN 为空 " if not token_ok else "")
            + ("TG_CHAT_ID 为空 " if not chat_ok else "")
            + "—— 消息会照常抓取和判定，但不会发到 Bot"
        )
    else:
        print("[run] ✅ 推送已启用")
    print(f"[run] 日志写到 logs/run.log（launchd 下）", flush=True)

    while True:
        try:
            fresh = fetch.tick(conn, cfg)
            if not args.no_llm:
                # 每次都跑：处理本轮新增 + 补上次失败的积压
                _judge(conn, cfg, limit=cfg["llm"].get("max_per_cycle", 500), verbose=bool(fresh))
            publish.push_new(conn, cfg)

            # 日报：用 state 保证一天只发一次（进程重启也不会重发）
            today = time.strftime("%Y-%m-%d")
            if (
                time.localtime().tm_hour >= cfg["digest_hour"]
                and db.get_state(conn, "last_digest_date", "") != today
            ):
                print("[run] 生成日报", flush=True)
                publish.send_digest(conn, cfg)
                db.set_state(conn, "last_digest_date", today)
        except KeyboardInterrupt:
            print("[run] 退出", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[run] ERROR {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)


def cmd_backfill(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    n = fetch.backfill(conn, cfg, pages=args.pages)
    print(f"[backfill] 新增 {n} 条")


def cmd_judge(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    _judge(conn, cfg, limit=args.limit)


def cmd_rejudge(args) -> None:
    """换 prompt 版本后重判：把指定版本的 verdict 清掉再跑。"""
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    v = args.version or cfg["llm"]["prompt_version"]
    if args.reset:
        n = conn.execute("DELETE FROM verdicts WHERE version=?", (v,)).rowcount
        conn.commit()
        print(f"[rejudge] 清除 {n} 条 version={v} 的判定")
    _judge(conn, cfg, limit=args.limit)


def cmd_digest(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    md = publish.build_digest(conn, cfg, hours=args.hours)
    print(md)
    if args.send:
        publish.send_digest(conn, cfg, hours=args.hours)


def cmd_flush(args) -> None:
    """把当前积压的待推送消息标记为已推送（不发送），避免首次启动刷屏。"""
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    version = cfg["llm"]["prompt_version"]
    rows = db.unpushed_above(conn, version, cfg["thresholds"]["push"], limit=10000)
    if not rows:
        print("没有积压")
        return
    for r in rows:
        db.mark_pushed(conn, r["msg_id"], "push")
    print(f"已静默 {len(rows)} 条积压（不发送），之后只推新内容。")
    print("想回看这批：python3 -m ldo show -n 50")


def cmd_doctor(args) -> None:
    """全链路自检：抓取 / LLM / Bot / 推送目标。"""
    ok_all = True

    def line(ok: bool, name: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        print(f"{'✅' if ok else '❌'} {name:<14} {detail}")

    cfg = load_config()

    # 1. 配置
    line(True, "config", f"channel={cfg['channel']} push阈值={cfg['thresholds']['push']}")

    # 2. 数据库
    try:
        conn = db.connect(cfg["db_path"])
        n = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        line(True, "database", f"{n} 条消息 @ {cfg['db_path']}")
    except Exception as e:  # noqa: BLE001
        line(False, "database", str(e))
        return

    # 3. 抓取
    try:
        html = fetch.fetch_page(cfg["channel"], None, cfg)
        msgs = parse.parse_page(html, cfg["channel"])
        line(bool(msgs), "fetch", f"t.me/s/{cfg['channel']} 拿到 {len(msgs)} 条")
    except Exception as e:  # noqa: BLE001
        line(False, "fetch", str(e))

    # 4. LLM
    try:
        v = classify.classify_one(
            {"msg_id": 0, "topic": "自检：我写了个开源工具", "text": "开源项目 https://github.com/x/y",
             "links": ["https://github.com/x/y"]},
            cfg,
        )
        line(True, "llm", f"{cfg['llm']['model']} → {v['label']} {v['score']:.2f}")
    except Exception as e:  # noqa: BLE001
        line(False, "llm", f"{type(e).__name__}: {e}")

    # 5. Bot token
    token = cfg["telegram"]["bot_token"]
    if not token:
        line(False, "bot token", "TG_BOT_TOKEN 为空 —— 去 @BotFather 建 bot 拿 token 填进 .env")
    else:
        try:
            me = publish.tg_call(cfg, "getMe", {})
            line(True, "bot token", f"@{me['result']['username']}")
        except Exception as e:  # noqa: BLE001
            line(False, "bot token", str(e))

    # 6. chat_id
    cid = cfg["telegram"]["chat_id"]
    if not cid:
        if token:
            found = publish.discover_chat_id(cfg)
            if found:
                line(False, "chat_id", f"为空，但检测到 {found} —— 填进 .env 的 TG_CHAT_ID")
            else:
                line(False, "chat_id", "为空；先给 bot 发一条消息，再跑 `python3 -m ldo whoami`")
        else:
            line(False, "chat_id", "未配置（先解决 bot token）")
    else:
        try:
            publish.tg_call(cfg, "getChat", {"chat_id": cid})
            line(True, "chat_id", cid)
        except Exception as e:  # noqa: BLE001
            line(False, "chat_id", str(e))

    print()
    if ok_all:
        print("全链路就绪 → 直接 `python3 -m ldo run` 或 `./install.sh` 装常驻")
    else:
        print("先修上面 ❌ 的项，再跑 `python3 -m ldo doctor` 复检")


def cmd_whoami(args) -> None:
    cfg = load_config()
    if not cfg["telegram"]["bot_token"]:
        print("TG_BOT_TOKEN 为空：先填 .env 的 TG_BOT_TOKEN")
        return
    cid = publish.discover_chat_id(cfg)
    print(f"chat_id = {cid}")
    if cid:
        print("把它写进 .env 的 TG_CHAT_ID，然后 `python -m ldo notify` 测一下")
        publish.send_message(cfg, "✅ ldo-filter 推送通道已连通", chat_id=cid)


def cmd_notify(args) -> None:
    cfg = load_config()
    ok = publish.send_message(cfg, "✅ ldo-filter 推送通道已连通")
    print("ok" if ok else "失败：检查 TG_BOT_TOKEN / TG_CHAT_ID")


def cmd_stats(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    total = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    judged = conn.execute("SELECT COUNT(*) c FROM verdicts").fetchone()["c"]
    print(f"messages={total} verdicts={judged} last_seen={db.get_state(conn,'last_seen_id','0')}")
    print("\n按 label 分布：")
    for r in conn.execute(
        "SELECT label, COUNT(*) c, ROUND(AVG(score),3) avg FROM verdicts GROUP BY label ORDER BY c DESC"
    ):
        print(f"  {r['label']:<16} {r['c']:>5}  avg={r['avg']}")
    print("\n分数分布：")
    for lo, hi in ((0.8, 1.01), (0.55, 0.8), (0.35, 0.55), (0.0, 0.35)):
        c = conn.execute("SELECT COUNT(*) c FROM verdicts WHERE score>=? AND score<?", (lo, hi)).fetchone()["c"]
        print(f"  [{lo:.2f},{hi:.2f})  {c}")


def cmd_show(args) -> None:
    cfg = load_config()
    conn = db.connect(cfg["db_path"])
    rows = conn.execute(
        """SELECT m.msg_id, m.topic, m.author, v.label, v.score, v.reason
           FROM messages m LEFT JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           ORDER BY m.msg_id DESC LIMIT ?""",
        (cfg["llm"]["prompt_version"], args.n),
    ).fetchall()
    for r in rows:
        score = f"{r['score']:.2f}" if r["score"] is not None else "  - "
        print(f"{r['msg_id']} {score} {(r['label'] or '-'):<14} {(r['topic'] or r['author'] or '')[:50]}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ldo", description="linux.do 频道 AI 过滤流水线")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tick", help="拉一次增量并判定推送")
    t.add_argument("--no-llm", action="store_true")
    t.set_defaults(func=cmd_tick)

    r = sub.add_parser("run", help="常驻轮询")
    r.add_argument("--no-llm", action="store_true")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backfill", help="回溯历史")
    b.add_argument("--pages", type=int, default=50)
    b.set_defaults(func=cmd_backfill)

    j = sub.add_parser("judge", help="判定待处理消息")
    j.add_argument("--limit", type=int, default=500)
    j.set_defaults(func=cmd_judge)

    rj = sub.add_parser("rejudge", help="重判（配合 prompt 版本）")
    rj.add_argument("--version", default=None)
    rj.add_argument("--reset", action="store_true")
    rj.add_argument("--limit", type=int, default=500)
    rj.set_defaults(func=cmd_rejudge)

    d = sub.add_parser("digest", help="生成日报")
    d.add_argument("--hours", type=int, default=24)
    d.add_argument("--send", action="store_true")
    d.set_defaults(func=cmd_digest)

    fl = sub.add_parser("flush", help="静默积压（标记已推送但不发送）")
    fl.set_defaults(func=cmd_flush)

    dr = sub.add_parser("doctor", help="全链路自检")
    dr.set_defaults(func=cmd_doctor)

    w = sub.add_parser("whoami", help="自动发现 bot chat_id")
    w.set_defaults(func=cmd_whoami)

    n = sub.add_parser("notify", help="测试推送")
    n.set_defaults(func=cmd_notify)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(func=cmd_stats)

    sh = sub.add_parser("show", help="看最近消息")
    sh.add_argument("-n", type=int, default=30)
    sh.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
